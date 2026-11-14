import math
import unittest

from designer_cad import CadDesignError, edit_design, validate_design


def line(id, start, end, layer="0", **properties):
    return {"id": id, "type": "line", "layer": layer, "start": start, "end": end, **properties}


def quick_trim(entities, stroke, tolerance=0.25):
    return edit_design(entities, "trim-quick", {"stroke": stroke, "tolerance": tolerance})


class QuickTrimTests(unittest.TestCase):
    def test_click_selects_nearest_bounded_part_without_selecting_cutters(self):
        target = line("target", [0, 0], [20, 0])
        left = line("left", [5, -3], [5, 3])
        right = line("right", [15, -3], [15, 3])
        after = quick_trim([target, left, right], [[11, 0.1]])
        self.assertTrue(after["changed"])
        first, last, remaining_left, remaining_right = after["entities"]
        self.assertEqual((first["start"], first["end"]), ([0, 0], [5, 0]))
        self.assertEqual((last["start"], last["end"]), ([15, 0], [20, 0]))
        self.assertEqual([remaining_left, remaining_right], [left, right])
        self.assertEqual(after["selectedIds"], [first["id"], last["id"]])

    def test_drag_sweeps_multiple_fragments_with_original_contours_as_boundaries(self):
        entities = [line("long", [0, 0], [30, 0]),
                    line("upper", [0, 10], [30, 10]),
                    *[line(f"edge{i}", [i, -5], [i, 15]) for i in (5, 15, 25)]]
        after = quick_trim(entities, [[10, -1], [10, 16], [20, 16], [20, -1]])
        self.assertTrue(after["changed"])
        for y in (0, 10):
            ends = [(entity["start"][0], entity["end"][0]) for entity in after["entities"]
                    if entity["start"][1] == entity["end"][1] == y]
            self.assertEqual(ends, [(0, 5), (25, 30)])
        self.assertEqual(after["entities"][4:], entities[2:])

    def test_no_touch_is_noop_and_one_tangent_does_not_partition_closed_loop(self):
        circle = {"id": "circle", "type": "circle", "center": [0, 0], "radius": 10}
        tangent = line("edge", [-20, 10], [20, 10])
        data = [circle, tangent]
        for stroke in ([[-15, -12], [-15, -8]], [[-19, 11]]):
            with self.subTest(stroke=stroke):
                after = quick_trim(data, stroke)
                self.assertFalse(after["changed"])
                self.assertEqual(after["entities"], validate_design(data))
        after = quick_trim(data, [[0, -10]])
        self.assertTrue(after["changed"])
        self.assertEqual(after["selectedIds"], [])
        self.assertEqual(after["entities"], [tangent])

    def test_whole_object_click_chooses_closest_and_erases_notches_and_points(self):
        parent = line("parent", [0, 0], [12, 0])
        notch = line("notch", [5, 0], [5, 2], notch=True, parentId="parent")
        point = {"id": "point", "type": "point", "point": [8, 2]}
        data = [parent, notch, point]
        self.assertEqual(quick_trim(data, [[5, 1]])["entities"], validate_design([parent, point]))
        self.assertEqual(quick_trim(data, [[8, 2]])["entities"], [parent, notch])
        self.assertEqual(quick_trim(data, [[0, 0]])["entities"], validate_design([point]))
        self.assertEqual(quick_trim([parent], [[0, 0]])["entities"], [])

    def test_drag_touches_start_end_center_between_samples_and_removes_parent_and_notch_once(self):
        a = line("start", [-2, 0], [-0.2, 0])
        b = line("end", [20.2, 0], [22, 0])
        circle = {"id": "circle", "type": "circle", "center": [10, 3], "radius": 2.8}
        notch = line("notch", [10, 0.2], [10, 1], notch=True, parentId="circle")
        point = {"id": "point", "type": "point", "point": [15, 0.2]}
        untouched = line("untouched", [5, -0.4], [15, -0.4])
        after = quick_trim([a, b, circle, notch, point, untouched], [[0, 0], [20, 0]])
        self.assertTrue(after["changed"])
        self.assertEqual(after["entities"], [untouched])
        self.assertEqual(after["selectedIds"], [])

    def test_exact_crossing_prefers_its_partition_over_grazing_an_adjacent_one(self):
        outline = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                   "closed": True}
        cutter = line("cut", [10, -5], [10, 15])
        after = quick_trim([outline, cutter], [[9.9, -1], [9.9, 1]])
        self.assertGreaterEqual(len(after["entities"]), 1)
        right = after["entities"][0]
        self.assertEqual(right["points"], [[10, 0], [20, 0], [20, 10], [10, 10]])

    def test_adjacent_near_miss_after_crossing_keeps_the_other_interval(self):
        target = line("target", [0, 0], [20, 0])
        cutter = line("cut", [10, -5], [10, 12])
        after = quick_trim([target, cutter], [[9, -0.1], [9, 0.1], [10.4, 0.12]])
        self.assertEqual(after["entities"][0]["start"], [10, 0])
        self.assertEqual(after["entities"][0]["end"], [20, 0])

    def test_distinct_near_miss_after_crossing_erases_both_intervals(self):
        target = line("target", [0, 0], [20, 0])
        cutter = line("cut", [10, -5], [10, 12])
        after = quick_trim([target, cutter], [[9, -0.1], [9, 0.1], [9, 6], [15, 6], [15, 0.1]])
        self.assertNotIn("target", {entity["id"] for entity in after["entities"]})
        self.assertTrue(all(entity["start"][0] == entity["end"][0] == 10 for entity in after["entities"]))

    def test_circle_stays_analytic_and_intersection_hits_exactly(self):
        circle = {"id": "circle", "type": "circle", "center": [0, 0], "radius": 10}
        cutter = line("cut", [3, -20], [3, 20])
        result = quick_trim([circle, cutter], [[10.15, 0]])
        arc, line_after = result["entities"]
        self.assertEqual(line_after, cutter)
        self.assertEqual(arc["type"], "arc")
        self.assertEqual(arc["center"], [0, 0])
        self.assertEqual(arc["radius"], 10)
        boundary = math.degrees(math.acos(3 / 10))
        self.assertAlmostEqual(arc["startAngle"], boundary)
        self.assertAlmostEqual(arc["sweep"], 360 - 2 * boundary)
        self.assertEqual(result["warnings"], [])

    def test_clockwise_arc_sign_is_preserved(self):
        arc = {"id": "arc", "type": "arc", "center": [0, 0], "radius": 10,
               "startAngle": 90, "sweep": -180}
        cutter = line("cut", [-5, 0], [20, 0])
        result = quick_trim([arc, cutter], [[7, 7]])
        self.assertEqual(result["entities"][0]["type"], "arc")
        self.assertAlmostEqual(result["entities"][0]["startAngle"], 0)
        self.assertAlmostEqual(result["entities"][0]["sweep"], -90)

    def test_closed_polyline_wrap_keeps_corners_and_only_surviving_notches(self):
        shape = {"id": "box", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True, "layer": "profile"}
        cutter = line("cut", [7, -5], [7, 15])
        removed_notch = line("n-removed", [0, 5], [2, 5], notch=True, parentId="box")
        kept_notch = line("n-kept", [20, 6], [18, 6], notch=True, parentId="box")
        result = quick_trim([shape, cutter, removed_notch, kept_notch], [[0, 3]])
        piece, actual_cutter, actual_notch = result["entities"]
        self.assertEqual(actual_cutter, cutter)
        self.assertEqual(actual_notch, {**kept_notch, "parentId": piece["id"]})
        self.assertEqual(piece["layer"], "profile")
        self.assertFalse(piece["closed"])
        self.assertEqual(piece["points"], [[7, 0], [20, 0], [20, 10], [7, 10]])

    def test_retained_bulge_uses_partial_sweep(self):
        shape = {"id": "bulge", "type": "polyline", "points": [[-10, 0], [10, 0], [20, 0]],
                 "bulges": [1, 0, 0], "closed": False}
        cutter = line("cut", [0, -15], [0, -5])
        result = quick_trim([shape, cutter], [[-7, -7]])
        piece = result["entities"][0]
        self.assertEqual(piece["type"], "polyline")
        self.assertAlmostEqual(piece["points"][0][0], 0)
        self.assertAlmostEqual(piece["points"][0][1], -10)
        self.assertAlmostEqual(piece["bulges"][0], math.tan(math.pi / 8))

    def test_limits_and_reference_assertions(self):
        with self.assertRaises(CadDesignError):
            quick_trim([], [[0, 0]] * 257)
        with self.assertRaises(CadDesignError):
            quick_trim([], [[0, 0]], 900000)
        target = line("target", [0, 0], [10, 0], reference=True)
        cutter = line("edge", [5, -3], [5, 3])
        result = quick_trim([target, cutter], [[0, 0]])
        self.assertTrue(result["entities"][0]["reference"])

    def test_join_across_layers_uses_first_selected(self):
        a = line("a", [0, 0], [5, 0], layer="A", reference=True)
        b = line("b", [5, 0], [10, 0], layer="B", reference=True)
        result = edit_design([a, b], "join", {"ids": ["b", "a"]})
        self.assertEqual(result["entities"][0]["layer"], "B")
        self.assertTrue(result["entities"][0]["reference"])
        self.assertTrue(result["entities"][0]["joined"])
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
