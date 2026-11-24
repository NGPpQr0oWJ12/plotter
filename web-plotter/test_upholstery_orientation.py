import math
import unittest

import numpy as np

from test_upholstery import grid, model, panel, seam
from upholstery import build_entities, calculate


def fan(points, start=0):
    count = len(points)
    center = np.mean(points, axis=0).tolist()
    order = [(start + index) % count for index in range(count)]
    return model([*points, center], [[count, a, (a + 1) % count] for a in order])


class UpholsteryOrientationTests(unittest.TestCase):
    def test_longest_straight_run_beats_longest_mesh_segment(self):
        perimeter = [[0, 0, 0], [45, 0, 0], [90, 0, 0], [135, 0, 0], [111, 55, 0], [56, 55, 0], [1, 55, 0]]
        source = fan(perimeter, start=4)
        state = {"panels": [panel()]}
        result = calculate(source, state)
        self.assertTrue(result["complete"], result["diagnostics"])
        pattern = result["panels"][0]
        flattened = {tuple(point): xy for point, xy in zip(pattern["sourceVertices"], pattern["vertices"])}
        a, b = flattened[tuple(perimeter[0])], flattened[tuple(perimeter[3])]
        self.assertAlmostEqual(a[1], b[1], places=10)
        self.assertAlmostEqual(math.dist(a, b), 135, places=10)
        uv = np.asarray(pattern["vertices"])
        p, q, r = np.moveaxis(uv[pattern["triangles"]], 1, 0)
        self.assertTrue(np.all((q - p)[:, 0] * (r - p)[:, 1] - (q - p)[:, 1] * (r - p)[:, 0] > 0))
        self.assertTrue(np.all(uv.min(axis=0) == 0))
        self.assertEqual(calculate(source, state)["panels"], result["panels"])
        outline = next(shape for shape in build_entities(result, 0, 0) if shape["layer"] == "Срез")
        min_y = min(point[1] for point in outline["points"])
        max_y = max(point[1] for point in outline["points"])
        edges = [[x for x, y in outline["points"] if math.isclose(y, side, abs_tol=1e-8)] for side in (min_y, max_y)]
        extents = [max(edge) - min(edge) for edge in edges]
        self.assertAlmostEqual(max(extents), 135)

    def test_dense_curved_outline_uses_major_axis(self):
        count = 96
        points = [[100 * math.cos(i * math.tau / count), 25 * math.sin(i * math.tau / count), 0] for i in range(count)]
        result = calculate(fan(points, start=11), {"panels": [panel()]})
        self.assertTrue(result["complete"], result["diagnostics"])
        pattern = result["panels"][0]
        flattened = {tuple(point): xy for point, xy in zip(pattern["sourceVertices"], pattern["vertices"])}
        a, b = flattened[tuple(points[0])], flattened[tuple(points[count // 2])]
        self.assertAlmostEqual(a[1], b[1], places=10)
        self.assertAlmostEqual(math.dist(a, b), 200, places=10)

    def test_grain_orientation_after_rounded_fit_preserves_pair_marks_and_shape(self):
        source = grid(5, lambda x, y: [25 * x, 25 * y, 4 * ((x - 2)**2 + (y - 2)**2)])
        flat = model([[0, 0, 0], [120, 0, 0], [120, 35, 0], [0, 35, 0]], [[0, 1, 2], [0, 2, 3]])["nodes"][0]
        flat["id"] = "flat"
        source["nodes"].append(flat)
        pair = {"id": "fit", "a": {"panelId": "curve", "seamId": "curve"},
                "b": {"panelId": "flat", "seamId": "flat", "reverse": True}, "marks": [.32, .7], "ease": [4, 0, -3]}
        state = {"panels": [panel("curve"), panel("flat", nodeId="flat")],
                 "seams": [seam("curve", [{"vertex": i} for i in range(5)]),
                           seam("flat", [{"vertex": 0}, {"vertex": 1}], nodeId="flat")], "pairs": [pair]}
        without_grain = calculate(source, state)
        with_grain = calculate(source, {**state, "panels": [panel("curve", grain=[0, 20]), state["panels"][1]]})
        self.assertTrue(with_grain["complete"], with_grain["diagnostics"])
        before, after = without_grain["panels"][0], with_grain["panels"][0]
        self.assertEqual(after["kind"], "rounded")
        start, end = after["grain"]
        self.assertAlmostEqual(start[1], end[1], places=10)
        self.assertGreater(end[0], start[0])
        self.assertEqual(after["sourceVertices"], before["sourceVertices"])
        for triangle in after["triangles"]:
            for index, vertex in enumerate(triangle):
                neighbor = triangle[index - 1]
                self.assertAlmostEqual(math.dist(before["vertices"][vertex], before["vertices"][neighbor]),
                                       math.dist(after["vertices"][vertex], after["vertices"][neighbor]), places=10)
        for interval_a, interval_b in zip(without_grain["pairs"][0]["intervals"], with_grain["pairs"][0]["intervals"]):
            for key in ("a", "b", "delta", "ease"):
                self.assertAlmostEqual(interval_a[key], interval_b[key], places=10)
        for earlier, final in zip(before["marks"], after["marks"]):
            self.assertEqual(earlier["label"], final["label"])
            self.assertAlmostEqual(math.dist(earlier["point"], before["guides"][0]["points"][0]),
                                   math.dist(final["point"], after["guides"][0]["points"][0]), places=10)
        self.assertEqual(len(after["strain"]), len(after["triangles"]))


if __name__ == "__main__":
    unittest.main()
