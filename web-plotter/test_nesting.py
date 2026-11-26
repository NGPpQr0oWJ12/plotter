from copy import deepcopy
from math import cos, dist, isclose, radians, sin
import unittest

from cut_plan import prepare_cut_plan
from designer_cad import prepare_design
from machine import PlotterError
from nesting import NestingFitError, identify_parts, nest_paths


def contour(points, *, layer="Лекало", closed=True):
    return {"points": points, "layer": layer, "closed": closed}


def rectangle(x, y, length, width, **options):
    return contour([[x, y], [x + length, y], [x + length, y + width], [x, y + width]], **options)


def pack(paths, **settings):
    options = dict(width_mm=1650, gap_mm=10, margin_mm=10, rotation="none", page_length_mm=2499.88)
    options.update(settings)
    return nest_paths(paths, **options)


def tilt(path, angle, *, x=0, y=0):
    angle = radians(angle)
    return {**path, "points": [[x + a * cos(angle) - b * sin(angle),
                              y + a * sin(angle) + b * cos(angle)] for a, b in path["points"]]}


class NestingTests(unittest.TestCase):
    def test_registry_part_names_follow_numbered_parts_after_nesting(self):
        paths = [{**rectangle(0, 0, 90, 50), "partNumber": 5, "partDisplayNumber": "6/1",
                  "partName": "Сиденье", "productName": "Диван"},
                 {**rectangle(0, 0, 30, 40), "partNumber": 7, "partName": "Вставка"}]
        result = pack(paths)["paths"]
        self.assertEqual([(p["partNumber"], p.get("partDisplayNumber"), p["partName"], p.get("productName")) for p in result],
                         [(5, "6/1", "Сиденье", "Диван"), (7, None, "Вставка", None)])

    def test_omitted_and_null_fabric_length_preserve_unlimited_layout(self):
        paths = [rectangle(0, index * 160, 90, 50) for index in range(2)]
        options = dict(width_mm=100, gap_mm=4, margin_mm=10, page_length_mm=119.88)

        unlimited = pack(paths, **options)

        self.assertEqual(pack(paths, length_mm=None, **options), unlimited)
        self.assertAlmostEqual(unlimited["lengthMm"], 204)
        self.assertEqual(unlimited["pageCount"], 2)

    def test_fabric_length_limit_includes_both_end_margins(self):
        paths = [rectangle(0, 0, 90, 50)]
        original = deepcopy(paths)
        options = dict(width_mm=100, gap_mm=4, margin_mm=10, page_length_mm=119.88)

        fitting = pack(paths, length_mm=110, **options)

        xs = [x for x, _ in fitting["paths"][0]["points"]]
        self.assertAlmostEqual(min(xs), 10)
        self.assertAlmostEqual(max(xs), 100)
        self.assertAlmostEqual(fitting["lengthMm"], 110)
        with self.assertRaises(PlotterError):
            pack(paths, length_mm=109.5, **options)
        self.assertEqual(paths, original)

    def test_multiple_passes_use_one_fabric_length_limit(self):
        paths = [rectangle(0, index * 160, 90, 50) for index in range(2)]
        original = deepcopy(paths)
        options = dict(width_mm=100, gap_mm=4, margin_mm=10, page_length_mm=119.88)

        result = pack(paths, length_mm=204, **options)
        plan = prepare_cut_plan({"paths": result["paths"]}, 120, 100, 0.12)

        self.assertEqual(result["partCount"], len(paths))
        self.assertAlmostEqual(result["lengthMm"], 204)
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual((plan["issues"], plan["seams"], len(plan["pages"])), ([], [], 2))
        with self.assertRaises(PlotterError):
            pack(paths, length_mm=203.5, **options)
        self.assertEqual(paths, original)

    def test_shorter_layout_must_still_split_into_valid_passes(self):
        sizes = [(90, 80), (110, 20), (40, 35)]
        paths = [rectangle(0, index * 160, *size) for index, size in enumerate(sizes)]
        options = dict(width_mm=120, gap_mm=4, margin_mm=6, page_length_mm=119.88, rotation="quarter")

        self.assertEqual(pack(paths, **options)["pageCount"], 2)
        with self.assertRaisesRegex(PlotterError, "разбиением на проходы"):
            pack(paths, length_mm=158, **options)

    def test_compacts_distant_parts_with_gaps_and_stable_contour_order(self):
        paths = [rectangle(-4400, 8000, 700, 550), rectangle(6900, 20, 500, 590),
                 rectangle(18000, -8500, 600, 650)]
        original = deepcopy(paths)
        result = pack(paths)
        self.assertEqual(paths, original)
        self.assertEqual(result, pack(paths))
        self.assertEqual(result["partCount"], 3)
        self.assertEqual(result["pageCount"], 1)
        self.assertLess(result["lengthMm"], 1500)
        for old, new in zip(paths, result["paths"]):
            self.assertEqual((old["layer"], old["closed"]), (new["layer"], new["closed"]))
            self.assertEqual([dist(*pair) for pair in zip(old["points"], old["points"][1:])],
                             [dist(*pair) for pair in zip(new["points"], new["points"][1:])])
            self.assertTrue(all(x >= 10 and 10 <= y <= 1640 for x, y in new["points"]))
        placements = result["placements"]
        for index, a in enumerate(placements):
            for b in placements[index + 1:]:
                x_gap = max(a["x"] - b["x"] - b["lengthMm"], b["x"] - a["x"] - a["lengthMm"])
                y_gap = max(a["y"] - b["y"] - b["widthMm"], b["y"] - a["y"] - a["widthMm"])
                self.assertGreaterEqual(max(x_gap, y_gap), 10)

    def test_closed_outline_and_internal_features_move_together(self):
        # Last shape is in the bounding box but in the notch, outside the part.
        outline = contour([[0, 0], [100, 0], [100, 100], [65, 100], [65, 35], [35, 35],
                           [35, 100], [0, 100]])
        paths = [outline, rectangle(5, 5, 15, 20, layer="Отверстие"),
                 contour([[9, 45], [11, 60], [18, 90]], layer="Линия", closed=False),
                 rectangle(42, 55, 15, 20, layer="Отдельная")]
        result = pack(paths)
        self.assertEqual(result["partCount"], 2)
        self.assertEqual(result["placements"][0]["indices"], [0, 1, 2])
        offset = [new - old for new, old in zip(result["paths"][0]["points"][0], outline["points"][0])]
        for index in (1, 2):
            self.assertEqual(result["paths"][index]["points"],
                             [[x + offset[0], y + offset[1]] for x, y in paths[index]["points"]])

    def test_fragmented_closed_perimeter_retains_attached_and_enclosed_marks(self):
        paths = [contour([a, b], closed=False) for a, b in [
            ([0, 0], [100, 0]), ([100, 100], [100, 0]), ([100, 100], [0, 100]), ([0, 0], [0, 100]),
            ([0, 0], [-5, 0]), ([-5, 0], [0, 0]), ([25, 25], [75, 75]), ([50, -2], [50, 3]),
        ]]
        result = pack(paths)
        self.assertEqual(result["partCount"], 1)
        self.assertEqual(result["placements"][0]["indices"], list(range(len(paths))))
        self.assertTrue(all(not path["closed"] for path in result["paths"]))

    def test_edge_notches_share_the_piece_quarter_turn(self):
        paths = [rectangle(0, 0, 80, 140),
                 contour([[30, -2], [30, 3]], closed=False),
                 contour([[40, 140.21], [40, 145.21]], closed=False),
                 contour([[60, 142], [65, 142]], closed=False)]
        result = pack(paths, width_mm=112, rotation="quarter")
        piece, standalone = result["placements"]
        self.assertEqual(piece["indices"], [0, 1, 2])
        self.assertEqual(piece["angle"], 90)
        self.assertEqual(standalone["indices"], [3])
        for index in piece["indices"]:
            for (x, y), (placed_x, placed_y) in zip(paths[index]["points"], result["paths"][index]["points"]):
                self.assertAlmostEqual(placed_x, piece["x"] + 145.21 - y)
                self.assertAlmostEqual(placed_y, piece["y"] + x)

    def test_disconnected_notch_can_point_toward_an_open_perimeter(self):
        paths = [contour([[0, 0], [100, 0], [100, 100], [0, 100], [0, 0.7]], closed=False),
                 contour([[100.7, 40], [105.7, 40]], closed=False),
                 contour([[100.7, 60], [100.7, 65]], closed=False)]
        result = pack(paths)
        self.assertEqual([part["indices"] for part in result["placements"]], [[0, 1], [2]])
        self.assertTrue(all(not path["closed"] for path in result["paths"]))

    def test_explicit_long_notch_keeps_part_number_and_rotates_with_outline(self):
        outline = rectangle(0, 0, 80, 140)
        notch = {**contour([[30, -12], [30, 3]], closed=False), "notch": True}
        imported = contour(notch["points"], layer="Надсечки", closed=False)
        for paths in ([outline, notch], [outline, imported],
                      [{**outline, "partNumber": 7}, {**notch, "partNumber": 13}]):
            with self.subTest(paths=paths):
                part, = identify_parts(paths)
                self.assertEqual(part["indices"], [0, 1])
                self.assertEqual(part["notchIndices"], [1])
                result = pack(paths, width_mm=112, rotation="quarter")
                placement, = result["placements"]
                self.assertEqual(placement["indices"], [0, 1])
                self.assertEqual(placement["angle"], 90)
                self.assertEqual({path["partNumber"] for path in result["paths"]}, {part["number"]})
                self.assertTrue(result["paths"][1]["notch"])
                self.assertAlmostEqual(result["paths"][1]["points"][0][0] - result["paths"][1]["points"][1][0], 15)

    def test_explicit_notch_num_does_not_merge_when_two_owners_are_tied(self):
        paths = [{**rectangle(0, 0, 100, 100), "partNumber": 4},
                 {**rectangle(104, 0, 100, 100), "partNumber": 8},
                 {**contour([[95, 50], [109, 50]], closed=False), "notch": True, "partNumber": 11}]
        self.assertEqual([part["indices"] for part in identify_parts(paths)], [[0], [1], [2]])

    def test_cad_bound_notch_survives_contour_repair_and_layout_cut_plan(self):
        vertices = [[0, 0], [80, 0], [80, 140], [0, 140], [0, 0]]
        entities = [{"id": f"edge-{index}", "type": "line", "start": start, "end": end}
                    for index, (start, end) in enumerate(zip(vertices, vertices[1:]))]
        entities.append({"id": "mark", "type": "line", "start": [40, -2], "end": [40, -17],
                         "layer": "Надсечки", "notch": True, "parentId": "edge-0"})
        prepared = prepare_design(entities)
        self.assertEqual(prepared["sourceIds"], [["edge-0", "edge-1", "edge-2", "edge-3"], ["mark"]])
        self.assertEqual(prepared["closedCount"], 1)
        outline, mark = prepared["paths"]
        self.assertEqual(outline["partNumber"], mark["partNumber"])
        layout = pack(prepared["paths"], width_mm=112, rotation="quarter")
        self.assertEqual(layout["partCount"], 1)
        plan = prepare_cut_plan({"paths": layout["paths"], "optimize": False}, 300, 112, 0.12)
        self.assertFalse(plan["issues"])
        self.assertEqual({item["index"] for item in plan["pages"][0]["paths"]}, {0, 1})

    def test_route_prioritizes_long_explicit_notch_on_the_numbered_outline(self):
        outline = {**rectangle(10, 10, 80, 65), "partNumber": 7}
        notch = {**contour([[60, 72], [60, 90]], closed=False), "partNumber": 7}

        def order(marker):
            plan = prepare_cut_plan({"paths": [outline, marker]}, 200, 112, 0.12)
            self.assertFalse(plan["issues"])
            return [item["index"] for item in plan["pages"][0]["paths"]]

        self.assertEqual(order({**notch, "notch": True}), [1, 0])
        self.assertEqual(order({**notch, "notch": False}), [0, 1])
        self.assertEqual(order({**notch, "notch": True, "partNumber": 8}), [0, 1])

    def test_ambiguous_edge_contact_does_not_join_distinct_pieces(self):
        paths = [rectangle(0, 0, 100, 100), rectangle(104, 0, 100, 100),
                 contour([[99.5, 50], [104.5, 50]], closed=False)]
        result = pack(paths)
        self.assertEqual([part["indices"] for part in result["placements"]], [[0], [1], [2]])

    def test_quarter_turn_fits_wide_piece_without_changing_scale_or_winding(self):
        paths = [rectangle(0, 0, 800, 1800), rectangle(90, 20, 40, 60)]
        with self.assertRaises(NestingFitError) as failure:
            pack(paths)
        details = failure.exception
        self.assertEqual(details.indices, [0])
        self.assertEqual(details.blocked_part_count, 1)
        self.assertAlmostEqual(details.usable_width_mm, 1629.99)
        self.assertAlmostEqual(details.minimum_part_width_mm, 1800)
        self.assertAlmostEqual(details.minimum_width_mm, 1820.01)
        self.assertAlmostEqual(details.quarter_turn_minimum_width_mm, 820.01)
        self.assertTrue(details.quarter_turn_fits)

        result = pack(paths, rotation="quarter")
        self.assertEqual((result["partCount"], result["rotatedCount"]), (1, 1))
        self.assertEqual(result["placements"][0]["angle"], 90)
        outline = result["paths"][0]["points"]
        self.assertEqual(outline, [[1810.0, 10.0], [1810.0, 810.0], [10.0, 810.0], [10.0, 10.0]])
        self.assertEqual(result["paths"][1]["points"][0], [1790.0, 100.0])

    def test_quarter_turn_still_cannot_fit_1400_by_1426_piece_on_1380(self):
        paths = [rectangle(0, 0, 1400, 1426), rectangle(2000, 2000, 1400, 1426)]
        with self.assertRaises(NestingFitError) as failure:
            pack(paths, width_mm=1380, rotation="half")
        details = failure.exception
        self.assertEqual(details.indices, [0, 1])
        self.assertEqual(details.blocked_part_count, 2)
        self.assertAlmostEqual(details.usable_width_mm, 1359.99)
        self.assertAlmostEqual(details.minimum_part_width_mm, 1426)
        self.assertAlmostEqual(details.minimum_width_mm, 1446.01)
        self.assertAlmostEqual(details.quarter_turn_minimum_width_mm, 1420.01)
        self.assertFalse(details.quarter_turn_fits)

    def test_individual_permission_fits_an_oblique_piece_and_rotates_its_notch(self):
        outline = tilt(rectangle(0, 0, 900, 80), -26.73, x=3000, y=1000)
        notch = tilt(contour([[450, -2], [450, 3]], closed=False), -26.73, x=3000, y=1000)
        paths = [{**outline, "partNumber": 23, "partRotation": "any"},
                 {**notch, "partNumber": 23},
                 {**rectangle(100, 100, 120, 65), "partNumber": 18, "partRotation": "none"}]
        result = pack(paths, width_mm=108, rotation="none")
        piece, locked = result["placements"]
        self.assertGreater(piece["angle"], 26)
        self.assertLess(piece["angle"], 28)
        self.assertEqual(locked["angle"], 0)
        self.assertEqual(piece["indices"], [0, 1])
        self.assertEqual(result["paths"][1]["partRotation"], "any")
        angle = radians(piece["angle"])
        old_x, old_y = paths[0]["points"][0]
        new_x, new_y = result["paths"][0]["points"][0]
        for old, placed in zip(paths[:2], result["paths"][:2]):
            for (x, y), (px, py) in zip(old["points"], placed["points"]):
                self.assertAlmostEqual(px, new_x + (x - old_x) * cos(angle) - (y - old_y) * sin(angle))
                self.assertAlmostEqual(py, new_y + (x - old_x) * sin(angle) + (y - old_y) * cos(angle))
                self.assertGreaterEqual(py, 10 - 1e-7)
                self.assertLessEqual(py, 98 + 1e-7)

    def test_unrestricted_rotation_improves_material_use_and_respects_individual_lock(self):
        tilted = tilt(rectangle(0, 0, 120, 500), 26.73, x=1000, y=1000)
        fixed = {**rectangle(4000, 1200, 160, 85), "partNumber": 18, "partRotation": "none"}
        unrotated = pack([tilted, fixed], width_mm=580, rotation="none")
        result = pack([tilted, fixed], width_mm=580, rotation="any")
        self.assertLess(result["lengthMm"], unrotated["lengthMm"])
        self.assertNotEqual(result["placements"][0]["angle"] % 90, 0)
        self.assertEqual(result["placements"][1]["angle"], 0)
        self.assertEqual(result, pack([tilted, fixed], width_mm=580, rotation="quarter"))
        with self.assertRaises(NestingFitError) as failure:
            pack([{**tilted, "partNumber": 18, "partRotation": "none"}], width_mm=490, rotation="any")
        self.assertFalse(failure.exception.quarter_turn_fits)

    def test_regular_parts_remain_complete_in_roll_plan(self):
        paths = [rectangle(5 * index, 2000 * index, 1350, 1000) for index in range(3)]
        paths.append(rectangle(0, 16000, 3200, 400))
        result = pack(paths)
        self.assertEqual(result["longPartCount"], 1)
        plan = prepare_cut_plan({"paths": result["paths"]}, 2500, 1650, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["mode"], "roll")
        self.assertEqual(len(plan["pages"]), result["pageCount"])
        self.assertEqual({seam["index"] for seam in plan["seams"]}, {3})
        self.assertTrue(isclose(sum(dist(a, b) for path in result["paths"]
                                    for a, b in zip(path["points"], path["points"][1:] + path["points"][:1])),
                                plan["cutDistanceMm"]))

    def test_feed_boundaries_do_not_leave_unused_material_after_wide_parts(self):
        paths = [rectangle(0, 2000 * index, 1400, 1400) for index in range(5)]
        result = pack(paths, width_mm=1450, gap_mm=3, margin_mm=0)
        plan = prepare_cut_plan({"paths": result["paths"]}, 2500, 1650, 0.12)
        self.assertEqual(result["lengthMm"], 7013.5)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["seams"], [])
        self.assertEqual(len(plan["pages"]), result["pageCount"])
        self.assertEqual([page["feedAfterMm"] for page in plan["pages"]], [1404.5, 1403, 1403, 1403, 0])

    def test_short_parts_can_use_space_beside_a_long_part(self):
        paths = [rectangle(0, 0, 240, 15)]
        paths.extend(rectangle(0, 100 * (index + 1), 55, 40) for index in range(3))
        result = pack(paths, width_mm=80, gap_mm=5, margin_mm=0, page_length_mm=99.88)
        plan = prepare_cut_plan({"paths": result["paths"]}, 100, 80, 0.12)
        self.assertEqual(result["lengthMm"], 242.5)
        self.assertEqual(plan["issues"], [])
        self.assertEqual({seam["index"] for seam in plan["seams"]}, {0})
        self.assertEqual(len(plan["pages"]), result["pageCount"])

    def test_pass_partition_packs_multiple_groups_alongside_overlength_part(self):
        dimensions = [(300, 15), (160, 20), (200, 50), (170, 20), (120, 30)]
        paths = [rectangle(0, index * 110, length, width)
                 for index, (length, width) in enumerate(dimensions)]
        result = pack(paths, width_mm=120, gap_mm=5, margin_mm=0, page_length_mm=245)
        self.assertAlmostEqual(result["lengthMm"], 367.5)
        plan = prepare_cut_plan({"paths": result["paths"]}, 250, 120, .12, roll_pass_length_mm=245)
        self.assertEqual((plan["issues"], len(plan["pages"]), result["pageCount"]), ([], 2, 2))
        self.assertEqual({seam["index"] for seam in plan["seams"]}, {0})
        self.assertTrue(all(page["feedAfterMm"] <= 245 for page in plan["pages"]))

    def test_maximal_free_rectangles_pack_short_parts_in_two_sections(self):
        paths = [rectangle(0, 130 * index, length, width)
                 for index, (length, width) in enumerate([(70, 90), (90, 60), (80, 50), (85, 15)])]
        result = pack(paths, width_mm=120, gap_mm=5, margin_mm=0, page_length_mm=149.88)
        self.assertLessEqual(result["lengthMm"], 182.5)
        plan = prepare_cut_plan({"paths": result["paths"]}, 150, 120, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["seams"], [])
        self.assertEqual(len(plan["pages"]), 2)

    def test_diagonal_parts_share_space_with_clearance(self):
        paths = [contour([[0, 0], [100, 0], [0, 100]]),
                 contour([[100, 300], [0, 300], [100, 200]])]
        result = pack(paths, width_mm=126, gap_mm=5)
        self.assertLess(result["lengthMm"], 125)
        first, second = result["placements"]
        # The closest edges run along parallel diagonals through the two bounding boxes.
        diagonal_clearance = (second["x"] + second["y"] - first["x"] - first["y"]) / 2**0.5
        self.assertGreaterEqual(diagonal_clearance, 5)
        plan = prepare_cut_plan({"paths": result["paths"], "materialWidthMm": 126, "materialMarginMm": 10},
                                2500, 126, 0.12)
        self.assertEqual(plan["issues"], [])
        self.assertGreater(plan["material"]["utilizationPercent"], 63)

    def test_allowed_half_turn_nests_identical_asymmetric_parts(self):
        paths = [contour([[0, 0], [100, 0], [0, 100]]),
                 contour([[0, 300], [100, 300], [0, 400]])]
        locked = pack(paths, width_mm=126, gap_mm=5, rotation="none")
        rotated = pack(paths, width_mm=126, gap_mm=5, rotation="any")
        self.assertLess(rotated["lengthMm"], 135)
        self.assertGreater(locked["lengthMm"], 210)
        self.assertTrue(all(part["angle"] == 0 for part in locked["placements"]))
        first, second = rotated["placements"]
        self.assertAlmostEqual(abs(first["angle"] - second["angle"]), 180)

    def test_concave_parts_use_the_recess_and_keep_a_valid_arrangement(self):
        outline = contour([[0, 0], [100, 0], [100, 25], [25, 25], [25, 100], [0, 100]])
        hole = contour([[5, 50], [10, 50], [10, 55], [5, 55]])
        mark = contour([[5, 60], [10, 60]], closed=False)
        settings = dict(width_mm=110, gap_mm=5, margin_mm=0, page_length_mm=119.88)
        for square_x in (30, 200):
            with self.subTest(square_x=square_x):
                paths = [outline, hole, mark, rectangle(square_x, square_x, 60, 60)]
                result = pack(paths, **settings)
                self.assertAlmostEqual(result["lengthMm"], 102.5)
                first, second = result["placements"]
                self.assertEqual(first["indices"], [0, 1, 2])
                self.assertGreaterEqual(second["x"] - first["x"], 30)
                self.assertGreaterEqual(second["y"] - first["y"], 30)
                if square_x == 30:
                    self.assertEqual(result["paths"][3]["points"][0], [32.5, 30])
                plan = prepare_cut_plan({"paths": result["paths"]}, 120, 110, .12)
                self.assertEqual((plan["issues"], len(plan["pages"])), ([], 1))

        roll = pack([outline, hole, mark, rectangle(200, 200, 60, 60)],
                    **{**settings, "page_length_mm": 99.88})
        plan = prepare_cut_plan({"paths": roll["paths"]}, 100, 110, .12)
        self.assertEqual((plan["issues"], len(plan["pages"])), ([], 2))

    def test_fragmented_concave_outline_keeps_clear_even_with_zero_gap(self):
        outline = [[0, 0], [100, 0], [100, 25], [25, 25], [25, 100], [0, 100]]
        fragments = [{**contour([first, last], closed=False), "partNumber": 1}
                     for first, last in zip(outline, outline[1:] + outline[:1])]
        # The original arrangement touches both arms and must be moved even at zero gap.
        square = {**rectangle(25, 25, 60, 60), "partNumber": 2}
        result = pack([*fragments, square], width_mm=110, gap_mm=0,
                      margin_mm=0, page_length_mm=119.88)
        first, second = result["placements"]
        self.assertAlmostEqual(result["lengthMm"], 100.01)
        self.assertEqual(first["indices"], list(range(6)))
        self.assertGreater(round(second["x"], 3) - round(first["x"], 3), 25)
        self.assertGreater(round(second["y"], 3) - round(first["y"], 3), 25)
        self.assertFalse(any(path["closed"] for path in result["paths"][:6]))

    def test_concave_layout_falls_back_for_unrecovered_or_invalid_outlines(self):
        points = [[0, 0], [100, 0], [100, 25], [25, 25], [25, 100], [0, 100]]
        exterior_mark = {**contour([[35, 40], [55, 40]], closed=False), "partNumber": 1}
        candidates = [
            ([contour(points, closed=False)], "open perimeter"),
            ([contour(points + [[99, 99]])], "self-intersection"),
            ([{**contour(points), "partNumber": 1}, exterior_mark], "cut outside perimeter"),
        ]
        for paths, reason in candidates:
            with self.subTest(reason=reason):
                result = pack([*paths, rectangle(200, 200, 60, 60)], width_mm=110, gap_mm=5,
                              margin_mm=0, page_length_mm=250)
                self.assertEqual(result["partCount"], 2)
                self.assertGreater(result["lengthMm"], 140)
                self.assertEqual(len(result["paths"]), len(paths) + 1)

    def test_degenerate_open_contour_cannot_keep_an_unsafe_saved_clearance(self):
        outline = {**contour([[0, 0], [100, 0], [100, 25], [25, 25], [25, 100], [0, 100]]),
                   "partNumber": 1}
        for y in (10, 25, 25.0001):
            with self.subTest(y=y):
                line = {**contour([[30, y], [90, y]], closed=False), "partNumber": 2}
                result = pack([outline, line], width_mm=110, gap_mm=0, margin_mm=0, page_length_mm=119.88)
                for x, placed_y in result["paths"][1]["points"]:
                    self.assertTrue(round(placed_y, 3) > 100 or (round(x - .01, 3) > 25 and round(placed_y, 3) > 25))


if __name__ == "__main__":
    unittest.main()
