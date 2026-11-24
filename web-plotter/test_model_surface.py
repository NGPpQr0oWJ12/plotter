from collections import Counter
from copy import deepcopy
import math
import unittest

from model_surface import MAX_FACE_EDGES, build_surface_model, surface_node_id, validate_model_edges


def model(*, hole=False):
    vertices = [[0, 0, 0], [20, 0, 0], [20, 20, 0], [0, 20, 0]]
    triangles = [[0, 1, 2], [0, 2, 3]]
    rings = [[0, 1, 2, 3]]
    if hole:
        vertices += [[8, 8, 0], [12, 8, 0], [12, 12, 0], [8, 12, 0]]
        triangles = [[0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                     [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
        rings.append([4, 5, 6, 7])
    return {"name": "Model", "nodes": [{"id": "root", "name": "Model", "visible": True, "parent": None,
            "vertices": vertices, "triangles": triangles, "faces": ["sheet"] * len(triangles),
            "edges": [[0, 1], [1, 2], [2, 3], [3, 0]], "faceLoops": {"sheet": rings},
            "faceFlags": {"sheet": {"visible": True, "frontColor": [1, 2, 3]}}}]}


def edge(identifier, a, b, *, node_id="root", face_id="sheet"):
    return {"id": identifier, "nodeId": node_id, "faceId": face_id, "points": [a, b]}


def areas(node):
    result = Counter()
    for face, triangle in zip(node["faces"], node["triangles"], strict=True):
        a, b, c = [node["vertices"][index] for index in triangle]
        result[face] += ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2
    return sorted(result.values())


class ModelSurfaceTests(unittest.TestCase):
    def test_cut_creates_two_selectable_faces_without_changing_original_indices(self):
        source = model()
        original = deepcopy(source)
        edited = build_surface_model(source, [edge("cut", [7, -.001, .001], [7, 20.001, 0])])
        base, derived = edited["nodes"]
        self.assertEqual(source, original)
        self.assertEqual(base, {**source["nodes"][0], "editorHiddenFaces": ["sheet"]})
        self.assertEqual(derived["id"], surface_node_id("root", "sheet"))
        self.assertEqual((derived["parent"], derived["editorSourceNode"], derived["editorSourceFace"]),
                         ("root", "root", "sheet"))
        self.assertEqual(areas(derived), [140, 260])
        self.assertEqual(len(derived["edges"]), 1)
        self.assertEqual(list(derived["faceFlags"].values()), [source["nodes"][0]["faceFlags"]["sheet"]] * 2)
        self.assertTrue(all(len(rings) == 1 for rings in derived["faceLoops"].values()))
        self.assertEqual(build_surface_model(source, []), source)

    def test_dangling_edges_keep_one_face_until_t_junction_reaches_opposite_border(self):
        source = model()
        hanging = [edge("one", [0, 7, 0], [10, 7, 0]), edge("two", [10, 7, 0], [10, 14, 0])]
        interior = build_surface_model(source, hanging)["nodes"][-1]
        self.assertEqual(areas(interior), [400])
        self.assertEqual(len(interior["edges"]), 2)
        completed = build_surface_model(source, [*hanging, edge("three", [5, 7, 0], [5, 20, 0])])["nodes"][-1]
        self.assertEqual(areas(completed), [65, 335])
        self.assertEqual(len(completed["edges"]), 4)

    def test_crossing_strokes_split_into_four_regions_and_rebuild_deterministically(self):
        source = model()
        edges = [edge("one", [0, 7, 0], [20, 7, 0]), edge("two", [8, 0, 0], [8, 20, 0])]
        reversed_edges = [{**item, "points": item["points"][::-1]} for item in reversed(edges)]
        first = build_surface_model(source, edges)
        self.assertEqual(first, build_surface_model(source, reversed_edges))
        derived = first["nodes"][-1]
        self.assertEqual(areas(derived), [56, 84, 104, 156])
        self.assertEqual(len(derived["edges"]), 4)
        center = derived["vertices"].index([8, 7, 0])
        self.assertTrue(all(center in line for line in derived["edges"]))

    def test_closed_chain_creates_an_inner_face_and_a_face_with_a_hole(self):
        source = model()
        edges = [edge("a", [4, 3, 0], [12, 3, 0]), edge("b", [12, 3, 0], [12, 11, 0]),
                 edge("c", [12, 11, 0], [4, 11, 0]), edge("d", [4, 11, 0], [4, 3, 0])]
        derived = build_surface_model(source, edges)["nodes"][-1]
        self.assertEqual(areas(derived), [64, 336])
        self.assertEqual(sorted(map(len, derived["faceLoops"].values())), [1, 2])
        self.assertEqual(len(derived["edges"]), 4)

    def test_a_hole_is_not_filled_or_bridged(self):
        source = model(hole=True)
        with self.assertRaisesRegex(ValueError, "целиком"):
            build_surface_model(source, [edge("bad", [0, 9, 0], [20, 9, 0])])
        edges = [edge("left", [0, 9, 0], [7.999, 9, 0]), edge("right", [12, 9, 0], [20, 9, 0])]
        derived = build_surface_model(source, edges)["nodes"][-1]
        self.assertEqual(areas(derived), [176, 208])
        self.assertEqual(len(derived["edges"]), 2)

    def test_boundary_only_and_coincident_strokes_do_not_duplicate_geometry(self):
        source = model()
        edges = [edge("border", [4, 0, 0], [16, 0, 0]), edge("cut", [0, 9, 0], [20, 9, 0]),
                 edge("same", [20, 9, 0], [0, 9, 0]), edge("partial", [4, 9, 0], [16, 9, 0])]
        derived = build_surface_model(source, edges)["nodes"][-1]
        self.assertEqual(areas(derived), [180, 220])
        self.assertEqual(len(derived["edges"]), 3)
        self.assertTrue(all(derived["vertices"][i][1] == 9 for line in derived["edges"] for i in line))

    def test_tilted_face_uses_source_winding_and_triangle_fallback(self):
        source = model()
        base = source["nodes"][0]
        base.pop("faceLoops")
        base["vertices"] = [[5 + x, 4 + y, 2 + y] for x, y, _ in base["vertices"]]
        derived = build_surface_model(source, [edge("cut", [13, 4, 2], [13, 24, 22])])["nodes"][-1]
        for area, expected in zip(areas(derived), [160, 240], strict=True):
            self.assertAlmostEqual(area, expected, places=4)
        self.assertTrue(all(math.isclose(y - z, 2, abs_tol=1e-9) for _, y, z in derived["vertices"]))

    def test_rejects_invalid_records_indices_and_points(self):
        source = model()
        for record, error in [
                (edge("off", [0, 9, 0], [20, 9, -1]), "плоскости"),
                (edge("outside", [0, 9, 0], [21, 9, 0]), "целиком"),
                (edge("zero", [0, 9, 0], [0, 9, 0]), "разные"),
                (edge("bad", [0, 9, math.nan], [20, 9, 0]), "конечными"),
                (edge("bad", [0, 9, False], [20, 9, 0]), "конечными"),
                (edge("missing", [0, 9, 0], [20, 9, 0], node_id="unknown"), "компонент"),
                (edge("missing", [0, 9, 0], [20, 9, 0], face_id="unknown"), "грань")]:
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, error):
                build_surface_model(source, [record])
        valid = edge("cut", [0, 9, 0], [20, 9, 0])
        with self.assertRaisesRegex(ValueError, "уникальными"):
            validate_model_edges([valid, valid])
        with self.assertRaisesRegex(ValueError, str(MAX_FACE_EDGES)):
            validate_model_edges([{**valid, "id": str(i)} for i in range(MAX_FACE_EDGES + 1)])
        source["nodes"][0]["triangles"][0][2] = -1
        with self.assertRaisesRegex(ValueError, "вершина"):
            build_surface_model(source, [valid])


if __name__ == "__main__":
    unittest.main()
