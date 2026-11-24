import math
import unittest
from copy import deepcopy
from unittest.mock import patch

from shapely.geometry import LineString, Point, Polygon

from designer_cad import prepare_design
from upholstery import (UpholsteryError, build_entities, cad_sync_geometry, calculate, check_cover_panel, check_panel_placement, check_transfer,
                        edit_cover_cut, preview_contour, project_cad_contour, remaining_panels, route_contour, selected_panel_regions)


def model(points, triangles):
    return {"nodes": [{"id": "surface", "name": "Surface", "vertices": points,
                       "triangles": triangles, "faces": ["face"] * len(triangles)}]}


def panel(identifier="panel", **options):
    return {"id": identifier, "nodeId": "surface", "faces": [], **options}


def seam(identifier, anchors, **options):
    return {"id": identifier, "nodeId": "surface", "anchors": anchors, "kind": "seam", **options}


def grid(size, point, skip=()):
    vertices = [point(x, y) for y in range(size) for x in range(size)]
    triangles = []
    for y in range(size - 1):
        for x in range(size - 1):
            if (x, y) in skip:
                continue
            a = y * size + x
            triangles.extend([[a, a + 1, a + size + 1], [a, a + size + 1, a + size]])
    return model(vertices, triangles)


class UpholsteryTests(unittest.TestCase):
    def assert_isometric(self, source, pattern):
        node = source["nodes"][0]
        lengths = lambda tri, vertices: sorted(math.dist(vertices[tri[i - 1]], vertices[tri[i]]) for i in range(3))
        for i, triangle in enumerate(pattern["triangles"]):
            original = node["triangles"][pattern["sourceTriangles"][i]]
            for length, target in zip(lengths(triangle, pattern["vertices"]), lengths(original, node["vertices"])):
                self.assertLess(abs(length - target), 0.02)

    def test_tilted_plane_with_hole_grain_and_allowance(self):
        source = grid(4, lambda x, y: [30 * x, 24 * y, 18 * y], skip=[(1, 1)])
        result = calculate(source, {"panels": [panel(grain=[0, 3])]})
        self.assertTrue(result["complete"], result["warnings"])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "flat")
        self.assertEqual(len(pattern["loops"]), 2)
        self.assertAlmostEqual(math.dist(*pattern["grain"]), 90)
        self.assert_isometric(source, pattern)
        entities = build_entities(result, 5, 0)
        contours = {entity["layer"]: Polygon(entity["points"]) for entity in entities if not entity.get("reference")}
        self.assertAlmostEqual(contours["Срез"].area, 100**2)
        self.assertAlmostEqual(contours["Срез отверстия"].area, 20**2)
        self.assertEqual(sum(path["closed"] for path in prepare_design(entities)["paths"]), 2)
        with self.assertRaisesRegex(UpholsteryError, "закрывает отверстие"):
            build_entities(result, 16, 0)
        state = {"panels": [panel(grain=[0, 3])]}
        clear = check_transfer(result, state, 0, 0)["panels"][0]
        self.assertTrue(clear["ready"])
        blocked = check_transfer(result, state, 15, 0)["panels"][0]
        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["issues"][0]["code"], "allowance_closes_hole")
        self.assertIn("15 мм", blocked["issues"][0]["message"])
        self.assertIn("Уменьшите", blocked["issues"][0]["detail"])
        suggestion = {"allowanceMm": 15, "holeAllowanceMm": 14, "notchDepthMm": 0}
        self.assertEqual(blocked["suggestion"], suggestion)
        overrides = {"panel": suggestion}
        repaired = check_transfer(result, state, 15, 0, panel_settings=overrides)["panels"][0]
        self.assertTrue(repaired["ready"])
        self.assertEqual(repaired["settings"], suggestion)
        adjusted = build_entities(result, 15, 0, state=state, panel_settings=overrides)
        contours = {entity["layer"]: Polygon(entity["points"]) for entity in adjusted if not entity.get("reference")}
        self.assertAlmostEqual(contours["Срез"].area, 120**2)
        self.assertAlmostEqual(contours["Срез отверстия"].area, 2**2)

    def test_developable_cylinder_retains_triangle_lengths(self):
        source = grid(5, lambda x, y: [200 * math.sin(x * 0.3), 40 * y, 200 * math.cos(x * 0.3)])
        result = calculate(source, {"panels": [panel()]})
        self.assertTrue(result["complete"], result["warnings"])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "developable")
        self.assert_isometric(source, pattern)
        self.assertLess(max(max(metrics.values()) for metrics in pattern["strain"]), 1e-6)

    def test_cylinder_sews_two_banks_of_the_same_cut(self):
        count = 8
        vertices = [[30 * math.cos(i * math.tau / count), 30 * math.sin(i * math.tau / count), height]
                    for height in [0, 50] for i in range(count)]
        triangles = []
        for a in range(count):
            b = (a + 1) % count
            triangles.extend([[a, b, b + count], [a, b + count, a + count]])
        edge = {"panelId": "panel", "seamId": "open"}
        state = {"panels": [panel()], "seams": [seam("open", [{"vertex": 0}, {"vertex": count}])],
                 "pairs": [{"id": "close", "a": edge, "b": edge, "marks": [.4]}]}
        source = model(vertices, triangles)
        result = calculate(source, state)
        self.assertTrue(result["complete"], result["warnings"])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "developable")
        self.assert_isometric(source, pattern)
        self.assertAlmostEqual(math.dist(*(mark["point"] for mark in pattern["marks"])), 60 * count * math.sin(math.pi / count))
        self.assertEqual(sum(entity.get("notch", False) for entity in build_entities(result, 4, 2)), 2)

    def test_rounded_panel_returns_finite_material_distortion(self):
        source = grid(5, lambda x, y: [25 * x, 25 * y, 4 * ((x - 2)**2 + (y - 2)**2)])
        result = calculate(source, {"panels": [panel(grain=[0, 4])],
                                    "material": {"stretch": 0.01, "compression": 0.01, "shear": 0.01}})
        self.assertTrue(result["complete"], result["warnings"])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "rounded")
        self.assertEqual(len(pattern["triangles"]), len(pattern["strain"]))
        self.assertTrue(all(math.isfinite(value) and value >= 0 for strain in pattern["strain"] for value in strain.values()))
        self.assertTrue(any("превышает допуск" in warning for warning in result["warnings"]))
        diagnostics = [item for item in result["diagnostics"] if item["code"] == "material_limit"]
        self.assertTrue(diagnostics)
        self.assertTrue(all(item["panelId"] == "panel" and item["severity"] == "warning" for item in diagnostics))
        self.assertIn("вытачку", diagnostics[0]["detail"])
        paths = prepare_design(build_entities(result, 0, 0))["paths"]
        self.assertEqual(sum(path["closed"] for path in paths), 1)

    def test_rounded_pair_fits_ease_and_preserves_source_marks(self):
        source = grid(5, lambda x, y: [25 * x, 25 * y, 4 * ((x - 2)**2 + (y - 2)**2)])
        flat = model([[0, 0, 0], [120, 0, 0], [120, 35, 0], [0, 35, 0]], [[0, 1, 2], [0, 2, 3]])["nodes"][0]
        flat["id"] = "flat"
        source["nodes"].append(flat)
        first_five = source["nodes"][0]["vertices"][:5]
        fraction = math.dist(*first_five[:2]) / sum(math.dist(a, b) for a, b in zip(first_five, first_five[1:]))
        pair = {"id": "generated-id", "a": {"panelId": "curve", "seamId": "curve"},
                "b": {"panelId": "flat", "seamId": "flat", "reverse": True},
                "marks": [fraction], "ease": [4, -3]}
        state = {"panels": [panel("curve"), panel("flat", nodeId="flat")],
                 "seams": [seam("curve", [{"vertex": i} for i in range(5)]),
                           seam("flat", [{"vertex": 0}, {"vertex": 1}], nodeId="flat")]}
        unpaired = calculate(source, state)
        paired = calculate(source, {**state, "pairs": [pair]})
        self.assertTrue(paired["complete"], paired["warnings"])
        curve, flat = paired["panels"]
        self.assertEqual(flat["vertices"], unpaired["panels"][1]["vertices"])
        self.assertNotEqual(curve["strain"], unpaired["panels"][0]["strain"])
        self.assertEqual(len(curve["sourceVertices"]), len(curve["vertices"]))
        self.assertEqual(curve["marks"][0]["label"], "1.1")
        self.assertLess(math.dist(curve["marks"][0]["point"], curve["guides"][0]["points"][1]), 1e-8)
        flat_guide = flat["guides"][0]["points"]
        self.assertAlmostEqual(math.dist(flat["marks"][0]["point"], flat_guide[-1]), fraction * 120)
        intervals = paired["pairs"][0]["intervals"]
        self.assertAlmostEqual(intervals[0]["a"], math.dist(*curve["guides"][0]["points"][:2]))
        self.assertAlmostEqual(intervals[0]["b"], 120 * fraction)
        self.assertLess(abs(intervals[0]["delta"] - 4), 1.5)
        self.assertLess(abs(intervals[1]["delta"] + 3), 1)

    def test_high_deformation_warning_includes_relief_without_blocking_valid_cut(self):
        source = grid(5, lambda x, y: [25 * x, 25 * y, 12 * ((x - 2)**2 + (y - 2)**2)])
        state = {"panels": [panel()]}
        result = calculate(source, state)
        warnings = [diagnostic for diagnostic in result["diagnostics"] if diagnostic["code"] == "high_deformation"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["severity"], "warning")
        self.assertIn("добавьте вытачку", warnings[0]["detail"])
        readiness = check_transfer(result, state, 0, 0)["panels"][0]
        self.assertTrue(readiness["ready"])
        self.assertEqual([issue["code"] for issue in readiness["issues"]], ["ready", "high_deformation"])

    def test_barycentric_cut_pair_and_physical_notches(self):
        source = model([[0, 0, 0], [60, 0, 0], [60, 40, 0], [0, 40, 0]], [[0, 1, 2], [0, 2, 3]])
        line = seam("cut", [{"triangle": 1, "barycentric": [0.5, 0, 0.5]},
                            {"triangle": 0, "barycentric": [0, 0.5, 0.5]}])
        side_a = panel("a", seed=0, seedBarycentric=[0.7, 0.1, 0.2])
        side_b = panel("b", seed=0, seedBarycentric=[0.1, 0.1, 0.8])
        state = {"panels": [side_a, side_b], "seams": [line],
                 "pairs": [{"id": "p", "a": {"panelId": "a", "seamId": "cut"},
                            "b": {"panelId": "b", "seamId": "cut", "reverse": True},
                            "marks": [0.25, 0.75], "ease": [2, -1]}]}
        result = calculate(source, state)
        self.assertTrue(result["complete"], result["warnings"])
        self.assertEqual(result["seams"][0]["panelIds"], ["a", "b"])
        self.assertEqual(len(result["seams"][0]["points"]), 3)
        self.assertEqual([bank["sourcePoints"] for bank in result["seams"][0]["banks"]], [result["seams"][0]["points"]]*2)
        intervals = result["pairs"][0]["intervals"]
        self.assertEqual([interval["ease"] for interval in intervals], [2, -1, 0])
        for interval, length in zip(intervals, [15, 30, 15]):
            self.assertAlmostEqual(interval["a"], length)
            self.assertAlmostEqual(interval["b"], length)
            self.assertAlmostEqual(interval["delta"], 0)
        for pattern in result["panels"]:
            self.assertEqual(len(pattern["marks"]), 2)
            self.assertAlmostEqual(Polygon([pattern["vertices"][i] for i in pattern["loops"][0]]).area, 1200)
        entities = build_entities(result, 6, 2)
        notches = [entity for entity in entities if entity.get("notch")]
        self.assertEqual(len(notches), 4)
        self.assertTrue(all(abs(math.dist(notch["start"], notch["end"]) - 2) < 1e-6 for notch in notches))
        self.assertEqual(sum(path.get("notch", False) for path in prepare_design(entities)["paths"]), 4)
        self.assertEqual(len({entity["groupPath"][1] for entity in entities if entity.get("groupPath")}), 2)
        selected = build_entities(result, 6, 2, state=state, panel_ids=["a"])
        selected_notches = [entity for entity in selected if entity.get("notch")]
        self.assertEqual([entity["layer"] for entity in selected if entity["type"] == "point"], ["Метка 1.1", "Метка 1.2"])
        outlines = [entity["id"] for entity in selected if entity["layer"] == "Срез" and not entity.get("notch")]
        self.assertEqual(len(outlines), 1)
        self.assertEqual([entity["parentId"] for entity in selected_notches], outlines * 2)
        marks_only = build_entities(result, 0, 0, state=state, panel_ids=["a"])
        self.assertEqual(sum(entity["type"] == "point" for entity in marks_only), 2)
        self.assertFalse(any(entity.get("notch") for entity in marks_only))
        with self.assertRaisesRegex(UpholsteryError, "превышать припуск"):
            build_entities(result, 1, 2)

    def test_explicit_edge_ranges_and_stable_paired_marks(self):
        source = model([[0, 0, 0], [80, 0, 0], [80, 40, 0], [0, 40, 0]], [[0, 1, 2], [0, 2, 3]])
        state = {"panels": [panel()]}
        pattern = calculate(source, state)["panels"][0]
        edges = {edge["sourcePoints"][0][1]: edge for edge in pattern["edges"]
                 if edge["sourcePoints"][0][1] == edge["sourcePoints"][1][1]}
        first = {"panelId": "panel", "edgeId": edges[0]["id"], "range": [0, .5]}
        second = {"panelId": "panel", "edgeId": edges[40]["id"], "range": [0, .5], "reverse": True}
        pair = {"id": "hem", "a": first, "b": second, "marks": [.75, .25], "markIds": ["tip", "base"]}
        result = calculate(source, {**state, "pairs": [pair]})
        self.assertTrue(result["complete"], result["diagnostics"])
        comparison, = result["pairs"]
        self.assertEqual([mark["id"] for mark in comparison["marks"]], ["base", "tip"])
        self.assertEqual([interval["a"] for interval in comparison["intervals"]], [10, 20, 10])
        self.assertEqual([interval["b"] for interval in comparison["intervals"]], [10, 20, 10])
        self.assertEqual(result["fitStitches"][0]["b"]["range"], [.5, 0])
        marks = result["panels"][0]["marks"]
        self.assertEqual([mark["id"] for mark in marks], ["base", "base", "tip", "tip"])
        self.assertEqual({mark["edgeId"] for mark in marks}, {edges[0]["id"], edges[40]["id"]})
        moved = calculate(source, {**state, "pairs": [{**pair, "marks": [.6, .4]}]})
        self.assertEqual([mark["id"] for mark in moved["pairs"][0]["marks"]], ["base", "tip"])
        duplicate = calculate(source, {**state, "pairs": [pair, {**pair, "id": "duplicate"}]})
        self.assertEqual([issue["pairId"] for issue in duplicate["diagnostics"] if issue["code"] == "pair_invalid"],
                         ["duplicate"])
        self_pair = calculate(source, {**state, "pairs": [{**pair, "b": {**first, "range": [.25, .75]}}]})
        self.assertFalse(self_pair["complete"])
        distinct = calculate(source, {**state, "pairs": [{**pair, "b": {**first, "range": [.5, 1]}}]})
        self.assertTrue(distinct["complete"], distinct["diagnostics"])

    def test_variable_cut_allowances_and_notches_follow_selected_edges(self):
        source = grid(4, lambda x, y: [30*x, 30*y, 0], skip=[(1, 1)])
        state = {"panels": [panel()]}
        pattern, = calculate(source, state)["panels"]
        hole, = pattern["holes"]
        outside = next(edge for edge in pattern["edges"]
                       if all(point[1] == 0 and 30 <= point[0] <= 60 for point in edge["sourcePoints"]))
        inside = next(edge for edge in pattern["edges"] if edge["id"] == hole["edgeIds"][0])
        pair = {"id": "hem", "marks": [.5], "a": {"panelId": "panel", "edgeId": outside["id"]},
                "b": {"panelId": "panel", "edgeId": inside["id"]}}
        state["pairs"] = [pair]
        result = calculate(source, state)
        overrides = {"panel": {"allowanceMm": 2, "holeAllowanceMm": 3, "holeAllowances": {hole["id"]: 4},
                               "edgeAllowances": {outside["id"]: 6, inside["id"]: 7}}}
        status, = check_transfer(result, state, 9, 5, panel_settings=overrides)["panels"]
        self.assertTrue(status["ready"], status["issues"])
        shapes = {entity["layer"]: Polygon(entity["points"]) for entity in status["preview"]
                  if entity.get("closed") and not entity.get("reference")}
        self.assertAlmostEqual(shapes["Срез"].area, 94**2 + 30*(6-2))
        self.assertAlmostEqual(shapes["Срез отверстия"].area, 19*22)
        notches = [item for item in status["preview"] if item.get("notch")]
        self.assertEqual({item["layer"] for item in notches}, {"Срез", "Срез отверстия"})
        for notch in notches:
            self.assertAlmostEqual(math.dist(*notch["points"]), 5)
            self.assertLess(shapes[notch["layer"]].boundary.distance(Point(notch["points"][0])), 1e-6)
        invalid, = check_transfer(result, state, 9, 6.5, panel_settings=overrides)["panels"]
        self.assertEqual(invalid["issues"][0]["code"], "notch_invalid")
        overrides["panel"]["edgeAllowances"][outside["id"]] = 0
        zero, = check_transfer(result, state, 9, 0, panel_settings=overrides)["panels"]
        cut = next(item for item in zero["preview"] if item["layer"] == "Срез")
        self.assertAlmostEqual(Polygon(cut["points"]).area, 94**2 - 30*2)
        overrides["panel"]["edgeAllowances"]["removed"] = 1
        stale, = check_transfer(result, state, 9, 0, panel_settings=overrides)["panels"]
        self.assertEqual(stale["issues"][0]["code"], "allowance_edge_unavailable")

    def test_variable_allowance_joins_unequal_offsets_at_an_oblique_corner(self):
        source = model([[0, 0, 0], [70, -10, 0], [35, 10, 0]], [[0, 1, 2]])
        state = {"panels": [panel()]}
        result = calculate(source, state)
        pattern, = result["panels"]
        settings = {"panel": {"edgeAllowances": {edge["id"]: width
                                                for edge, width in zip(pattern["edges"], [1, 12, 20])}}}
        status, = check_transfer(result, state, 5, 0, panel_settings=settings)["panels"]
        self.assertTrue(status["ready"], status["issues"])
        cut = next(item for item in status["preview"] if item["layer"] == "Срез")
        shell = Polygon(cut["points"])
        self.assertTrue(shell.is_valid)
        stitch_line = Polygon([pattern["vertices"][i] for i in pattern["loops"][0]])
        self.assertTrue(shell.covers(stitch_line))

    def test_welded_fabric_supports_seam_grain_pair_and_transfer(self):
        left = {"id": "left", "vertices": [[-40, 0, 0], [-0.015, 0, 0], [-0.015, 60, 0], [-40, 60, 0],
                                             [-20, 0, 0], [-0.011, 0, 0], [-50, 0, 0]],
                "triangles": [[0, 1, 2], [0, 2, 3], [2, 1, 0], [0, 4, 1], [1, 5, 2], [0, 3, 6]],
                "faces": ["face"] * 5 + ["hidden"], "faceFlags": {"hidden": {"visible": False}}}
        right = {"id": "right", "vertices": [[-0.011, 0, 0], [40, 0, 0], [40, 60, 0], [-0.011, 60, 0]],
                 "triangles": [[0, 1, 2], [0, 2, 3]], "faces": ["face", "face"]}
        source = {"nodes": [left, right]}
        original = deepcopy(source)
        fabric = {"id": "cover", "name": "Обивка", "color": "#365f82",
                  "surfaces": [{"nodeId": "left", "faces": ["face", "hidden"]},
                               {"nodeId": "right", "faces": ["face"]}]}
        node_id = "fabric:cover"
        line = seam("join", [{"vertex": 1}, {"vertex": 2}], nodeId=node_id)
        side_a = panel("a", nodeId=node_id, faces=["0:face"], grain=[0, 3])
        side_b = panel("b", nodeId=node_id, faces=["1:face"])
        pair = {"id": "p", "a": {"panelId": "a", "seamId": "join"},
                "b": {"panelId": "b", "seamId": "join", "reverse": True}, "marks": [0.25, 0.75]}
        state = {"fabricLayers": [fabric], "panels": [side_a, side_b], "seams": [line], "pairs": [pair]}

        result = calculate(source, state)
        self.assertTrue(result["complete"], result["warnings"])
        self.assertEqual(result, calculate(source, state))
        self.assertEqual(source, original)
        joined, = result["seams"]
        self.assertEqual((joined["nodeId"], joined["panelIds"]), (node_id, ["a", "b"]))
        self.assertEqual(joined["points"], [[-0.015, 0, 0], [-0.015, 60, 0]])
        a, b = result["panels"]
        self.assertEqual([set(pattern["sourceTriangles"]) for pattern in (a, b)], [{0, 1}, {2, 3}])
        self.assertAlmostEqual(math.dist(*a["grain"]), 60)
        self.assertEqual([len(pattern["marks"]) for pattern in (a, b)], [2, 2])
        entities = build_entities(result, 6, 2, state=state)
        self.assertEqual(sum(bool(entity.get("notch")) for entity in entities), 4)
        self.assertEqual(calculate(source, {**state, "autoNotches": True}), result)
        common, = result["joins"]
        combined = calculate(source, {**state, "joins": [{"id": common["id"], "marks": [.25, .75]}]})
        self.assertEqual([item["marks"] for item in combined["panels"]], [a["marks"], b["marks"]])
        unpaired = {**state, "pairs": []}
        baseline = calculate(source, unpaired)
        self.assertEqual([item["marks"] for item in baseline["panels"]], [[], []])
        automatic = calculate(source, {**unpaired, "autoNotches": True})
        self.assertEqual(automatic["pairs"], [])
        labels = []
        for item, original_item in zip(automatic["panels"], baseline["panels"]):
            self.assertEqual(item["vertices"], original_item["vertices"])
            mark, = item["marks"]
            guide, = item["guides"]
            for end in (guide["points"][0], guide["points"][-1]):
                self.assertAlmostEqual(math.dist(end, mark["point"]), 30)
            labels.append(mark["label"])
        self.assertEqual(labels[0], labels[1])
        notches = [entity for entity in build_entities(automatic, 6, 2, state=unpaired) if entity.get("notch")]
        self.assertEqual(len(notches), 2)
        self.assertEqual(len({entity["parentId"] for entity in notches}), 2)

    def test_notch_spacing_preflight_reports_only_the_affected_panel(self):
        source = model([[0, 0, 0], [60, 0, 0], [60, 40, 0], [0, 40, 0]], [[0, 1, 2], [0, 2, 3]])
        state = {"panels": [panel("tight"), panel("clear")]}
        result = calculate(source, state)
        result["panels"][0]["marks"] = [{"point": [20, 0], "label": "1.1"}, {"point": [20.2, 0], "label": "1.2"}]
        reports = check_transfer(result, state, 5, 2)["panels"]
        self.assertEqual([report["ready"] for report in reports], [False, True])
        self.assertEqual(reports[0]["issues"][0]["code"], "notch_invalid")
        self.assertIn("0,5 мм", reports[0]["issues"][0]["message"])
        self.assertEqual(reports[1]["issues"][0]["code"], "ready")
        self.assertEqual(len(build_entities(result, 5, 2, state=state, panel_ids=["clear"])), 2)
        self.assertTrue(check_transfer(result, state, 5, 0)["panels"][0]["ready"])

    def test_grain_surface_anchor_is_mapped_after_splitting_source_face(self):
        source = model([[0, 0, 0], [60, 0, 0], [60, 40, 0], [0, 40, 0]], [[0, 1, 2], [0, 2, 3]])
        line = seam("cut", [{"triangle": 1, "barycentric": [0.5, 0, 0.5]},
                            {"triangle": 0, "barycentric": [0, 0.5, 0.5]}])
        anchors = [{"vertex": 0}, {"triangle": 0, "barycentric": [0.5, 0.25, 0.25]}]
        lower = panel(seed=0, seedBarycentric=[0.7, 0.1, 0.2], grain=anchors)
        result = calculate(source, {"panels": [lower], "seams": [line]})
        self.assertTrue(result["complete"], result["diagnostics"])
        self.assertAlmostEqual(math.dist(*result["panels"][0]["grain"]), math.hypot(30, 10))
        out = build_entities(result, 0, 0)
        grain = next(item for item in out if item["layer"] == "Долевая")
        self.assertTrue(grain["reference"])
        self.assertAlmostEqual(math.dist(grain["start"], grain["end"]), math.hypot(30, 10), places=5)
        outside = {**lower, "grain": [anchors[0], {"triangle": 0, "barycentric": [0.1, 0.1, 0.8]}]}
        rejected = calculate(source, {"panels": [outside], "seams": [line]})
        self.assertFalse(rejected["complete"])
        self.assertIn("после разреза", rejected["diagnostics"][0]["message"])

    def test_conical_dart_opens_and_planar_dart_remains_reference(self):
        triangles = [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]]
        dart = {"id": "dart", "nodeId": "surface", "anchors": [{"vertex": 0}, {"vertex": 4}], "kind": "dart"}
        for height, kind, size in ((20, "developable", 5), (0, "flat", 4)):
            with self.subTest(height=height):
                source = model([[0, 0, 0], [60, 0, 0], [60, 60, 0], [0, 60, 0], [30, 30, height]], triangles)
                result = calculate(source, {"panels": [panel()], "seams": [dart]})
                self.assertTrue(result["complete"], result["warnings"])
                pattern, = result["panels"]
                self.assertEqual(pattern["kind"], kind)
                self.assertEqual(len(pattern["loops"][0]), size + (1 if height else 0))
                self.assert_isometric(source, pattern)
                self.assertTrue(any(entity["reference"] for entity in build_entities(result, 1, 0) if entity["layer"] == "Вытачка"))
                if not height:
                    self.assertEqual(result["fitStitches"], [])
                    continue
                stitch, = result["fitStitches"]
                self.assertEqual((stitch["kind"], stitch["seamId"]), ("dart", "dart"))
                first, second = (stitch[side]["vertices"] for side in "ab")
                self.assertNotEqual(first[0], second[0])
                self.assertEqual(first[-1], second[-1])
                self.assertEqual([pattern["sourceVertices"][vertex] for vertex in first],
                                 [pattern["sourceVertices"][vertex] for vertex in second])
                endpoints = [{"panelId": "panel", "edgeId": next(edge["id"] for edge in pattern["edges"]
                                                                    if set(edge["vertices"]) == set(bank))}
                             for bank in (first, second)]
                explicit = {"id": "selected-dart", "a": endpoints[0], "b": endpoints[1]}
                revised = calculate(source, {"panels": [panel()], "seams": [dart], "pairs": [explicit]})
                self.assertTrue(revised["complete"], revised["diagnostics"])
                self.assertEqual([item["id"] for item in revised["fitStitches"]], ["selected-dart"])

    def test_straight_seam_crosses_multiple_planar_facets(self):
        source = grid(5, lambda x, y: [25 * x, 25 * y, 0])
        line = seam("across", [{"triangle": 9, "barycentric": [0.6, 0, 0.4]},
                               {"triangle": 14, "barycentric": [0, 0.6, 0.4]}])
        result = calculate(source, {"panels": [panel("a", seed=0), panel("b", seed=30)], "seams": [line]})
        self.assertTrue(result["complete"], result["warnings"])
        points = result["seams"][0]["points"]
        self.assertGreater(len(points), 4)
        self.assertTrue(all(abs(point[1] - 35) < 1e-6 for point in points))

    def test_contour_preview_inferring_inside_and_retaining_explicit_outside(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0]], [[0, 1, 2]])
        target = "fabric:draft"
        state = {"fabricLayers": [{"id": "draft", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}]}
        anchors = [{"triangle": 0, "barycentric": weights} for weights in ([.92,.04,.04], [.04,.92,.04], [.04,.04,.92])]
        original = deepcopy((source, state, anchors))
        with patch("upholstery._flatten", side_effect=AssertionError("preview must not unfold")):
            inside = preview_contour(source, state, target, anchors)
            self.assertEqual(set(inside), {"vertices", "triangles", "loops", "seed"})
            self.assertEqual(len(inside["loops"]), 1)
            self.assertAlmostEqual(Polygon([inside["vertices"][v] for v in inside["loops"][0]]).area, 3872)
            outside_seed = {"triangle": 0, "barycentric": [.98,.01,.01]}
            outside = preview_contour(source, state, target, anchors, outside_seed)
            self.assertEqual(outside["seed"], outside_seed)
            self.assertEqual(len(outside["loops"]), 2)
            with self.assertRaisesRegex(UpholsteryError, "на контуре"):
                preview_contour(source, state, target, anchors, anchors[0])
        self.assertEqual((source, state, anchors), original)
        saved = {**state, "seams": [seam("outline", anchors, nodeId=target, closed=True, placementFor="panel")],
                 "panels": [panel(nodeId=target, seed=inside["seed"]["triangle"], seedBarycentric=inside["seed"]["barycentric"])]}
        actual, = calculate(source, saved)["placements"]
        for key in ("vertices", "triangles", "loops"):
            self.assertEqual(inside[key], actual[key])

    def test_polyline_contour_routes_across_fold_between_tessellated_faces(self):
        source = model([[-100, 0, 0], [0, 0, 0], [0, 100, 0], [-100, 100, 0]], [[0, 1, 2], [0, 2, 3]])
        source["nodes"].append({"id": "fold", "vertices": [[0, 0, 0], [0, 0, 100], [0, 100, 100], [0, 100, 0]],
                                "triangles": [[0, 1, 2], [0, 2, 3]], "faces": ["face", "face"]})
        target = "fabric:cloth"
        state = {"fabricLayers": [{"id": "cloth", "joinFolded": True,
                                  "surfaces": [{"nodeId": node["id"], "faces": ["face"]} for node in source["nodes"]]}]}
        anchors = [{"triangle": i, "barycentric": weights} for i, weights in
                   enumerate(([.30, .45, .25], [.25, .70, .05], [.70, .05, .25], [.25, .30, .45]))]
        anchors = [anchors[i] for i in (0, 2, 3, 1)]
        with patch("upholstery._Surface.triangulate", side_effect=AssertionError("open routing must not triangulate")):
            paths = route_contour(source, state, target, anchors)["paths"]
        self.assertEqual(len(paths), 3)
        for leg, y in ((paths[0], 25), (paths[2], 75)):
            self.assertTrue(all(math.isclose(point[1], y) for point in leg))
            self.assertTrue(any(math.dist(point, [0, y, 0]) < 1e-6 for point in leg))

        preview = preview_contour(source, state, target, anchors, surface_route=True)
        self.assertEqual(preview["outline"][0], preview["outline"][-1])
        self.assertTrue(all(25 - 1e-6 <= point[1] <= 75 + 1e-6 for point in preview["outline"]))
        legacy = preview_contour(source, state, target, anchors)
        legacy_outline = [legacy["vertices"][v] for v in legacy["loops"][0]]
        self.assertEqual(min(point[1] for point in legacy_outline), 0)
        self.assertEqual(max(point[1] for point in legacy_outline), 100)
        seed = preview["seed"]
        state["panels"] = [panel(nodeId=target, seed=seed["triangle"], seedBarycentric=seed["barycentric"])]
        state["seams"] = [seam("outline", anchors, nodeId=target, closed=True, placementFor="panel", surfaceRoute=True)]
        actual = calculate(source, state)
        self.assertEqual(actual["diagnostics"], [])
        self.assertEqual(actual["seams"][0]["points"], preview["outline"])
        pattern, = actual["panels"]
        self.assertEqual(pattern["kind"], "developable")
        self.assertAlmostEqual(Polygon([pattern["vertices"][v] for v in pattern["loops"][0]]).area, 3000)

    def test_open_contour_rejects_disconnected_surfaces(self):
        state = {"fabricLayers": [{"id": "cloth", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}]}
        anchors = [{"triangle": i, "barycentric": [.2, .3, .5]} for i in range(2)]
        for other in ([[0, 0, 10], [100, 0, 10], [0, 100, 10]], [[0, 0, 0], [-100, 0, 0], [0, -100, 0]]):
            source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0], *other], [[0, 1, 2], [3, 4, 5]])
            with self.assertRaisesRegex(UpholsteryError, "несвязанным"):
                route_contour(source, state, "fabric:cloth", anchors)

    def test_planar_cad_edit_projects_back_from_rotated_drawing(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 80, 60]], [[0, 1, 2]])
        node_id = "fabric:cloth"
        anchors = [{"triangle": 0, "barycentric": w} for w in ([.8, .1, .1], [.2, .7, .1], [.2, .1, .7])]
        state = {"fabricLayers": [{"id": "cloth", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "panels": [panel("a", nodeId=node_id, seed=0, seedBarycentric=[.4, .3, .3])],
                 "seams": [seam("line", anchors, nodeId=node_id, closed=True, placementFor="a")]}
        before = deepcopy(state)
        solved = calculate(source, state)
        manifest = {"a": {"id": "a"}}
        sync = cad_sync_geometry("model", solved, state, manifest)
        self.assertEqual(sync, cad_sync_geometry("model", solved, state, manifest))
        line = next(shape for shape in sync["entities"] if shape.get("reference") and shape["closed"])
        previous = [[120 - y, x - 40] for x, y in line["points"]]
        edited = deepcopy(previous)
        edited[1] = [(3 * a + b) / 4 for a, b in zip(previous[1], previous[2])]
        projected = project_cad_contour(source, state, "a", previous, edited)
        self.assertTrue(projected["calculated"]["complete"])
        self.assertNotEqual(projected["anchors"], anchors)
        piece, = projected["calculated"]["panels"]
        self.assertAlmostEqual(Polygon([piece["vertices"][i] for i in piece["loops"][0]]).area, Polygon(edited).area)
        with self.assertRaisesRegex(UpholsteryError, "за границы"):
            project_cad_contour(source, state, "a", previous, [[1000, 1000], *edited[1:]])
        self.assertEqual(state, before)

    def test_contour_preview_adjacent_to_existing_patch_and_moved_handle(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0]], [[0, 1, 2]])
        target = "fabric:draft"
        def ring(points):
            return [{"triangle": 0, "barycentric": [1-(x+y)/100, x/100, y/100]} for x,y in points]
        left = ring([(10,10), (30,10), (30,40), (10,40)])
        right = ring([(30,10), (50,10), (50,40), (30,40)])
        saved = {"fabricLayers": [{"id": "draft", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "seams": [seam("left", left, nodeId=target, closed=True)]}
        for x, expected in ((50,600), (55,675)):
            adjusted = [*right[:2], ring([(x,40)])[0], right[3]]
            preview = preview_contour(source, saved, target, adjusted)
            self.assertAlmostEqual(Polygon([preview["vertices"][v] for v in preview["loops"][0]]).area, expected)
            self.assertGreaterEqual(min(point[0] for point in preview["vertices"]), 30 - 1e-6)
        for invalid in (right[:2], [right[0], right[0], right[2]], [right[i] for i in (0,2,1,3)]):
            with self.assertRaises(UpholsteryError):
                preview_contour(source, saved, target, invalid)

    def test_contour_preview_on_closed_shell_and_nonseparating_torus(self):
        source = model([[0,0,0], [40,0,0], [0,40,0], [0,0,40]], [[0,2,1], [0,1,3], [1,2,3], [2,0,3]])
        target = "fabric:draft"
        state = {"fabricLayers": [{"id": "draft", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}]}
        anchors = [{"triangle": 0, "barycentric": weights} for weights in ([1,0,0], [0,1,0], [0,0,1])]
        auto = preview_contour(source, state, target, anchors)
        self.assertEqual(len(auto["triangles"]), 1)
        self.assertTrue(all(v[2] == 0 for v in auto["vertices"]))
        side = {"triangle": 1, "barycentric": [.2,.2,.6]}
        curved = preview_contour(source, state, target, anchors, side)
        self.assertEqual(curved["seed"], side)
        self.assertEqual(len(curved["triangles"]), 3)
        self.assertEqual(max(v[2] for v in curved["vertices"]), 40)

        points, faces = [], []
        major, minor = 8, 6
        def index(i,j):
            return (i % major) * minor + j % minor
        for i in range(major):
            for j in range(minor):
                u, v = math.tau*i/major, math.tau*j/minor
                radius = 30 + 8*math.cos(v)
                points.append([radius*math.cos(u), radius*math.sin(u), 8*math.sin(v)])
                faces.extend([[index(i,j),index(i+1,j),index(i+1,j+1)], [index(i,j),index(i+1,j+1),index(i,j+1)]])
        loop = [{"triangle": 2*i*minor, "barycentric": [1,0,0]} for i in range(major)]
        with self.assertRaisesRegex(UpholsteryError, "не отделяет"):
            preview_contour(model(points, faces), state, target, loop)

    def test_closed_seam_inside_one_facet_produces_insert_and_hole(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0]], [[0, 1, 2]])
        anchors = [{"triangle": 0, "barycentric": bary} for bary in ([.8, .1, .1], [.5, .4, .1], [.5, .1, .4])]
        outer = panel("outer", seed=0, seedBarycentric=[.1, .7, .2])
        inner = panel("inner", seed=0, seedBarycentric=[.7, .16, .14])
        pair = {"id": "join", "a": {"panelId": "outer", "seamId": "insert"},
                "b": {"panelId": "inner", "seamId": "insert"}, "marks": [.13]}
        result = calculate(source, {"panels": [outer, inner], "seams": [seam("insert", anchors, closed=True)], "pairs": [pair]})
        self.assertTrue(result["complete"], result["warnings"])
        self.assertEqual([len(pattern["loops"]) for pattern in result["panels"]], [2, 1])
        entities = build_entities(result, 3, 1)
        notch = next(entity for entity in entities if entity.get("notch") and entity["layer"] == "Срез отверстия")
        hole = Polygon(next(entity["points"] for entity in entities if entity["id"] == notch["parentId"]))
        self.assertFalse(hole.covers(Point(notch["end"])))

    def test_remaining_cover_panels_follow_closed_seam_inside_one_source_triangle(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0]], [[0, 1, 2]])
        target = "fabric:cover"
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}
        anchors = [{"triangle": 0, "barycentric": bary} for bary in ([.8, .1, .1], [.5, .4, .1], [.5, .1, .4])]
        state = {"fabricLayers": [fabric], "seams": [seam("cut", anchors, closed=True, nodeId=target)]}
        original = deepcopy((source, state))

        proposed = remaining_panels(source, state, target)
        self.assertEqual(proposed, remaining_panels(source, state, target))
        self.assertEqual(len(proposed), 2)
        self.assertTrue(all(set(part) == {"faces", "seed", "seedBarycentric"} and part["seed"] == 0 for part in proposed))
        self.assertEqual(original, (source, state))
        state["panels"] = [panel("outside", nodeId=target, **proposed[0])]
        self.assertEqual(remaining_panels(source, state, target), proposed[1:])
        state["panels"].append(panel("insert", nodeId=target, **proposed[1]))
        self.assertIsNone(check_cover_panel(source, state, target, "insert"))
        self.assertEqual(remaining_panels(source, state, target), [])
        result = calculate(source, state)
        self.assertTrue(result["complete"], result["diagnostics"])
        areas = []
        for item in result["panels"]:
            shell, *holes = [[item["vertices"][i] for i in loop] for loop in item["loops"]]
            areas.append(Polygon(shell, holes).area)
        self.assertEqual([round(area) for area in sorted(areas)], [450, 4550])
        self.assertEqual(sum(entity["layer"] == "Срез" for entity in build_entities(result, 1, 0, state=state)), 2)
        joint, = result["joins"]
        self.assertEqual(joint["points"][0], joint["points"][-1])
        joined = calculate(source, {**state, "joins": [{"id": joint["id"], "marks": [.16]}]})
        notch_layers = {item["layer"] for item in build_entities(joined, 3, 1, state=state) if item.get("notch")}
        self.assertEqual(notch_layers, {"Срез", "Срез отверстия"})
        state["panels"].append(panel("duplicate", nodeId=target, **proposed[0]))
        with self.assertRaisesRegex(UpholsteryError, "пересекается"):
            check_cover_panel(source, state, target, "duplicate")
        with self.assertRaisesRegex(UpholsteryError, "пересекается"):
            remaining_panels(source, state, target)

    def test_selected_fabric_welds_split_boundary_across_fold(self):
        source = model([[0, 0, 0], [80, 0, 0], [80, 100, 0], [0, 100, 0]], [[0, 1, 2], [0, 2, 3]])
        source["nodes"].append({
            "id": "fold", "vertices": [[80, 0, 0], [80, 0, 120], [80, 100, 120], [80, 100, 0],
                                        [80, 35, 0], [80, 35, 120]],
            "triangles": [[0, 1, 5], [0, 5, 4], [4, 5, 2], [4, 2, 3]],
            "faces": ["left", "left", "right", "right"],
        })
        selected = ["0:face", "1:left", "1:right"]
        layer = {"id": "cover", "surfaces": [{"nodeId": node["id"], "faces": list(dict.fromkeys(node["faces"]))}
                                             for node in source["nodes"]]}
        state = {"fabricLayers": [{**layer, "joinCoplanar": True}]}
        self.assertEqual(len(selected_panel_regions(source, state, "fabric:cover", selected)), 2)
        state["fabricLayers"][0]["joinFolded"] = True
        region, = selected_panel_regions(source, state, "fabric:cover", selected)
        state["panels"] = [panel("cover", nodeId="fabric:cover", **region)]
        result = calculate(source, state)
        self.assertEqual(result["diagnostics"], [])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "developable")
        loop, = pattern["loops"]
        self.assertAlmostEqual(Polygon([pattern["vertices"][i] for i in loop]).area, 20000)
        self.assertEqual(result["seams"], [])

    def test_new_cloth_cannot_overlap_existing_different_layer(self):
        def patch(identifier, nodes):
            return {"id": identifier, "nodeId": "fabric:" + identifier, "vertices": nodes, "triangles": [[0, 1, 2]]}

        first = patch("existing", [[0, 0, 0], [0, 20, 0], [0, 0, 20]])
        touching = patch("new", [[0, 0, 0], [0, -20, 0], [0, 0, 20]])
        offset = patch("new", [[2, 0, 0], [2, 20, 0], [2, 0, 20]])
        overlap = patch("new", [[0, 1, 1], [0, 5, 1], [0, 1, 5]])
        check_panel_placement({"placements": [first, touching]}, ["new"])
        check_panel_placement({"placements": [first, offset]}, ["new"])
        with self.assertRaisesRegex(UpholsteryError, "уже лежит ткань"):
            check_panel_placement({"placements": [first, overlap]}, ["new"])

    def test_selected_fabric_batch_accepts_five_regions_and_rejects_overlap(self):
        def assign(corners):
            source = {"nodes": []}
            for index, (x, y) in enumerate(corners):
                node = grid(2, lambda col, row: [x + col * 20, y + row * 20, 0])["nodes"][0]
                source["nodes"].append({**node, "id": str(index)})
            target = "fabric:cover"
            layer = {"id": "cover", "joinFolded": True,
                     "surfaces": [{"nodeId": node["id"], "faces": ["face"]} for node in source["nodes"]]}
            state = {"fabricLayers": [layer]}
            selected = [f"{index}:face" for index in range(len(corners))]
            regions = selected_panel_regions(source, state, target, selected)
            state["panels"] = [panel(str(index), nodeId=target, **region) for index, region in enumerate(regions)]
            result = calculate(source, state)
            self.assertEqual(result["diagnostics"], [])
            self.assertEqual(len(result["placements"]), len(corners))
            self.assertEqual(len(result["panels"]), len(corners))
            check_panel_placement(result, [item["id"] for item in state["panels"]])

        assign([(0, 0), (20, 20), (65, 0), (100, 0), (135, 0)])
        with self.assertRaisesRegex(UpholsteryError, "Выбранные участки ткани пересекаются"):
            assign([(0, 0), (8, 6)])

    def test_new_cloth_batch_accepts_shared_edge_and_fold(self):
        def patch(identifier, vertices):
            return {"id": identifier, "nodeId": "fabric:cover", "vertices": vertices, "triangles": [[0, 1, 2]]}

        flat = patch("flat", [[0, 0, 0], [20, 0, 0], [0, 20, 0]])
        touching = patch("touching", [[0, 0, 0], [20, 0, 0], [0, -20, 0]])
        folded = patch("folded", [[0, 0, 0], [20, 0, 0], [0, 18, 12]])
        for contact in [touching, folded]:
            check_panel_placement({"placements": [flat, contact]}, [flat["id"], contact["id"]])

    def test_selected_fabric_joins_t_junction_but_preserves_islands_and_seams(self):
        source = model([[0, 0, 0], [80, 0, 0], [80, 100, 0], [0, 100, 0]], [[0, 1, 2], [0, 2, 3]])
        source["nodes"].extend([
            {"id": "right", "vertices": [[80, 0, 0], [200, 0, 0], [200, 100, 0], [80, 100, 0], [80, 35, 0], [200, 35, 0]],
             "triangles": [[0, 1, 5], [0, 5, 4], [4, 5, 2], [4, 2, 3]], "faces": ["low", "low", "high", "high"]},
            {"id": "corner", "vertices": [[200, 100, 0], [230, 100, 0], [230, 120, 0]],
             "triangles": [[0, 1, 2]], "faces": ["tip"]},
            {"id": "other-plane", "vertices": [[80, 0, 1], [200, 0, 1], [200, 100, 1], [80, 100, 1]],
             "triangles": [[0, 1, 2], [0, 2, 3]], "faces": ["other", "other"]},
        ])
        node_id = "fabric:cover"
        layer = {"id": "cover", "joinCoplanar": True, "surfaces": [
            {"nodeId": n["id"], "faces": list(dict.fromkeys(n["faces"]))} for n in source["nodes"]
        ]}
        state = {"fabricLayers": [layer]}
        surface = ["0:face", "1:low", "1:high"]
        regions = selected_panel_regions(source, state, node_id, [*surface, "2:tip", "3:other"])
        self.assertEqual([region["faces"] for region in regions], [surface, ["2:tip"], ["3:other"]])
        legacy = {"fabricLayers": [{**layer, "joinCoplanar": False}]}
        self.assertEqual(len(selected_panel_regions(source, legacy, node_id, surface)), 2)
        state["panels"] = [panel("cover", nodeId=node_id, **regions[0])]
        result = calculate(source, state)
        self.assertEqual(result["diagnostics"], [])
        pattern, = result["panels"]
        self.assertEqual(pattern["kind"], "flat")
        self.assertEqual(len(pattern["loops"]), 1)
        self.assertAlmostEqual(Polygon([pattern["vertices"][i] for i in pattern["loops"][0]]).area, 20000)
        self.assertEqual(len(result["placements"][0]["triangles"]), 7)

        line = seam("divider", [{"triangle": 1, "barycentric": [0.75, 0, 0.25]},
                                {"triangle": 2, "barycentric": [0, 10 / 35, 25 / 35]}], nodeId=node_id)
        divided = {"fabricLayers": [layer], "seams": [line]}
        pieces = selected_panel_regions(source, divided, node_id, surface)
        self.assertEqual(len(pieces), 2)
        divided["panels"] = [panel(str(i), nodeId=node_id, **piece) for i, piece in enumerate(pieces)]
        split = calculate(source, divided)
        self.assertEqual(split["diagnostics"], [])
        self.assertEqual(len(split["panels"]), 2)

    def test_explicit_shoulder_seam_splits_rounded_cover_without_losing_surface(self):
        source = grid(5, lambda x, y: [20 * x, 20 * y, .9 * (x - 2) * (y - 2)])
        state = {"fabricLayers": [{"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "panels": [panel("cover", nodeId="fabric:cover", number="Д07", seed=0, fabricName="Лён", color="#677788")],
                 "nextPanelNumber": 9}
        original = calculate(source, state)
        self.assertEqual([part["kind"] for part in original["panels"]], ["rounded"])
        anchors = []
        for vertex in range(2, 25, 5):
            index, corners = next((i, t) for i, t in enumerate(source["nodes"][0]["triangles"]) if vertex in t)
            anchors.append({"triangle": index, "barycentric": [int(v == vertex) for v in corners]})
        unchanged = deepcopy(state)
        edited = edit_cover_cut(source, state, "cover", anchors, "seam")
        self.assertEqual(state, unchanged)
        self.assertEqual(edited["panelIds"][0], "cover")
        parent, daughter = edited["state"]["panels"]
        self.assertEqual((parent["number"], daughter["number"], edited["state"]["nextPanelNumber"]), ("Д07", "Д09", 10))
        self.assertEqual(daughter["fabricName"], "Лён")
        flat = edited["calculated"]
        self.assertTrue(flat["complete"], flat["diagnostics"])
        lead, follow = flat["panels"]
        self.assertTrue(set(lead["sourceTriangles"]).isdisjoint(follow["sourceTriangles"]))
        self.assertEqual(set(lead["sourceTriangles"]) | set(follow["sourceTriangles"]), set(original["panels"][0]["sourceTriangles"]))
        cut, = flat["seams"]
        self.assertEqual((cut["id"], cut["kind"], cut["coverCut"]), (edited["cutId"], "seam", True))
        self.assertEqual(cut["panelIds"], edited["panelIds"])
        self.assertEqual(len(cut["banks"]), 2)
        self.assertEqual(len(flat["joins"]), 1)

    def test_slash_exports_one_open_cut_starting_at_allowance_boundary(self):
        source = model([[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0]], [[0, 1, 2], [0, 2, 3]])
        state = {"fabricLayers": [{"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "panels": [panel("cover", nodeId="fabric:cover")], "autoNotches": True}
        start, end = ({"triangle": 0, "barycentric": weights} for weights in ([.5, .5, 0], [.5, .2, .3]))
        unchanged = deepcopy(state)
        edited = edit_cover_cut(source, state, "cover", [start, end], "slash")
        self.assertEqual(state, unchanged)
        self.assertEqual(edited["panelIds"], ["cover"])
        result = edited["calculated"]
        piece, = result["panels"]
        self.assertFalse(piece["marks"])
        self.assertFalse(result["fitStitches"])
        cut, = result["seams"]
        self.assertEqual((cut["kind"], cut["panelIds"], cut["banks"]), ("slash", ["cover"], []))
        paths = prepare_design(build_entities(result, 10, 0, state=edited["state"]))["paths"]
        opening, = [path for path in paths if not path["closed"]]
        outline, = [path for path in paths if path["closed"]]
        self.assertEqual(opening["layer"], "Рассечка")
        self.assertGreater(paths.index(outline), paths.index(opening))
        outline = Polygon(outline["points"])
        self.assertAlmostEqual(outline.exterior.distance(Point(opening["points"][0])), 0)
        self.assertTrue(outline.contains(Point(opening["points"][-1])))
        self.assertAlmostEqual(LineString(opening["points"]).length, 40)
        short_end = {"triangle": 0, "barycentric": [.5, .45, .05]}
        short = edit_cover_cut(source, state, "cover", [start, short_end], "slash")
        paths = prepare_design(build_entities(short["calculated"], 0, 0, state=short["state"]))["paths"]
        short_cut, = [path for path in paths if not path["closed"]]
        self.assertFalse(short_cut["notch"])
        self.assertAlmostEqual(LineString(short_cut["points"]).length, 5)
        with self.assertRaisesRegex(UpholsteryError, "Начните рассечку"):
            edit_cover_cut(source, state, "cover", [end, start], "slash")
        self.assertEqual(state, unchanged)

    def test_cut_refuses_to_orphan_existing_edge_notch(self):
        source = model([[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0]], [[0, 1, 2], [0, 2, 3]])
        state = {"fabricLayers": [{"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "panels": [panel("cover", nodeId="fabric:cover")]}
        old = calculate(source, state)["panels"][0]
        edge = next(edge for edge in old["edges"] if all(point[1] == 0 for point in edge["sourcePoints"]))
        state["panels"][0]["notches"] = [{"id": "n", "edgeId": edge["id"], "position": .25}]
        unchanged = deepcopy(state)
        anchors = [{"triangle": 0, "barycentric": [.5, .5, 0]}, {"triangle": 1, "barycentric": [0, .5, .5]}]
        with self.assertRaisesRegex(UpholsteryError, "кромку с насечкой"):
            edit_cover_cut(source, state, "cover", anchors, "seam")
        self.assertEqual(state, unchanged)

    def test_remaining_cover_panels_respect_manual_faces_across_welded_sources(self):
        left = model([[0, 0, 0], [50, 0, 0], [0, 50, 0]], [[0, 1, 2]])["nodes"][0]
        right = model([[50, 0, 0], [50, 50, 0], [0, 50, 0], [100, 50, 0]], [[0, 1, 2], [0, 3, 1]])["nodes"][0]
        right.update(id="next", faces=["middle", "face"])
        source = {"nodes": [left, right]}
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}, {"nodeId": "next", "faces": ["middle", "face"]}]}
        target = "fabric:cover"
        state = {"fabricLayers": [fabric], "panels": [panel("manual", nodeId=target, faces=["1:middle"]), panel("other")]}
        original = deepcopy(state)
        parts = remaining_panels(source, state, target)
        self.assertEqual([part["faces"] for part in parts], [["0:face"], ["1:face"]])
        self.assertEqual(state, original)
        state["panels"].extend(panel(str(i), nodeId=target, **part) for i, part in enumerate(parts))
        self.assertEqual(remaining_panels(source, state, target), [])
        self.assertIsNone(check_cover_panel(source, state, target, "manual"))
        with self.assertRaisesRegex(UpholsteryError, "этого чехла"):
            check_cover_panel(source, state, target, "other")
        state["panels"] = [panel(str(i)) for i in range(120)]
        with self.assertRaisesRegex(UpholsteryError, "120"):
            remaining_panels(source, state, target)

    def test_removing_cover_seam_blocks_only_overlapping_panels_in_transfer(self):
        source = model([[0, 0, 0], [100, 0, 0], [0, 100, 0], [120, 0, 0], [200, 0, 0], [120, 80, 0]],
                       [[0, 1, 2], [3, 4, 5]])
        target = "fabric:cover"
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}
        anchors = [{"triangle": 0, "barycentric": bary} for bary in ([.8, .1, .1], [.5, .4, .1], [.5, .1, .4])]
        state = {"fabricLayers": [fabric], "seams": [seam("cut", anchors, closed=True, nodeId=target)]}
        parts = remaining_panels(source, state, target)
        self.assertEqual([part["seed"] for part in parts], [0, 0, 1])
        state["panels"] = [panel(name, nodeId=target, **part) for name, part in zip(["outer", "insert", "clear"], parts)]
        self.assertTrue(calculate(source, state)["complete"])
        state["seams"] = []
        result = calculate(source, state)
        self.assertFalse(result["complete"])
        overlaps = [issue for issue in result["diagnostics"] if issue["code"] == "cover_panel_overlap"]
        self.assertEqual([(issue["panelId"], issue["severity"]) for issue in overlaps], [("outer", "error"), ("insert", "error")])
        reports = check_transfer(result, state, 3, 1)["panels"]
        self.assertEqual([item["ready"] for item in reports], [False, False, True])
        for selection in (None, ["outer"], ["insert"], ["clear", "outer"]):
            with self.assertRaisesRegex(UpholsteryError, "участок чехла"):
                build_entities(result, 3, 1, state=state, panel_ids=selection)
        entities = build_entities(result, 3, 1, state=state, panel_ids=["clear"])
        self.assertEqual(sum(item["layer"] == "Срез" for item in entities), 1)

    def test_cover_with_closed_uncut_remainder_and_unrepresentable_masks_reports_adjustment(self):
        source = model([[0, 0, 0], [50, 0, 0], [0, 50, 0], [0, 0, 50]], [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]])
        source["nodes"][0]["faces"] = ["top", "side", "side", "side"]
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["top", "side"]}]}
        target = "fabric:cover"
        state = {"fabricLayers": [fabric]}
        with self.assertRaisesRegex(UpholsteryError, "замкнутой поверхности.*шов"):
            remaining_panels(source, state, target)
        state["panels"] = [panel("cap", nodeId=target, faces=["0:top"])]
        self.assertIsNone(check_cover_panel(source, state, target, "cap"))
        self.assertEqual(remaining_panels(source, state, target)[0]["faces"], ["0:side"])

        source = model([[0, 0, 0], [50, 0, 0], [0, 50, 0], [50, 50, 0], [100, 50, 0]], [[0, 1, 2], [1, 3, 2], [1, 4, 3]])
        source["nodes"][0]["faces"] = ["side", "top", "side"]
        state["panels"] = [panel("end", nodeId=target, faces=["0:side"], seed=0)]
        with self.assertRaisesRegex(UpholsteryError, "Добавьте разделительный шов"):
            remaining_panels(source, state, target)

    def test_manual_cover_check_stays_within_selected_faces(self):
        source = model([[0, 0, 0], [40, 0, 0], [0, 40, 0], [0, -40, 0], [0, 0, 40]],
                       [[0, 1, 2], [1, 0, 3], [1, 0, 4]])
        source["nodes"][0]["faces"] = ["a", "b", "c"]
        target = "fabric:cover"
        state = {"fabricLayers": [{"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["a", "b", "c"]}]}],
                 "panels": [panel("a", nodeId=target, faces=["0:a"]), panel("b", nodeId=target, faces=["0:b"])]}
        self.assertIsNone(check_cover_panel(source, state, target, "b"))
        with self.assertRaisesRegex(UpholsteryError, "более двух"):
            remaining_panels(source, state, target)

    def test_face_selected_parts_join_without_authored_seams_and_export_matched_notches(self):
        source = grid(3, lambda x, y: [40*x, [0, 20, 80][y], 0])
        node = source["nodes"][0]
        node["faces"] = ["left" if i % 4 < 2 else "right" for i in range(8)]
        target = "fabric:cover"
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["left", "right"]}]}
        state = {"fabricLayers": [fabric], "panels": [panel(k, nodeId=target, faces=["0:" + k]) for k in ("left", "right")]}
        baseline = calculate(source, state)
        joint, = baseline["joins"]
        self.assertEqual((joint["nodeId"], joint["panelIds"]), (target, ["left", "right"]))
        self.assertEqual(joint["points"], [[40, 0, 0], [40, 20, 0], [40, 80, 0]])
        state["joins"] = [{"id": joint["id"], "marks": [.5]}]
        state["panels"].reverse()
        state["panels"][0]["name"] = "Renamed"
        snapshot = deepcopy((source, state))
        result = calculate(source, state)
        self.assertTrue(result["complete"], result["diagnostics"])
        self.assertEqual((source, state), snapshot)
        self.assertEqual(result["joins"], [joint])
        for item in result["panels"]:
            mark, = item["marks"]
            for xyz, uv in zip(item["sourceVertices"], item["vertices"]):
                self.assertAlmostEqual(math.dist(uv, mark["point"]), math.dist(xyz, [40, 40, 0]))
        entities = build_entities(result, 4, 2, state=state)
        notches = [item for item in entities if item.get("notch")]
        self.assertEqual(len(notches), 2)
        self.assertEqual(len({item["parentId"] for item in notches}), 2)
        self.assertEqual([item["marks"] for item in calculate(source, {**state, "joins": [{"id": joint["id"]}]})["panels"]], [[], []])
        node["vertices"][4][0] += 3
        stale = calculate(source, state)
        self.assertFalse(stale["complete"])
        self.assertNotEqual(stale["joins"][0]["id"], joint["id"])
        self.assertEqual({issue["panelId"] for issue in stale["diagnostics"] if issue["code"] == "join_unavailable"},
                         {"left", "right"})
        self.assertFalse(check_transfer(stale, state, 4, 2, ["right"])["panels"][0]["ready"])
        self.assertTrue(all(not item["marks"] for item in stale["panels"]))

    def test_disconnected_common_boundaries_and_ambiguous_contacts(self):
        skip = {(1, 1), (2, 1), (1, 2), (2, 2)}
        source = grid(5, lambda x, y: [x*20, y*20, 0], skip)
        node = source["nodes"][0]
        node["faces"] = ["left" if node["vertices"][triangle[0]][0] < 40 else "right" for triangle in node["triangles"]]
        target = "fabric:cover"
        fabric = {"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["left", "right"]}]}
        state = {"fabricLayers": [fabric], "panels": [panel(k, nodeId=target, faces=["0:" + k]) for k in ("left", "right")]}
        candidates = calculate(source, state)["joins"]
        self.assertEqual(sorted(j["points"] for j in candidates), [[[40, 0, 0], [40, 20, 0]], [[40, 60, 0], [40, 80, 0]]])
        self.assertEqual(len({j["id"] for j in candidates}), 2)
        state["panels"].append(panel("duplicate", nodeId=target, faces=["0:left"]))
        self.assertFalse(calculate(source, state)["joins"])

        source = model([[0, 0, 0], [50, 0, 0], [0, 40, 0], [0, -40, 0], [0, 0, 40]],
                       [[0, 1, 2], [1, 0, 3], [1, 0, 4]])
        node = source["nodes"][0]
        node["faces"] = ["left", "right", "third"]
        fabric["surfaces"][0]["faces"].append("third")
        state["panels"].pop()
        self.assertFalse(calculate(source, state)["joins"])
        node["faceFlags"] = {"third": {"visible": False}}
        self.assertEqual(len(calculate(source, state)["joins"]), 1)
        node["vertices"][3] = [4, 20, 0]
        self.assertFalse(calculate(source, state)["joins"])

    def test_failed_closed_unfold_keeps_resolved_3d_placement(self):
        source = model([[0, 0, 0], [50, 0, 0], [0, 50, 0], [0, 0, 50]], [[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]])
        state = {"fabricLayers": [{"id": "cover", "surfaces": [{"nodeId": "surface", "faces": ["face"]}]}],
                 "panels": [panel("closed", nodeId="fabric:cover")]}
        result = calculate(source, state)
        self.assertFalse(result["complete"])
        self.assertEqual(result["panels"], [])
        placed, = result["placements"]
        self.assertEqual((placed["id"], placed["nodeId"], placed["loops"]), ("closed", "fabric:cover", []))
        self.assertEqual(len(placed["triangles"]), 4)
        self.assertEqual(sorted(placed["vertices"]), sorted(source["nodes"][0]["vertices"]))

    def test_overlapping_coplanar_mesh_and_ambiguous_cut_cannot_export(self):
        source = model([[0, 0, 0], [20, 0, 0], [0, 20, 0], [3, 3, 0]], [[0, 1, 2], [1, 0, 3]])
        result = calculate(source, {"panels": [panel()]})
        self.assertFalse(result["complete"])
        self.assertIn("перевёрнутые", result["warnings"][0])
        with self.assertRaises(UpholsteryError):
            build_entities(result, 0, 0)

        source = grid(2, lambda x, y: [60 * x, 40 * y, 0])
        result = calculate(source, {"panels": [panel()], "seams": [seam("split", [{"vertex": 0}, {"vertex": 3}])]})
        self.assertFalse(result["complete"])
        self.assertIn("точку внутри", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
