import math
import unittest

from designer_cad import CadDesignError, edit_design, prepare_design, validate_design


def line(id, start, end, layer="0", **properties):
    return {"id": id, "type": "line", "layer": layer, "start": start, "end": end, **properties}


def quick_trim(entities, stroke, tolerance=0.25):
    return edit_design(entities, "trim-quick", {"stroke": stroke, "tolerance": tolerance})


class QuickTrimTests(unittest.TestCase):
    def test_click_persists_intersection_segments_and_selects_hit_span_once(self):
        target = line("target", [0, 0], [20, 0], "cut", groupPath=["detail"])
        fixtures = [
            target, line("first", [5, -5], [5, 5], groupPath=["detail"]),
            line("crossing", [10, -5], [10, 5], groupPath=["detail"]),
            line("shared", [10, 0], [12, 5], groupPath=["detail"]),
            line("reference", [12, -5], [12, 5], groupPath=["detail"], reference=True),
            line("other-group", [15, -5], [15, 5], groupPath=["other"]),
            line("notch", [17, 0], [17, 4], notch=True, parentId="target"),
        ]
        response = edit_design(fixtures, "split-intersections", {"id": "target", "point": [15, 0]})
        head, middle, tail, *unchanged, mark = response["entities"]
        self.assertEqual((response["changed"], response["selectedIds"], response["selectedEdgeIndex"]),
                         (True, [tail["id"]], None))
        self.assertEqual([shape["start"] for shape in (head, middle, tail)], [[0, 0], [5, 0], [10, 0]])
        self.assertEqual([shape["end"] for shape in (head, middle, tail)], [[5, 0], [10, 0], [20, 0]])
        self.assertEqual((head["id"], middle["layer"], tail["groupPath"]), ("target", "cut", ["detail"]))
        self.assertEqual(unchanged, fixtures[1:-1])
        self.assertEqual(mark, {**fixtures[-1], "parentId": tail["id"]})
        second = edit_design(response["entities"], "split-intersections", {"id": tail["id"], "point": [15, 0]})
        self.assertEqual((second["changed"], second["selectedIds"], second["entities"]),
                         (False, [tail["id"]], response["entities"]))

    def test_intersection_nodes_preserve_closed_contour_and_untouched_bulge(self):
        contour = {"id": "outline", "type": "polyline", "layer": "cut", "closed": True,
                   "points": [[0, 0], [20, 0], [20, 20], [0, 20]], "bulges": [0, 0.2, 0, 0]}
        cutter = line("cutter", [10, -5], [10, 5])
        entities = [contour, cutter]
        response = edit_design(entities, "split-intersections", {"id": "outline", "edgeIndex": 0, "point": [16, 0]})
        result, unmodified_cutter = response["entities"]
        self.assertEqual((response["changed"], response["selectedIds"], response["selectedEdgeIndex"]),
                         (True, ["outline"], 1))
        self.assertEqual(result, {**contour, "points": [[0, 0], [10, 0], [20, 0], [20, 20], [0, 20]],
                                  "bulges": [0, 0, 0.2, 0, 0]})
        self.assertEqual(unmodified_cutter, cutter)
        self.assertTrue(any(path["closed"] for path in prepare_design(response["entities"])["paths"]))
        again = edit_design(response["entities"], "split-intersections",
                            {"id": "outline", "edgeIndex": 1, "point": [16, 0]})
        self.assertEqual((again["changed"], again["selectedEdgeIndex"]), (False, 1))

        for index, invalid in ((1, contour), (0, {**contour, "joined": True}), (True, contour)):
            with self.subTest(index=index, contour=invalid), self.assertRaises(CadDesignError):
                edit_design([invalid, cutter], "split-intersections", {"id": "outline", "edgeIndex": index, "point": [16, 0]})

    def test_deleting_a_whole_cutter_materializes_crossing_even_before_selection(self):
        target = line("target", [0, 0], [20, 0])
        cutter = line("cutter", [10, -5], [10, 5])
        notch = line("notch", [10, 3], [12, 3], notch=True, parentId="cutter")
        unrelated = line("reference", [0, 2], [20, 2], reference=True)
        response = edit_design([target, cutter, notch, unrelated], "split-intersections", {"removeIds": ["cutter"]})
        head, tail, untouched = response["entities"]
        self.assertEqual((response["changed"], response["selectedIds"], response["selectedEdgeIndex"]), (True, [], None))
        self.assertEqual(head, {**target, "end": [10, 0]})
        self.assertEqual((tail["start"], tail["end"], untouched), ([10, 0], [20, 0], unrelated))
        erased = edit_design(response["entities"], "split-intersections", {"removeIds": [head["id"]]})
        self.assertEqual(erased["entities"], [tail, unrelated])

    def test_deleting_one_polyline_span_opens_only_that_side_and_splits_its_neighbor(self):
        contour = {"id": "outline", "type": "polyline", "layer": "cut", "closed": True,
                   "points": [[0, 0], [20, 0], [20, 20], [0, 20]], "bulges": [0, 0.2, 0, 0]}
        cutter = line("cutter", [10, -5], [10, 5])
        removed = line("removed-mark", [3, 0], [3, 2], notch=True, parentId="outline")
        retained = line("retained-mark", [0, 15], [3, 15], notch=True, parentId="outline")
        response = edit_design([contour, cutter, removed, retained], "split-intersections",
                               {"id": "outline", "edgeIndex": 0, "removeSpan": True})
        shape, first, second, mark = response["entities"]
        self.assertEqual(shape, {**contour, "points": [[20, 0], [20, 20], [0, 20], [0, 0]],
                                 "closed": False, "bulges": [0.2, 0, 0, 0]})
        self.assertEqual((first["end"], second["start"], mark), ([10, 0], [10, 0], retained))
        self.assertFalse(any(path["closed"] for path in prepare_design(response["entities"])["paths"]))

    def test_eraser_opt_in_leaves_crossing_node_on_the_other_stroke(self):
        target = line("target", [0, 0], [20, 0])
        cutter = line("cutter", [10, -8], [10, 8])
        options = {"stroke": [[10, -4]], "tolerance": 0.25}
        legacy = edit_design([target, cutter], "trim-quick", options)
        self.assertEqual(legacy["entities"][0], target)
        response = edit_design([target, cutter], "trim-quick", {**options, "preserveIntersections": True})
        head, tail, kept = response["entities"]
        self.assertEqual(head, {**target, "end": [10, 0]})
        self.assertEqual((tail["start"], tail["end"], kept["start"], kept["end"]),
                         ([10, 0], [20, 0], [10, 0], [10, 8]))
        self.assertEqual(response["selectedIds"], [kept["id"]])

    def test_exclusive_eraser_filters_neither_hit_nor_partition_with_hidden_geometry(self):
        guide = line("guide", [0, 0], [20, 0], reference=True)
        cut = line("cut", [10, -10], [10, 10])
        options = {"stroke": [[4, 0]], "tolerance": 0.25, "preserveIntersections": True}
        all_geometry = edit_design([guide, cut], "trim-quick", {**options, "canvasFilter": "all"})
        self.assertEqual(all_geometry["entities"][0]["start"], [10, 0])
        self.assertEqual(all_geometry["entities"][0]["end"], [20, 0])
        guides = edit_design([guide, cut], "trim-quick", {**options, "canvasFilter": "guides"})
        self.assertTrue(guides["changed"])
        self.assertEqual(guides["entities"], [cut])
        cuts = edit_design([guide, cut], "trim-quick", {**options, "canvasFilter": "cut"})
        self.assertFalse(cuts["changed"])
        self.assertEqual(cuts["entities"], [guide, cut])
        with self.assertRaisesRegex(CadDesignError, "фильтр"):
            edit_design([guide, cut], "trim-quick", {**options, "canvasFilter": "unknown"})

    def test_eraser_filter_prefers_notch_category_and_keeps_parent_rules(self):
        cut = line("cut", [0, 0], [20, 0], groupPath=["detail"])
        mark = line("mark", [10, 0], [10, 5], reference=True, notch=True, parentId="cut")
        point = {"id": "point", "type": "point", "point": [10, 0], "groupPath": ["detail"]}
        entities = [cut, mark, point]
        options = {"stroke": [[10, 0]], "tolerance": 0.25, "groupPath": ["detail"]}
        for category, expected in (("notches", [cut, point]), ("guides", [cut, mark]), ("cut", [point])):
            with self.subTest(category=category):
                response = edit_design(entities, "trim-quick", {**options, "canvasFilter": category})
                self.assertTrue(response["changed"])
                self.assertEqual(response["entities"], validate_design(expected))

    def test_corner_repair_ignores_hidden_guides_but_protects_attached_notches(self):
        outline = {"id": "outline", "type": "polyline", "layer": "0", "closed": True, "joined": True,
                   "points": [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]}
        guide = line("guide", [20.004, 0], [22, 0], reference=True)
        notch = line("protected", [20.004, 0], [22, 0], notch=True, parentId="outline")
        options = {"stroke": [[20.002, -0.0025]], "tolerance": 0.00032}
        self.assertFalse(edit_design([outline, guide], "trim-quick", options)["changed"])
        filtered = edit_design([outline, guide], "trim-quick", {**options, "canvasFilter": "cut"})
        self.assertTrue(filtered["changed"])
        self.assertEqual(filtered["entities"], [
            {**outline, "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}, guide,
        ])
        protected = edit_design([outline, notch], "trim-quick", {**options, "canvasFilter": "cut"})
        self.assertFalse(protected["changed"])
        self.assertEqual(protected["entities"], [outline, notch])

    def test_click_on_tiny_corner_loop_keeps_closed_outline_identity_and_notch(self):
        points = [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]
        for closed in (True, False):
            with self.subTest(closed=closed):
                outline = {"id": "outline", "type": "polyline", "points": points if closed else [*points, points[0]],
                           "closed": closed, "layer": "Крой", "groupPath": ["detail"], "joined": True}
                notch = line("notch", [5, 0], [5, 2], notch=True, parentId="outline")
                outside = line("outside", [40, 0], [40, 20])
                result = edit_design([outline, notch, outside], "trim-quick", {
                    "stroke": [[20.002, -0.0025]], "tolerance": 0.00032, "groupPath": ["detail"],
                })
                self.assertTrue(result["changed"])
                self.assertEqual(result["warnings"], [])
                self.assertEqual(result["selectedIds"], [outline["id"]])
                self.assertEqual(result["entities"], [
                    {**outline, "closed": True, "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}, notch, outside,
                ])

    def test_click_on_either_overrun_tail_is_also_eligible(self):
        outline = {"id": "outline", "type": "polyline", "closed": True,
                   "points": [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]}
        for point in ([20.002, 0], [20, -0.003]):
            with self.subTest(point=point):
                result = quick_trim([outline], [point], tolerance=0.00032)
                self.assertTrue(result["changed"])
                self.assertEqual(result["entities"], [{**outline, "layer": "0", "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}])

    def test_click_on_ordinary_side_or_large_loop_keeps_original_eraser_behavior(self):
        outline = {"id": "outline", "type": "polyline", "closed": True,
                   "points": [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]}
        large = {**outline, "points": [[0, 0], [20.4, 0], [20, -0.5], [20, 20], [0, 20]]}
        for source, point in ((outline, [20, 0.003]), (large, [20.2, -0.25])):
            with self.subTest(point=point):
                result = quick_trim([source], [point], tolerance=0.00032)
                self.assertTrue(result["changed"])
                self.assertTrue(all(piece["id"] != source["id"] and not piece.get("closed") for piece in result["entities"]))

    def test_protected_or_ambiguous_tiny_loop_is_left_unchanged(self):
        outline = {"id": "outline", "type": "polyline", "closed": True, "joined": True,
                   "points": [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]}
        notch = line("protected", [20.004, 0], [22, 0], notch=True, parentId="outline")
        ambiguous = {**outline, "points": [*outline["points"][:4], [-0.004, 20], [0, 20.005]]}
        for entities in ([outline, notch], [ambiguous]):
            with self.subTest(entities=entities):
                result = quick_trim(entities, [[20.002, -0.0025]], tolerance=0.00032)
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], validate_design(entities))
                self.assertIn("Контур сохранён", result["warnings"][0])

    def test_click_on_line_chain_loop_keeps_native_sides_and_identity(self):
        incoming = line("incoming", [20.004, 0], [0, 0], groupPath=["detail"])
        connector = line("connector", [20, -0.005], [20.004, 0], groupPath=["detail"])
        outgoing = line("outgoing", [20, 20], [20, -0.005], groupPath=["detail"])
        top = line("top", [20, 20], [0, 20], groupPath=["detail"])
        left = line("left", [0, 0], [0, 20], groupPath=["detail"])
        notch = line("notch", [12, 0], [12, 2], notch=True, parentId="incoming")
        entities = [incoming, connector, outgoing, top, left, notch]
        result = edit_design(entities, "trim-quick", {
            "stroke": [[20.002, -0.0025]], "tolerance": 0.00032, "groupPath": ["detail"],
        })
        self.assertTrue(result["changed"])
        self.assertEqual(result["warnings"], [])
        self.assertCountEqual(result["selectedIds"], ["incoming", "outgoing"])
        self.assertEqual(result["entities"], [{**incoming, "start": [20, 0]}, {**outgoing, "end": [20, 0]}, top, left, notch])

    def test_unjoined_rectangle_click_and_collinear_drag_remove_only_one_side(self):
        shape = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True, "joined": False}
        for stroke in ([[8, 0.1]], [[-1, 0], [21, 0]]):
            with self.subTest(stroke=stroke):
                result = quick_trim([shape], stroke)
                piece, = result["entities"]
                self.assertTrue(result["changed"])
                self.assertFalse(piece["closed"])
                self.assertFalse(piece["joined"])
                self.assertEqual(piece["points"], [[20, 0], [20, 10], [0, 10], [0, 0]])

    def test_unjoined_rectangle_drag_removes_both_crossed_sides(self):
        shape = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True}
        result = quick_trim([shape], [[8, -1], [8, 11]])
        left, right = sorted(result["entities"], key=lambda piece: piece["start"][0])
        self.assertEqual((left["start"], left["end"]), ([0, 10], [0, 0]))
        self.assertEqual((right["start"], right["end"]), ([20, 0], [20, 10]))

    def test_unjoined_rectangle_stroke_leaving_through_corner_keeps_neighbor(self):
        shape = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True}
        result = quick_trim([shape], [[20, 8], [20, 0], [24, -4]])
        piece, = result["entities"]
        self.assertEqual(piece["points"], [[20, 10], [0, 10], [0, 0], [20, 0]])

    def test_joined_rectangle_remains_one_eraser_target_without_intersections(self):
        shape = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True, "joined": True}
        self.assertEqual(quick_trim([shape], [[8, 0.1]])["entities"], [])

    def test_unjoined_rectangle_erases_to_intersection_and_keeps_other_groups(self):
        shape = {"id": "outline", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10], [0, 10]],
                 "closed": True, "layer": "profile", "groupPath": ["group"]}
        cutter = line("cut", [10, -5], [10, 5], groupPath=["group"])
        outside = line("outside", [15, -5], [15, 5])
        result = edit_design([shape, cutter, outside], "trim-quick", {
            "stroke": [[8, 0.1]], "tolerance": 0.25, "groupPath": ["group"],
        })
        piece, actual_cutter, actual_outside = result["entities"]
        self.assertEqual(piece["points"], [[10, 0], [20, 0], [20, 10], [0, 10], [0, 0]])
        self.assertEqual(piece["groupPath"], ["group"])
        self.assertEqual([actual_cutter, actual_outside], validate_design([cutter, outside]))
        result = quick_trim([shape, cutter], [[-1, 0], [21, 0]])
        piece = next(piece for piece in result["entities"] if piece["layer"] == "profile")
        self.assertEqual(piece["points"], [[20, 0], [20, 10], [0, 10], [0, 0]])

    def test_unjoined_two_bulge_contour_keeps_the_other_arc(self):
        shape = {"id": "outline", "type": "polyline", "points": [[-10, 0], [10, 0]],
                 "closed": True, "bulges": [1, 1]}
        result = quick_trim([shape], [[0, -10]])
        piece, = result["entities"]
        self.assertFalse(piece["closed"])
        self.assertAlmostEqual(piece["bulges"][0], 1)
        self.assertAlmostEqual(piece["points"][0][0], 10)
        self.assertAlmostEqual(piece["points"][1][0], -10)

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
                   "closed": True, "joined": True}
        cutter = line("cut", [10, -5], [10, 15])
        after = quick_trim([outline, cutter], [[9.9, -1], [9.9, 1]])
        self.assertGreaterEqual(len(after["entities"]), 1)
        right = after["entities"][0]
        self.assertEqual(right["points"], [[10, 0], [20, 0], [20, 10], [10, 10]])
        self.assertTrue(right["joined"])

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
                 "closed": True, "layer": "profile", "joined": True}
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
