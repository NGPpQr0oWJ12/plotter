import unittest
from itertools import pairwise
from math import dist, nextafter
from unittest.mock import patch

from cut_plan import prepare_cut_plan
from xiao_job import COORDINATE_OFFSET_MM, xiao_coordinates


def rectangle(x0, y0, x1, y1, layer="Крой"):
    return {"layer": layer, "closed": True, "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


class CutPlanTests(unittest.TestCase):
    def test_extended_trim_reaches_beyond_the_fabric_without_cutting_through_a_part(self):
        job = {"paths": [rectangle(20, 10, 220, 40)], "materialWidthMm": 1380, "optimize": False}
        plan = prepare_cut_plan(job, 100, 1650, 1, extended_waste_trim=True)
        baseline = prepare_cut_plan(job, 100, 1650, 1)
        first, _, last = plan["pages"]
        self.assertEqual([page["paths"] for page in plan["pages"]], [page["paths"] for page in baseline["pages"]])
        self.assertEqual(plan["material"], baseline["material"])
        self.assertEqual([path["points"] for path in first["wasteTrim"]["paths"]],
                         [[[99, -30], [99, 10]], [[99, 40], [99, 1420]]])
        trim, = last["wasteTrim"]["paths"]
        self.assertEqual(trim["points"], [[230, -30], [230, 1420]])

        for height, fabric in ((1400, 1380), (1650, 1450)):
            with self.subTest(height=height, fabric=fabric):
                supported_job = {**job, "materialWidthMm": fabric}
                plan = prepare_cut_plan(supported_job, 100, height, 1, extended_waste_trim=True)
                trim, = plan["pages"][-1]["wasteTrim"]["paths"]
                self.assertEqual(trim["points"], [[230, 0], [230, fabric]])

    def test_waste_trim_uses_free_intervals_between_full_part_footprints(self):
        outer = rectangle(20, 10, 220, 40)
        hole = rectangle(55, 15, 170, 30, "Отверстие")
        second = rectangle(20, 50, 220, 65)
        job = {"paths": [outer, hole, second], "materialWidthMm": 70}
        plan = prepare_cut_plan(job, 100, 80, 1)
        baseline = prepare_cut_plan({"paths": job["paths"]}, 100, 80, 1)
        page = plan["pages"][0]
        self.assertEqual(page["paths"], baseline["pages"][0]["paths"])
        self.assertEqual(page["wasteTrim"]["issues"], [])
        self.assertEqual([path["points"] for path in page["wasteTrim"]["paths"]],
                         [[[99, 0], [99, 10]], [[99, 40], [99, 50]], [[99, 65], [99, 70]]])
        self.assertTrue(all(not path["closed"] and path["operation"] == "wasteTrim" and path["layer"] == "waste-trim"
                            for path in page["wasteTrim"]["paths"]))
        self.assertEqual(plan["material"]["partsAreaMm2"], baseline["material"]["partsAreaMm2"])
        self.assertAlmostEqual(page["cutDistanceMm"], baseline["pages"][0]["cutDistanceMm"] + 25)
        self.assertAlmostEqual(plan["routeOptimization"]["afterMm"], plan["travelDistanceMm"])
        self.assertGreater(plan["estimatedSeconds"], baseline["estimatedSeconds"])

    def test_waste_trim_can_meet_cut_edge_but_never_overlaps_it(self):
        for cut_edge, next_edge in ((99, 110), (65, 95), (65, 95.0006)):
            with self.subTest(cut_edge=cut_edge, next_edge=next_edge):
                job = {"paths": [rectangle(10, 10, cut_edge, 40), rectangle(next_edge, 10, 140, 40)],
                       "materialWidthMm": 70}
                page = prepare_cut_plan(job, 100, 80, 1)["pages"][0]
                lower, upper = [path["points"] for path in page["wasteTrim"]["paths"]]
                if cut_edge == 99:
                    self.assertEqual((lower, upper), ([[99, 0], [99, 10]], [[99, 40], [99, 70]]))
                else:
                    self.assertEqual(page["endX"], next_edge)
                    self.assertLess(lower[1][1], 10)
                    self.assertGreater(upper[0][1], 40)

    def test_waste_trim_preserves_tangencies_and_notches(self):
        tangent = {"points": [[10, 10], [99, 25], [10, 40]], "closed": True}
        notch = {"points": [[99, 45], [99, 50]], "notch": True}
        job = {"paths": [tangent, notch, rectangle(110, 12, 140, 30)], "materialWidthMm": 60}
        trim = prepare_cut_plan(job, 100, 80, 1)["pages"][0]["wasteTrim"]
        below, middle, above = [path["points"] for path in trim["paths"]]
        self.assertLess(below[1][1], 25)
        self.assertGreater(middle[0][1], 25)
        self.assertLess(middle[1][1], 45)
        self.assertGreater(above[0][1], 50)

    def test_waste_trim_protects_ambiguous_group_between_disjoint_strokes(self):
        first = {"points": [[20, 20], [20, 30]]}
        last = {"points": [[170, 25], [170, 55]]}
        job = {"paths": [rectangle(110, 5, 145, 15), first, last], "materialWidthMm": 60}
        page = prepare_cut_plan(job, 100, 80, 1)["pages"][0]
        self.assertEqual(page["endX"], 99)
        self.assertFalse(page["wasteTrim"]["paths"])
        self.assertTrue(page["wasteTrim"]["issues"])
        first["partNumber"] = last["partNumber"] = 2
        trim = prepare_cut_plan(job, 100, 80, 1)["pages"][0]["wasteTrim"]
        below, above = [path["points"] for path in trim["paths"]]
        self.assertLess(below[1][1], 20)
        self.assertGreater(above[0][1], 55)
        self.assertTrue(trim["issues"])

    def test_waste_trim_protects_invalid_closed_outline_envelope(self):
        outline = {"closed": True, "partNumber": 2,
                   "points": [[10, 20], [140, 45], [10, 45], [140, 20]]}
        job = {"paths": [outline, rectangle(15, 5, 60, 10)], "materialWidthMm": 50}
        trim = prepare_cut_plan(job, 100, 80, 1)["pages"][0]["wasteTrim"]
        below, above = [path["points"] for path in trim["paths"]]
        self.assertLess(below[1][1], 20)
        self.assertGreater(above[0][1], 45)
        self.assertTrue(trim["issues"])

    def test_waste_trim_rounds_into_free_area_and_omits_sub_grid_gap(self):
        job = {"paths": [rectangle(20, 10.0006, 130, 24.0001), rectangle(20, 24.0004, 130, 39.9994)],
               "materialWidthMm": 70.0006}
        plan = prepare_cut_plan(job, 100, 80, 1)
        lower, upper = [path["points"] for path in plan["pages"][0]["wasteTrim"]["paths"]]
        self.assertLessEqual(float(xiao_coordinates(*lower[-1])[1]) - COORDINATE_OFFSET_MM, 10.0006)
        self.assertGreaterEqual(float(xiao_coordinates(*upper[0])[1]) - COORDINATE_OFFSET_MM, 39.9994)
        self.assertLess(float(xiao_coordinates(*upper[-1])[1]) - COORDINATE_OFFSET_MM, 70.0006)

    def test_waste_trim_final_margin_is_after_layout_inside_workable_window(self):
        job = {"paths": [rectangle(20, 5, 95, 40)], "materialWidthMm": 70}
        page, = prepare_cut_plan(job, 100, 80, 1)["pages"]
        self.assertFalse(page["wasteTrim"]["paths"])
        self.assertTrue(page["wasteTrim"]["issues"])
        job["materialMarginMm"] = 3
        page, = prepare_cut_plan(job, 100, 80, 1)["pages"]
        trim, = page["wasteTrim"]["paths"]
        self.assertEqual((page["endX"], page["feedAfterMm"], trim["points"]), (95, 0, [[98, 0], [98, 70]]))
        job["materialMarginMm"] = .0001
        page, = prepare_cut_plan(job, 100, 80, 1)["pages"]
        self.assertFalse(page["wasteTrim"]["paths"])
        self.assertTrue(page["wasteTrim"]["issues"])

    def test_waste_trim_requires_declared_width_that_fits_encoded_field(self):
        job = {"paths": [rectangle(10, 5, 50, 30)]}
        for width in (None, 80, 27):
            with self.subTest(width=width):
                if width is not None:
                    job["materialWidthMm"] = width
                page, = prepare_cut_plan(job, 100, 80, 1)["pages"]
                self.assertFalse(page["wasteTrim"]["paths"])
                self.assertTrue(page["wasteTrim"]["issues"])
        job["materialWidthMm"] = 45
        page, = prepare_cut_plan(job, 100, 80, 1)["pages"]
        trim, = page["wasteTrim"]["paths"]
        self.assertEqual(trim["points"], [[60, 0], [60, 45]])

    def test_waste_trim_can_run_on_empty_verified_pass(self):
        plan = prepare_cut_plan({"paths": [rectangle(215, 10, 290, 40)], "materialWidthMm": 60}, 100, 80, 1)
        page = plan["pages"][1]
        self.assertEqual(page["paths"], [])
        trim, = page["wasteTrim"]["paths"]
        self.assertEqual(trim["points"], [[198, 0], [198, 60]])

    def test_waste_trim_approach_is_included_in_route_choice(self):
        outer = rectangle(10, 20, 60, 40)
        outer["points"] = outer["points"][1:] + outer["points"][:1]
        old = prepare_cut_plan({"paths": [outer]}, 100, 80, 1)
        self.assertEqual(old["pages"][0]["paths"][0]["points"][0], [10, 20])
        job = {"paths": [outer], "materialWidthMm": 70, "materialMarginMm": 25}
        plan = prepare_cut_plan(job, 100, 80, 1)
        self.assertEqual(plan["pages"][0]["paths"][0]["points"][0], [60, 20])
        self.assertAlmostEqual(plan["routeOptimization"]["beforeMm"], plan["travelDistanceMm"])
        self.assertAlmostEqual(plan["routeOptimization"]["afterMm"], plan["travelDistanceMm"])
        job["paths"].append(rectangle(17, 25, 22, 30, "Отверстие"))
        ordered = prepare_cut_plan(job, 100, 80, 1)["pages"][0]["paths"]
        self.assertEqual([path["index"] for path in ordered], [1, 0])

    def test_single_page_keeps_placement_and_order_in_normalized_geometry(self):
        frame = rectangle(20, 30, 40, 50)
        frame["points"].extend([[20, 30], [20, 30]])
        inner = rectangle(23, 34, 25, 37, "Отверстие")
        plan = prepare_cut_plan({"paths": [inner, frame], "optimize": False}, 100, 80, 0.12)
        self.assertEqual(plan["mode"], "single")
        self.assertEqual(plan["issues"], [])
        page, = plan["pages"]
        self.assertEqual((page["startX"], page["endX"], page["feedAfterMm"]), (0, 40, 0))
        self.assertEqual(page["paths"], [{**inner, "index": 0}, {**frame, "index": 1, "points": frame["points"][:4]}])
        self.assertAlmostEqual(plan["cutDistanceMm"], 90)

    def test_roll_route_reduces_empty_travel_on_each_section_with_same_cut_geometry(self):
        def open_path(first, last):
            return {"layer": "Разметка", "closed": False, "points": [first, last]}

        paths = [
            open_path([55, 20], [50, 20]), open_path([145, 20], [140, 20]),
            open_path([9, 12], [5, 12]), open_path([105, 12], [101, 12]),
            open_path([27, 16], [22, 16]), open_path([127, 16], [122, 16]),
        ]
        baseline = prepare_cut_plan({"paths": paths, "optimize": False}, 100, 80, 0.12)
        plan = prepare_cut_plan({"paths": paths}, 100, 80, 0.12)
        self.assertEqual([[path["index"] for path in page["paths"]] for page in plan["pages"]], [[2, 4, 0], [3, 5, 1]])
        self.assertEqual(plan["pages"][0]["paths"][0]["points"], [[5, 12], [9, 12]])
        self.assertLess(plan["travelDistanceMm"], baseline["travelDistanceMm"] * .55)
        self.assertEqual(plan["cutDistanceMm"], baseline["cutDistanceMm"])
        self.assertAlmostEqual(plan["routeOptimization"]["beforeMm"], baseline["travelDistanceMm"])
        self.assertAlmostEqual(plan["routeOptimization"]["afterMm"], plan["travelDistanceMm"])

    def test_short_marks_and_hole_cut_before_outline_without_reversing_winding(self):
        outer = rectangle(10, 10, 80, 70)
        hole = rectangle(64, 41, 70, 53, "Отверстие")
        notch = {"layer": "Насечка", "closed": False, "points": [[80, 57], [84, 57]]}
        plan = prepare_cut_plan({"paths": [outer, hole, notch]}, 100, 90, 0.12)
        route = plan["pages"][0]["paths"]
        self.assertEqual({path["index"] for path in route[:2]}, {1, 2})
        outline = route[-1]
        self.assertEqual(outline["index"], 0)
        self.assertNotEqual(outline["points"][0], outer["points"][0])
        offset = outer["points"].index(outline["points"][0])
        self.assertEqual(outline["points"], outer["points"][offset:] + outer["points"][:offset])

    def test_roll_boundary_uses_gap_and_keeps_outline_and_hole_together(self):
        first = rectangle(10, 10, 65, 40)
        outline = rectangle(95, 15, 140, 45)
        hole = rectangle(105, 20, 110, 30)
        plan = prepare_cut_plan({"paths": [first, hole, outline]}, 100, 80, None)
        self.assertEqual((plan["mode"], plan["issues"], plan["seams"]), ("roll", [], []))
        self.assertEqual([(page["startX"], page["endX"], page["feedAfterMm"]) for page in plan["pages"]],
                         [(0, 95, 95), (95, 140, 0)])
        self.assertEqual([[path["index"] for path in page["paths"]] for page in plan["pages"]], [[0], [1, 2]])
        self.assertTrue(all(path["closed"] for page in plan["pages"] for path in page["paths"]))
        self.assertEqual(plan["feedDistanceMm"], 95)

    def test_overlength_outline_is_split_without_cutting_along_the_page_edge(self):
        outline = rectangle(20, 10, 220, 40)
        plan = prepare_cut_plan({"paths": [outline]}, 100, 80, 1)
        self.assertEqual(plan["issues"], [])
        self.assertEqual([(page["startX"], page["endX"]) for page in plan["pages"]],
                         [(0, 99), (99, 198), (198, 220)])
        self.assertEqual({(seam["x"], seam["y"]) for seam in plan["seams"]},
                         {(99, 10), (198, 10), (99, 40), (198, 40)})
        self.assertAlmostEqual(plan["cutDistanceMm"], 460)
        for page in plan["pages"]:
            for path in page["paths"]:
                self.assertFalse(path["closed"])
                for first, second in pairwise(path["points"]):
                    self.assertLessEqual(page["startX"], min(first[0], second[0]))
                    self.assertLessEqual(max(first[0], second[0]), page["endX"])
                    self.assertFalse(first[0] == second[0] and first[0] in (99, 198))

    def test_long_outline_keeps_a_boundary_edge_with_the_incoming_cut(self):
        outline = {"closed": True, "points": [
            [99, 10], [99, 20], [50, 20], [50, 30], [220, 30], [220, 40], [20, 40], [20, 10],
        ]}
        plan = prepare_cut_plan({"paths": [outline], "optimize": False}, 100, 80, 1)
        self.assertEqual({(seam["x"], seam["y"]) for seam in plan["seams"]},
                         {(99, 30), (198, 30), (99, 40), (198, 40)})
        edge = [[99, 10], [99, 20]]
        self.assertEqual([page["number"] for page in plan["pages"] for path in page["paths"]
                          if any([first, second] == edge for first, second in pairwise(path["points"]))], [1])
        original = outline["points"]
        self.assertAlmostEqual(plan["cutDistanceMm"], sum(dist(a, b) for a, b in pairwise(original + original[:1])))

    def test_long_open_cut_crosses_at_exact_boundary_even_near_a_vertex(self):
        near_boundary = nextafter(99.0, 0)
        path = {"points": [[5, 10], [near_boundary, 10], [99, 30], [130, 30]]}
        plan = prepare_cut_plan({"paths": [path], "optimize": False}, 100, 80, 1)
        self.assertEqual(plan["seams"], [{"x": 99, "y": 30, "index": 0}])
        first, second = (page["paths"][0]["points"] for page in plan["pages"])
        self.assertEqual(first[-1], second[0])
        self.assertEqual(first + second[1:], path["points"])

    def test_notch_on_end_boundary_stays_with_numbered_piece(self):
        outline = {**rectangle(10, 10, 99, 40), "partNumber": 1}
        notch = {"points": [[99, 40], [99, 45]], "partNumber": 1}
        next_part = rectangle(110, 10, 140, 40)
        plan = prepare_cut_plan({"paths": [outline, notch, next_part], "optimize": False}, 100, 80, 1)
        self.assertEqual(plan["seams"], [])
        self.assertEqual([[path["index"] for path in page["paths"]] for page in plan["pages"]], [[0, 1], [2]])

    def test_seam_fragments_keep_direction_and_relative_order_during_route_optimization(self):
        outline = rectangle(20, 10, 220, 40)
        decoration = rectangle(130, 25, 145, 32)
        paths = [outline, decoration]
        baseline = prepare_cut_plan({"paths": paths, "optimize": False}, 100, 80, 1)
        plan = prepare_cut_plan({"paths": paths}, 100, 80, 1)
        self.assertEqual(plan["seams"], baseline["seams"])
        for page, old_page in zip(plan["pages"], baseline["pages"]):
            self.assertEqual([path for path in page["paths"] if path["index"] == 0],
                             [path for path in old_page["paths"] if path["index"] == 0])
        self.assertEqual([path["index"] for path in plan["pages"][1]["paths"]], [1, 0, 0])

    def test_overlapping_fitting_parts_are_flagged_instead_of_split(self):
        first = rectangle(10, 10, 95, 30)
        second = rectangle(85, 35, 150, 55)
        plan = prepare_cut_plan({"paths": [first, second]}, 100, 80, 0.12)
        self.assertEqual(plan["pages"], [])
        self.assertEqual(plan["issues"][0]["indices"], [0, 1])

    def test_numbered_part_picks_a_boundary_before_all_its_fragments(self):
        first = rectangle(0, 0, 50, 20)
        front = {**rectangle(70, 0, 85, 20), "partNumber": 4}
        back = {**rectangle(105, 0, 120, 20), "partNumber": 4}
        plan = prepare_cut_plan({"paths": [first, front, back]}, 100, 80, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["seams"], [])
        self.assertEqual([page["startX"] for page in plan["pages"]], [0, 70])
        self.assertEqual([[path["index"] for path in page["paths"]] for page in plan["pages"]], [[0], [1, 2]])

    def test_move_recalculates_feed_under_the_working_pass_limit(self):
        first = rectangle(10, 10, 1500, 200)
        for x, expected in ((2380, 2380), (2410, 2410), (2480, 2450)):
            with self.subTest(x=x):
                second = rectangle(x, 10, x + 100, 200)
                plan = prepare_cut_plan({"paths": [first, second]}, 2500, 1650, 0.12,
                                        roll_pass_length_mm=2450)
                self.assertEqual((plan["mode"], plan["passLimitMm"], plan["issues"], plan["seams"]),
                                 ("roll", 2450, [], []))
                self.assertEqual(plan["pages"][0]["feedAfterMm"], expected)

    def test_open_line_of_a_long_part_can_continue_to_keep_a_short_part_whole(self):
        long_part = {**rectangle(5, 10, 180, 30), "partNumber": 1}
        line = {"points": [[40, 20], [130, 20]], "partNumber": 1}
        mark = {"points": [[81, 15], [85, 15]], "partNumber": 1}
        hole = {**rectangle(115, 13, 120, 18), "partNumber": 1}
        short_part = {**rectangle(80, 40, 155, 70), "partNumber": 2}
        plan = prepare_cut_plan({"paths": [long_part, line, mark, hole, short_part]}, 100, 80, 1)
        self.assertEqual(plan["issues"], [])
        self.assertEqual([page["endX"] for page in plan["pages"]], [80, 179, 180])
        self.assertEqual({seam["index"] for seam in plan["seams"]}, {0, 1})
        self.assertEqual({path["index"] for path in plan["pages"][1]["paths"]}, {0, 1, 2, 3, 4})

    def test_part_equal_to_nominal_length_can_split_for_page_allowance(self):
        plan = prepare_cut_plan({"paths": [rectangle(20, 10, 120, 40)]}, 100, 80, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(len(plan["pages"]), 2)
        self.assertEqual(len(plan["seams"]), 2)
        self.assertAlmostEqual(plan["pages"][1]["startX"], 99.88)

    def test_material_utilization_uses_recovered_outlines_and_subtracts_holes(self):
        outline = rectangle(10, 10, 50, 40)
        points = outline["points"] + outline["points"][:1]
        edges = [{"layer": "Крой", "closed": False, "points": [start, end]}
                 for start, end in pairwise(points)]
        hole = rectangle(20, 20, 30, 30)
        notch = {"layer": "Крой", "closed": False, "points": [[10, 10], [8, 8]]}
        plan = prepare_cut_plan({"paths": [*edges, hole, notch], "materialWidthMm": 50,
                                 "materialMarginMm": 5}, 100, 80, 0.12)
        self.assertEqual(plan["repairableContourCount"], 1)
        material = plan["material"]
        self.assertEqual(material["lengthMm"], 55)
        self.assertEqual(material["partsAreaMm2"], 1100)
        self.assertEqual(material["utilizationPercent"], 40)
        self.assertEqual(material["openContourCount"], 0)

    def test_kim_unavailable_when_outlines_overlap_without_containment(self):
        outlines = [rectangle(10, 10, 40, 40), rectangle(20, 20, 50, 50)]
        plan = prepare_cut_plan({"paths": outlines}, 100, 80, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertIsNone(plan["material"]["utilizationPercent"])
        self.assertIsNone(plan["material"]["partsAreaMm2"])

    def test_material_area_accepts_a_sub_micron_closing_sliver(self):
        outline = {"closed": True, "points": [
            [10, 10], [40, 10], [40, 40], [10, 40], [10.00002, 9.99998],
        ]}
        plan = prepare_cut_plan({"paths": [outline], "materialWidthMm": 50}, 100, 80, 0.12)
        self.assertAlmostEqual(plan["material"]["partsAreaMm2"], 900, places=3)
        self.assertAlmostEqual(plan["material"]["utilizationPercent"], 45, places=4)

    def test_work_estimate_and_page_progress_exclude_feed_time(self):
        paths = [rectangle(10, 10, 65, 40), rectangle(95, 15, 140, 45)]
        plan = prepare_cut_plan({"paths": paths, "speed": 100, "travelSpeed": 200}, 100, 80, None)
        first, second = plan["pages"]
        self.assertAlmostEqual(plan["cutSeconds"], 3.2)
        self.assertAlmostEqual(plan["travelSeconds"], (2 ** .5 * 10 + 15) / 200)
        self.assertEqual(first["workStartSeconds"], 0)
        self.assertAlmostEqual(second["workStartSeconds"], first["estimatedWorkSeconds"])
        self.assertAlmostEqual(plan["estimatedWorkSeconds"], plan["cutSeconds"] + plan["travelSeconds"])
        self.assertAlmostEqual(plan["estimatedSeconds"] - plan["estimatedWorkSeconds"], 95 / 150)

    def test_width_and_single_page_compensation_are_checked_before_simulation(self):
        wide = prepare_cut_plan({"paths": [rectangle(20, 30, 140, 80)]}, 100, 80, 0.12)
        self.assertEqual(wide["pages"], [])
        self.assertEqual(wide["issues"][0]["indices"], [0])
        self.assertIn("ширину", wide["issues"][0]["message"])
        margin = prepare_cut_plan({"paths": [rectangle(20, 30, 99.95, 50)]}, 100, 80, 0.12)
        self.assertEqual(margin["mode"], "single")
        self.assertEqual(margin["pages"], [])
        self.assertIn("добавкой страницы", margin["issues"][0]["message"])

    def test_can_feed_between_parts_when_xiao_allowance_prevents_a_single_page(self):
        first = rectangle(10, 10, 45, 30)
        second = rectangle(50, 10, 99.95, 30)
        plan = prepare_cut_plan({"paths": [first, second]}, 100, 80, 0.12)
        self.assertEqual((plan["mode"], plan["issues"], plan["seams"]), ("roll", [], []))
        self.assertEqual([page["startX"] for page in plan["pages"]], [0, 50])
        self.assertEqual([[path["index"] for path in page["paths"]] for page in plan["pages"]], [[0], [1]])
        preview = prepare_cut_plan({"paths": [first, second]}, 100, 80, None)
        self.assertEqual((preview["mode"], preview["issues"], len(preview["pages"])), ("single", [], 1))

    def test_roll_plan_limits_generated_fragments(self):
        with patch("cut_plan.MAX_PLANNED_POINTS", 10):
            plan = prepare_cut_plan({"paths": [rectangle(20, 10, 220, 40)]}, 100, 80, 1)
        self.assertEqual((plan["pages"], plan["seams"]), ([], []))
        self.assertEqual(plan["issues"][0]["indices"], [0])


if __name__ == "__main__":
    unittest.main()
