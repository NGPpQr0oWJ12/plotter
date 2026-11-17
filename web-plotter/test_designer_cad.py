import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ezdxf
from ezdxf import units
from shapely.geometry import LineString, Point

from designer_cad import (CadDesignError, IMPORT_DOUBLED_LINE, IMPORT_DUPLICATE_POINTS, IMPORT_INVALID_VERTEX,
                          edit_design, import_design, prepare_design, validate_design)


def line(identifier, start, end):
    return {"id": identifier, "type": "line", "layer": "0", "start": start, "end": end}


def square():
    return {"id": "outline", "type": "polyline", "layer": "Крой", "closed": True,
            "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}


def square_edges(prefix, x=0):
    corners = [[a + x, b] for a, b in square()["points"]]
    return [line(f"{prefix}{index}", start, end)
            for index, (start, end) in enumerate(zip(corners, corners[1:] + corners[:1]))]


class DesignerCadTests(unittest.TestCase):
    def test_groups_survive_server_edits_without_affecting_cut_paths(self):
        contour = square()
        loose = line("loose", [40, 0], [60, 0])
        mark = {**line("mark", [0, 10], [2, 10]), "notch": True, "parentId": "outline"}
        other = line("other", [80, 0], [100, 0])
        nested = ["group", "nested"]
        entities = [{**contour, "groupPath": nested}, {**loose, "groupPath": ["group"]}, mark, other]
        self.assertEqual(validate_design(entities), entities)
        self.assertEqual(prepare_design(entities), prepare_design([contour, loose, mark, other]))

        outside = edit_design(entities, "trim-quick", {"stroke": [[50, 0]], "tolerance": 0.1, "groupPath": []})
        self.assertFalse(outside["changed"])
        inside = edit_design(entities, "trim-quick", {"stroke": [[50, 0]], "tolerance": 0.1, "groupPath": ["group"]})
        self.assertEqual(inside["entities"], [entities[0], mark, other])

        offset = edit_design(entities, "offset", {"id": "outline", "distance": 1})
        replacement = next(entity for entity in offset["entities"] if entity["id"] in offset["selectedIds"])
        self.assertEqual(replacement["groupPath"], nested)
        self.assertTrue(any(entity.get("parentId") == replacement["id"] for entity in offset["entities"]))

    def test_repair_small_gap_and_duplicate_clears_only_resolved_import_issue(self):
        shape = {**square(), "closed": False, "points": [[0, 0], [0, 0], [20, 0], [20, 20], [0, 20], [0, 0.01]],
                 "importIssues": [IMPORT_DUPLICATE_POINTS, IMPORT_INVALID_VERTEX]}
        result = edit_design([shape], "repair-part", {"ids": [shape["id"]]})
        repaired, = result["entities"]
        self.assertTrue(result["changed"])
        self.assertTrue(repaired["closed"])
        self.assertEqual(repaired["id"], shape["id"])
        self.assertEqual(repaired["points"], square()["points"])
        self.assertEqual(repaired["importIssues"], [IMPORT_INVALID_VERTEX])
        self.assertTrue(repaired["importDamaged"])
        self.assertIn("ручной проверки", result["warnings"][0])

    def test_repair_join_preserves_bulge_and_reattaches_existing_notch(self):
        arc = {"id": "arc", "type": "arc", "center": [10, 0], "radius": 10, "startAngle": 180, "sweep": 180}
        base = line("base", [20, 0], [0, 0])
        notch = {**line("notch", [8, 0], [8, -2]), "notch": True, "parentId": "base"}
        result = edit_design([arc, base, notch], "repair-part", {"ids": ["arc", "base"]})
        self.assertTrue(result["changed"])
        repaired, retained = result["entities"]
        self.assertTrue(repaired["closed"])
        self.assertAlmostEqual(repaired["bulges"][0], 1)
        self.assertEqual(retained, {**notch, "parentId": "arc"})
        self.assertEqual(result["selectedIds"], ["arc"])

    def test_close_outline_preserves_group_bulge_and_notch_ownership(self):
        group = ["parent", "panel"]
        arc = {"id": "arc", "type": "arc", "center": [10, 0], "radius": 10, "startAngle": 180, "sweep": 180,
               "groupPath": group}
        base = {**line("base", [20, 0], [0, 0]), "groupPath": group, "importIssues": [IMPORT_INVALID_VERTEX]}
        notch = {**line("notch", [8, 0], [8, -2]), "notch": True, "parentId": "base"}
        unrelated = {**line("unrelated", [50, 0], [60, 0]), "groupPath": ["other"]}

        result = edit_design([arc, base, notch, unrelated], "close-outline", {"ids": ["arc", "base"]})

        self.assertTrue(result["changed"])
        self.assertEqual(result["selectedIds"], ["arc"])
        contour, child, other = result["entities"]
        self.assertTrue(contour["closed"])
        self.assertEqual(contour["groupPath"], group)
        self.assertEqual(contour["importIssues"], [IMPORT_INVALID_VERTEX])
        self.assertAlmostEqual(contour["bulges"][0], 1)
        self.assertEqual(child, {**notch, "parentId": "arc"})
        self.assertEqual(other, unrelated)

    def test_close_outline_confirms_even_small_gaps_without_snapping_vertices(self):
        edges = square_edges("piece")
        edges[1]["start"] = [20, 0.01]
        edges[3]["start"] = [0, 19.8]
        ids = [edge["id"] for edge in edges]
        result = edit_design(edges, "close-outline", {"ids": ids})

        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], edges)
        issue, = result["diagnostics"]
        self.assertTrue(issue["fixable"])
        self.assertEqual(issue["kind"], "gap")
        self.assertEqual(issue["sourceOperation"], "close-outline")
        self.assertAlmostEqual(issue["distance"], 0.2)
        options = {"ids": ids, "kind": "gap", "sourceOperation": issue["sourceOperation"]}
        retry = edit_design(edges, "repair-conflict", {**options, "point": [0, 10]})
        self.assertFalse(retry["changed"])
        self.assertEqual(retry["diagnostics"], result["diagnostics"])

        confirmed = edit_design(edges, "repair-conflict", {**options, "point": issue["point"]})

        self.assertTrue(confirmed["changed"])
        contour, = confirmed["entities"]
        self.assertTrue(contour["closed"])
        self.assertEqual(set(map(tuple, contour["points"])),
                         {tuple(edge[side]) for edge in edges for side in ("start", "end")})

    def test_close_outline_no_ops_or_rejects_ineligible_contours(self):
        contour = square()
        open_loop = {**contour, "closed": False, "points": [*contour["points"], contour["points"][0]]}
        result = edit_design([open_loop], "close-outline", {"ids": [contour["id"]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [contour])

        crossed = {**contour, "points": [[0, 0], [20, 20], [0, 20], [20, 0]]}
        branch = [*square_edges("piece"), line("branch", [0, 0], [-10, 0])]
        unrelated_groups = square_edges("grouped")
        unrelated_groups[0]["groupPath"] = ["other"]
        for shapes in ([contour], [crossed], [line("short", [0, 0], [0.01, 0])], branch, unrelated_groups,
                       [line("first", [0, 0], [0, 20]), line("second", [40, 0], [40, 20])]):
            with self.subTest(ids=[shape["id"] for shape in shapes]):
                rejected = edit_design(shapes, "close-outline", {"ids": [shape["id"] for shape in shapes]})
                self.assertFalse(rejected["changed"])
                self.assertEqual(rejected["entities"], shapes)
                for diagnostic in rejected.get("diagnostics", ()):
                    self.assertEqual(diagnostic["sourceOperation"], "close-outline")

    def test_repair_reports_large_gap_and_self_intersection_without_changes(self):
        open_contour = {**square(), "closed": False,
                        "points": [[0, 0], [20, 0], [20, 20], [0, 20], [0, 6]]}
        crossed = {**square(), "points": [[0, 0], [20, 20], [0, 20], [20, 0]]}
        for shape, reason in ((open_contour, "6.000 мм"), (crossed, "самопересекается")):
            with self.subTest(reason=reason):
                result = edit_design([shape], "repair-part", {"ids": [shape["id"]]})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], [shape])
                self.assertIn(reason, result["warnings"][0])
                if shape is crossed:
                    self.assertEqual(result["diagnostics"], [{"point": [10, 10], "kind": "intersection"}])
                else:
                    self.assertNotIn("diagnostics", result)

    def test_confirmed_dxf_gap_keeps_endpoints_and_reduces_cut_trajectory(self):
        curve = [[index / 20, -math.sin(math.pi * index / 400)] for index in range(401)]
        points = [*curve, [20, 20], [0, 20], [0, .796]]
        shape = {**square(), "closed": False, "points": points}
        options = {"ids": [shape["id"]]}
        preview = edit_design([shape], "repair-part", options)
        issue, = preview["diagnostics"]
        self.assertFalse(preview["changed"])
        self.assertEqual(issue["endpoints"], [points[0], points[-1]])
        self.assertAlmostEqual(issue["distance"], .796)
        self.assertTrue(issue["fixable"])

        result = edit_design([shape], "repair-conflict", {**options, "kind": "gap", "point": issue["point"]})
        repaired, = result["entities"]
        path, = prepare_design([repaired])["paths"]
        self.assertTrue(result["changed"] and path["closed"])
        self.assertEqual([path["points"][0], path["points"][-1]], [points[0], points[-1]])
        self.assertLess(len(path["points"]), 40)
        boundary = LineString([*path["points"], path["points"][0]])
        self.assertLessEqual(boundary.hausdorff_distance(LineString([*points, points[0]])), .02)

    def test_gap_confirmation_refuses_crossing_geometry_and_stale_location(self):
        shape = {**square(), "closed": False, "points": [*square()["points"], [0, .796]]}
        crossing = line("crossing", [-1, .4], [1, .4])
        options = {"ids": [shape["id"]], "kind": "gap", "point": [0, .398]}
        preview = edit_design([shape, crossing], "repair-part", options)
        self.assertFalse(preview["diagnostics"][0]["fixable"])
        for entities, point in (([shape, crossing], [0, .398]), ([shape], [1, .398])):
            with self.subTest(entities=entities):
                result = edit_design(entities, "repair-conflict", {**options, "point": point})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], entities)

    def test_confirmed_bridges_join_fragments_across_two_gaps(self):
        fragments = [
            {**square(), "closed": False, "points": [[.6, 0], [20, 0], [20, 8]]},
            {**square(), "id": "top", "closed": False, "points": [[20, 8.796], [20, 20], [0, 20], [0, 0]]},
        ]
        options = {"ids": [entity["id"] for entity in fragments]}
        preview = edit_design(fragments, "repair-part", options)
        issue, = preview["diagnostics"]
        self.assertFalse(preview["changed"])
        self.assertTrue(issue["fixable"])
        self.assertAlmostEqual(issue["distance"], .796)
        result = edit_design(fragments, "repair-conflict", {**options, "kind": "gap", "point": issue["point"]})
        repaired, = result["entities"]
        self.assertTrue(result["changed"] and repaired["closed"])
        restored = LineString([*repaired["points"], repaired["points"][0]])
        self.assertTrue(restored.equals(LineString([*square()["points"], square()["points"][0]])))

    def test_repair_reports_exact_and_tolerance_based_branch_endpoint(self):
        for offset in (0, 0.008):
            with self.subTest(offset=offset):
                entities = [line("first", [0, 0], [10, 0]), line("second", [10 + offset, 0], [20, 0]),
                            line("branch", [10 + offset, 0], [10, 10])]
                result = edit_design(entities, "repair-part", {"ids": [entity["id"] for entity in entities]})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], validate_design(entities))
                self.assertIn("развилка", result["warnings"][0])
                self.assertEqual(result["diagnostics"], [{"point": [10, 0], "kind": "branch"}])

    def test_repair_conflict_removes_only_exact_duplicate_and_keeps_attached_native_id(self):
        edges = [line("bottom", [0, 0], [20, 0]), line("right", [20, 0], [20, 20]),
                 line("top", [20, 20], [0, 20]), line("left", [0, 20], [0, 0])]
        duplicate = line("copy", [20, 0], [0, 0])
        attached = {**line("notch", [10, 0], [10, 2]), "parentId": "copy", "notch": True}
        entities = [*edges, duplicate, attached]
        options = {"ids": [entity["id"] for entity in [*edges, duplicate]]}
        preview = edit_design(entities, "repair-part", options)
        issue, = preview["diagnostics"]
        self.assertEqual((issue["kind"], issue.get("fixable")), ("branch", True))
        self.assertEqual(preview["entities"], validate_design(entities))
        result = edit_design(entities, "repair-conflict", {**options, **issue})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], validate_design([*edges[1:], duplicate, attached]))
        self.assertEqual(prepare_design(result["entities"])["closedCount"], 1)

    def test_repair_conflict_removes_dangling_line_arc_or_polyline_and_keeps_main_curve(self):
        ring = [line("bottom", [0, 0], [20, 0]),
                {"id": "arc", "type": "arc", "center": [20, 10], "radius": 10, "startAngle": 270, "sweep": 180},
                line("top", [20, 20], [0, 20]), line("left", [0, 20], [0, 0])]
        branches = [line("tail", [0, 0], [-20, -20]),
                    {"id": "tail", "type": "arc", "center": [-20, 0], "radius": 20, "startAngle": 0, "sweep": -90},
                    {"id": "tail", "type": "polyline", "points": [[0, 0], [-8, -9], [-20, -11]]}]
        for branch in branches:
            with self.subTest(kind=branch["type"]):
                entities = [{**entity, "groupPath": ["part"]} for entity in [*ring, branch]]
                options = {"ids": [entity["id"] for entity in entities]}
                issue, = edit_design(entities, "repair-part", options)["diagnostics"]
                self.assertTrue(issue["fixable"])
                result = edit_design(entities, "repair-conflict", {**options, **issue})
                self.assertTrue(result["changed"])
                self.assertEqual(result["entities"], validate_design(entities[:-1]))

    def test_repair_conflict_refuses_plausible_notch_and_geometry_on_dangling_branch(self):
        ring = [line("bottom", [0, 0], [20, 0]), line("right", [20, 0], [20, 20]),
                line("top", [20, 20], [0, 20]), line("left", [0, 20], [0, 0])]
        tail = line("tail", [0, 0], [-20, -20])
        cases = [[line("tail", [0, 0], [-2, -2])], [line("tail", [0, 0], [18, 18])],
                 [{**tail, "importIssues": [IMPORT_DOUBLED_LINE]}],
                 [tail, line("crosses-tail", [-15, -16], [-15, -14])],
                 [tail, {**line("notch", [-3, -3], [-3, -2]), "parentId": "tail", "notch": True}]]
        for extras in cases:
            with self.subTest(extras=extras):
                entities = [*ring, *extras]
                options = {"ids": [entity["id"] for entity in [*ring, extras[0]]]}
                issue, = edit_design(entities, "repair-part", options)["diagnostics"]
                self.assertNotIn("fixable", issue)
                result = edit_design(entities, "repair-conflict", {**options, **issue})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], validate_design(entities))

    def test_repair_conflict_never_chooses_between_two_distinct_outer_paths(self):
        entities = [line("bottom", [0, 0], [20, 0]), line("right", [20, 0], [20, 20]),
                    line("top", [20, 20], [0, 20]), line("left", [0, 20], [0, 0]),
                    {"id": "alternate", "type": "arc", "center": [10, 0], "radius": 10,
                     "startAngle": 180, "sweep": 180}]
        options = {"ids": [entity["id"] for entity in entities]}
        issue, = edit_design(entities, "repair-part", options)["diagnostics"]
        self.assertNotIn("fixable", issue)
        result = edit_design(entities, "repair-conflict", {**options, **issue})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design(entities))

    def test_repair_conflict_keeps_one_exact_traversal_and_preserves_bound_notch(self):
        outline = {**square(), "points": [[0, 0], [20, 0], [0, 0], [20, 0], [20, 20], [0, 20]],
                   "groupPath": ["part"], "bulges": [0.5, -0.5, 0.5, 0, 0, 0]}
        notch = {**line("notch", [10, -5], [10, -3]), "parentId": "outline", "notch": True}
        options = {"ids": [outline["id"]]}
        diagnosis = edit_design([outline, notch], "repair-part", options)["diagnostics"]
        issue = next(item for item in diagnosis if item["kind"] == "overlap")
        self.assertTrue(issue["fixable"])
        result = edit_design([outline, notch], "repair-conflict", {**options, **issue})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [{**outline, "points": square()["points"], "bulges": [0.5, 0, 0, 0]}, notch])

    def test_repair_conflict_never_removes_a_retraced_slit_without_retained_edge(self):
        outline = {**square(), "points": [[0, 0], [10, 0], [10, 2], [10, 0], [20, 0], [20, 20], [0, 20]]}
        options = {"ids": [outline["id"]]}
        issue = next(item for item in edit_design([outline], "repair-part", options)["diagnostics"] if item["kind"] == "overlap")
        self.assertNotIn("fixable", issue)
        result = edit_design([outline], "repair-conflict", {**options, **issue})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design([outline]))

    def test_bulk_repair_fixes_multiple_conflicts_on_two_native_rings(self):
        first = square_edges("first-")
        second = square_edges("second-", 100)
        extras = [line("duplicate-bottom", [20, 0], [0, 0]), line("duplicate-right", [20, 0], [20, 20]),
                  line("lower-branch", [0, 0], [-20, -20]), line("upper-branch", [20, 20], [40, 40]),
                  line("duplicate-second", [120, 0], [100, 0])]
        entities = [*first, *extras[:4], *second, extras[-1]]
        result = edit_design(entities, "repair-conflicts", {"ids": [entity["id"] for entity in entities]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], validate_design([*first, *second]))
        self.assertEqual(result["selectedIds"], [entity["id"] for entity in [*first, *second]])
        self.assertEqual(prepare_design(result["entities"])["closedCount"], 2)
        self.assertNotIn("diagnostics", result)
        self.assertIn("5", result["warnings"][0])

    def test_bulk_repair_preserves_protected_branch_and_independent_tolerance_branch(self):
        ring = square_edges("ring-")
        spur = line("long", [0, 0], [-20, -20])
        mark = line("short-mark", [0, 0], [-2, 0])
        tolerance = [line("one", [60, 0], [70, 0]), line("two", [70.008, 0], [85, 0]),
                     line("three", [70.008, 0], [70, 10])]
        entities = [*ring, spur, mark, *tolerance]
        result = edit_design(entities, "repair-conflicts", {"ids": [entity["id"] for entity in entities]})
        expected = [*ring, mark, *tolerance]
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], validate_design(expected))
        self.assertEqual(result["diagnostics"], [
            {"point": [0, 0], "kind": "branch", "ids": [entity["id"] for entity in [*ring, mark]]},
            {"point": [70, 0], "kind": "branch", "ids": [entity["id"] for entity in tolerance]},
        ])
        self.assertEqual(result["diagnosticIds"], [entity["id"] for entity in expected])

    def test_bulk_repair_reduces_multiple_exact_retraces_without_touching_bowtie(self):
        outline = {**square(), "points": [[0, 0], [20, 0], [0, 0], [20, 0], [20, 20], [0, 20], [20, 20], [0, 20]],
                   "groupPath": ["part"], "bulges": [0.5, -0.5, 0.5, 0, 0.25, -0.25, 0.25, 0]}
        notch = {**line("notch", [10, -5], [10, -3]), "parentId": "outline", "notch": True}
        crossed = {**square(), "id": "crossed", "points": [[50, 0], [70, 20], [50, 20], [70, 0]]}
        result = edit_design([outline, notch, crossed], "repair-conflicts", {"ids": [outline["id"], crossed["id"]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [
            {**outline, "points": square()["points"], "bulges": [0.5, 0, 0.25, 0]}, notch, crossed,
        ])
        self.assertEqual(result["diagnostics"], [{"point": [60, 10], "kind": "intersection", "ids": [crossed["id"]]}])
        self.assertEqual(result["diagnosticIds"], [crossed["id"]])

    def test_bulk_repair_retains_retry_ids_beyond_visible_diagnostic_limit(self):
        crossed = [{**square(), "id": str(index), "points": [[x, 0], [x + 20, 20], [x, 20], [x + 20, 0]]}
                   for index in range(17) for x in [index * 50]]
        ids = [entity["id"] for entity in crossed]
        result = edit_design(crossed, "repair-conflicts", {"ids": ids})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design(crossed))
        self.assertEqual(len(result["diagnostics"]), 16)
        self.assertEqual(result["diagnosticIds"], ids)
        self.assertIn("Показано 16 из 17.", result["warnings"])

    def test_repair_reports_only_real_conflicts_at_overlaid_edges(self):
        outline = {**square(), "points": [[0, 0], [20, 0], [10, 0], [10, 10], [0, 10]]}
        result = edit_design([outline], "repair-part", {"ids": [outline["id"]]})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design([outline]))
        diagnostics = {(tuple(item["point"]), item["kind"]) for item in result["diagnostics"]}
        self.assertEqual(diagnostics, {((15, 0), "overlap"), ((10, 0), "intersection")})
        healthy = edit_design([square()], "repair-part", {"ids": [outline["id"]]})
        self.assertNotIn("diagnostics", healthy)

    def test_repair_locates_crossing_and_overlap_on_exact_bulge_geometry(self):
        arc_crossing = {**square(), "points": [[-10, 0], [10, 0], [4, 0], [4, -15], [-10, -15]],
                        "bulges": [1, 0, 0, 0, 0]}
        crossing_result = edit_design([arc_crossing], "repair-part", {"ids": [arc_crossing["id"]]})
        self.assertFalse(crossing_result["changed"])
        self.assertTrue(any(item["kind"] == "intersection" and math.dist(item["point"], [4, -math.sqrt(84)]) < 1e-9
                            for item in crossing_result["diagnostics"]))
        overlapping = {**square(), "points": [[-10, 0], [10, 0], [0, -10], [0, -15], [-10, -15]],
                       "bulges": [1, -math.tan(math.pi / 8), 0, 0, 0]}
        overlap_result = edit_design([overlapping], "repair-part", {"ids": [overlapping["id"]]})
        self.assertFalse(overlap_result["changed"])
        self.assertTrue(any(item["kind"] == "overlap" and math.dist(item["point"], [math.sqrt(50), -math.sqrt(50)]) < 1e-9
                            for item in overlap_result["diagnostics"]))

    def test_repair_small_corner_loop_at_seam_preserves_bulge_and_notch(self):
        shape = {**square(), "points": [[20, -0.05], [20, 20], [0, 20], [0, 0], [20.04, 0]],
                 "bulges": [0, 0.25, 0, 0, 0], "groupPath": ["group"], "reference": True, "joined": True}
        notch = {**line("notch", [12, 0], [12, 2]), "notch": True, "parentId": shape["id"], "reference": True}
        result = edit_design([shape, notch], "repair-part", {"ids": [shape["id"]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["selectedIds"], [shape["id"]])
        self.assertEqual(result["entities"], [{**shape, "points": [[20, 0], [20, 20], [0, 20], [0, 0]],
                                              "bulges": [0, 0.25, 0, 0]}, notch])
        self.assertTrue(any("петля" in warning and "замкнутым" in warning for warning in result["warnings"]))

    def test_repair_corner_loop_from_separate_rotated_segments_uses_exact_intersection(self):
        rotation = math.radians(31)

        def transformed(point):
            x, y = point
            return [50_000 + x * math.cos(rotation) - y * math.sin(rotation),
                    -40_000 + x * math.sin(rotation) + y * math.cos(rotation)]

        # The two tips are closer to one another than the normal manual join tolerance.
        points = [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20]]
        fragments = [line(str(index), transformed(start), transformed(end))
                     for index, (start, end) in enumerate(zip(points, points[1:] + points[:1]))]
        result = edit_design(fragments, "repair-part", {"ids": [fragment["id"] for fragment in fragments]})
        self.assertTrue(result["changed"])
        outline, = result["entities"]
        self.assertTrue(outline["closed"])
        self.assertTrue(outline["joined"])
        self.assertEqual(outline["id"], fragments[0]["id"])
        self.assertEqual(len(outline["points"]), 4)
        for corner in square()["points"]:
            self.assertLess(min(math.dist(point, transformed(corner)) for point in outline["points"]), 2e-6)

    def test_repair_corner_loop_in_open_flag_with_repeated_start(self):
        outline = {**square(), "closed": False,
                   "points": [[0, 0], [20.004, 0], [20, -0.005], [20, 20], [0, 20], [0, 0]]}
        result = edit_design([outline], "repair-part", {"ids": [outline["id"]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [{**outline, "closed": True, "points": square()["points"]}])

    def test_explicit_repair_reconnects_cut_corner_at_support_line_intersection(self):
        for first, last in (([20, -0.05], [20.04, 0]), ([20, 0.018], [20.018, 0]),
                            ([20, 0.018], [19.982, 0])):
            with self.subTest(first=first, last=last):
                outline = {**square(), "closed": False, "joined": True, "groupPath": ["group"],
                           "points": [first, [20, 20], [0, 20], [0, 0], last], "bulges": [0, .25, 0, 0, 0]}
                notch = {**line("notch", [12, 0], [12, 2]), "parentId": outline["id"], "notch": True}
                result = edit_design([outline, notch], "repair-part", {"ids": [outline["id"]]})
                self.assertTrue(result["changed"])
                self.assertEqual(result["entities"], [{**outline, "closed": True, "points": [[20, 0], *outline["points"][1:-1]],
                                                       "bulges": outline["bulges"][:-1]}, notch])

    def test_explicit_corner_repair_keeps_large_or_protected_open_seams(self):
        shape = {**square(), "closed": False, "points": [[20, .018], [20, 20], [0, 20], [0, 0], [20.018, 0]]}
        long = {**shape, "points": [[20, .021], *shape["points"][1:]]}
        attachment = {**line("notch", shape["points"][-1], [20.018, -1]), "parentId": shape["id"], "notch": True}
        for entities in ([long], [shape, attachment]):
            with self.subTest(entities=entities):
                result = edit_design(entities, "repair-part", {"ids": [shape["id"]]})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], validate_design(entities))

    def test_bulk_repair_keeps_loop_with_attached_connector_and_repairs_independent_loop(self):
        points = [[0, 0], [20.04, 0], [20, -.05], [20, 20], [0, 20]]
        ring_edges = [line(f"edge-{index}", a, b) for index, (a, b) in enumerate(zip(points, points[1:] + points[:1]))]
        attached = {**line("attached", [20.02, -.025], [21, -1]), "notch": True, "parentId": "edge-1"}
        shape = {**square(), "points": [[x+40, y] for x, y in points], "bulges": [0, 0, 0, .25, 0],
                 "reference": True, "groupPath": ["group"], "joined": True}
        result = edit_design([*ring_edges, shape, attached], "repair-conflicts",
                             {"ids": [entity["id"] for entity in [*ring_edges, shape]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [*ring_edges, {**shape, "points": [[40, 0], [60, 0], [60, 20], [40, 20]],
                                                            "bulges": [0, 0, .25, 0]}, attached])
        self.assertIn("Удалено малых петель в углах — 1. Контуры остались замкнутыми.", result["warnings"])
        self.assertEqual(result["diagnosticIds"], [entity["id"] for entity in ring_edges])

    def test_repair_corner_loop_keeps_ambiguous_large_and_connected_overruns(self):
        shape = {**square(), "points": [[0, 0], [20.04, 0], [20, -0.05], [20, 20], [0, 20]]}
        large = {**shape, "points": [[0, 0], [20.11, 0], [20, -0.05], [20, 20], [0, 20]]}
        multiple = {**shape, "points": [[0, 0], [20.04, 0], [20, -0.05], [20, 20], [-0.04, 20], [0, 20.05]]}
        curved_connector = {**shape, "bulges": [0, 0.2, 0, 0, 0]}
        short_side = {**shape, "points": [[19.9, 0], [20.04, 0], [20, -0.05], [20, 20], [19.9, 20]]}
        open_contour = {**shape, "closed": False}
        attachment = line("connection", [20.04, 0], [22, 0])
        notch = {**line("notch", [20.04, 0], [20.04, 1]), "notch": True, "parentId": shape["id"]}
        for contours in ([large], [multiple], [curved_connector], [short_side], [open_contour], [shape, attachment], [shape, notch]):
            with self.subTest(contours=contours):
                result = edit_design(contours, "repair-part", {"ids": [shape["id"]]})
                self.assertFalse(result["changed"])
                self.assertEqual(result["entities"], validate_design(contours))

    def test_repair_removes_collinear_subdivisions_and_keeps_cut_geometry(self):
        corners = square()["points"]
        points = [[a + (b - a) * index / 80 for a, b in zip(start, end)]
                  for start, end in zip(corners, corners[1:] + corners[:1]) for index in range(80)]
        shape = {**square(), "points": points, "groupPath": ["group"], "joined": True,
                 "importDamaged": True, "importIssues": [IMPORT_INVALID_VERTEX]}
        notch = {**line("notch", [7.75, 0], [7.75, 1]), "notch": True, "parentId": shape["id"]}
        result = edit_design([shape, notch], "repair-part", {"ids": [shape["id"]]})
        self.assertTrue(result["changed"])
        self.assertEqual(result["entities"], [{**shape, "points": corners}, notch])
        self.assertEqual(result["selectedIds"], [shape["id"]])
        self.assertIn("316", result["warnings"][-1])

    def test_simplify_dense_sample_keeps_corners_and_tight_distance(self):
        base = [[index / 25, 0.008 * math.sin(index * math.tau / 500)] for index in range(1, 500)]
        points = [[0, 0], *base, [20, 0], [20, 20], [12.005, 20], [12.005, 20.006],
                  [12, 20.006], [12, 20], [0, 20]]
        shape = {**square(), "points": points, "joined": True, "reference": True, "groupPath": ["group"]}
        result = edit_design([shape], "simplify", {"ids": [shape["id"]]})
        reduced, = result["entities"]
        self.assertTrue(result["changed"])
        self.assertEqual(result["selectedIds"], [shape["id"]])
        self.assertEqual(reduced, {**shape, "points": [points[0], *points[500:]]})
        original_path = LineString([*points, points[0]])
        reduced_path = LineString([*reduced["points"], reduced["points"][0]])
        self.assertTrue(reduced_path.is_simple)
        self.assertLessEqual(original_path.hausdorff_distance(reduced_path), 0.02)
        repaired = edit_design([shape], "repair-part", {"ids": [shape["id"]]})["entities"][0]
        self.assertGreaterEqual(len(repaired["points"]), len(points) - 1)
        self.assertLessEqual(original_path.hausdorff_distance(LineString([*repaired["points"], points[0]])), 1e-12)

    def test_simplify_keeps_notch_geometry_and_skips_bulged_or_crossed_outlines(self):
        shape = {**square(), "points": [[0, 0], [5, 0.008], [15, 0.009], [20, 0], [20, 20], [0, 20]]}
        notch = {**line("notch", [5, 0.008], [5, 1]), "notch": True, "parentId": shape["id"]}
        bulged = {**shape, "id": "curved", "bulges": [0, 0.6, 0, 0, 0, 0]}
        crossed = {**shape, "id": "crossed", "points": [[0, 0], [5, 5], [20, 20], [0, 20], [20, 0]]}
        contours = [shape, notch, bulged, crossed]
        result = edit_design(contours, "simplify", {"ids": ["outline", "curved", "crossed"]})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design(contours))

    def test_simplify_preserves_connected_line_endpoints_without_notch_metadata(self):
        shape = {**square(), "points": [[0, 0], [5, 0.008], [15, 0.009], [20, 0], [20, 20], [0, 20]]}
        mark = line("mark", [5, 0.008], [5, 1.008])
        result = edit_design([shape, mark], "simplify", {"ids": [shape["id"]]})
        self.assertFalse(result["changed"])
        self.assertEqual(result["entities"], validate_design([shape, mark]))

    def test_simplify_linear_runs_preserves_mixed_outline_bulges_exactly(self):
        shape = {**square(), "points": [[0, 0], [5, 0], [10, 0], [20, 0], [20, 20], [0, 20], [0, 10]],
                 "bulges": [0, 0, 0, 0, 0.25, 0, 0]}
        expected = {**square(), "bulges": [0, 0, 0.25, 0]}
        for operation in ("repair-part", "simplify"):
            with self.subTest(operation=operation):
                result = edit_design([shape], operation, {"ids": [shape["id"]]})
                self.assertTrue(result["changed"])
                self.assertEqual(result["entities"], [expected])

    def test_import_review_marker_survives_validation_and_join(self):
        first = line("first", [0, 0], [10, 0])
        recovered = {**line("recovered", [10, 0], [20, 0]), "importDamaged": True}
        self.assertEqual(validate_design([first, recovered]), [first, recovered])
        result = edit_design([first, recovered], "join", {"ids": ["first", "recovered"]})
        self.assertTrue(result["entities"][0]["importDamaged"])

    def test_scale_from_fixed_side_keeps_unselected_objects_and_scales_attached_notch(self):
        outline = square()
        mark = {**line("mark", [20, 8], [18, 8]), "notch": True, "parentId": "outline"}
        other = line("other", [30, 0], [40, 0])
        result = edit_design([outline, mark, other], "scale", {"ids": ["outline"], "origin": [0, 10], "sx": 1.5, "sy": 1})
        new_outline, new_mark, untouched = result["entities"]
        self.assertEqual(new_outline["points"], [[0, 0], [30, 0], [30, 20], [0, 20]])
        self.assertEqual((new_mark["start"], new_mark["end"], new_mark["parentId"]), ([30, 8], [27, 8], "outline"))
        self.assertEqual(untouched, other)
        self.assertEqual(result["selectedIds"], ["outline"])

    def test_scale_keeps_circles_and_arcs_analytic(self):
        arc = {"id": "arc", "type": "arc", "center": [2, 0], "radius": 5, "startAngle": 90, "sweep": -180}
        options = {"ids": ["arc"], "origin": [0, 0], "sx": 2, "sy": 2}
        uniform, = edit_design([arc], "scale", options)["entities"]
        self.assertEqual((uniform["type"], uniform["center"], uniform["radius"], uniform["sweep"]), ("arc", [4, 0], 10, -180))
        ellipse, = edit_design([arc], "scale", {**options, "sy": 1})["entities"]
        self.assertEqual((ellipse["type"], ellipse["center"], ellipse["axis"], ellipse["ratio"]), ("ellipse", [4, 0], [10, 0], .5))
        self.assertAlmostEqual(ellipse["startAngle"], 90)
        self.assertAlmostEqual(ellipse["sweep"], -180)

    def test_one_axis_scale_samples_bulges_and_retains_closed_shape(self):
        outline = {**square(), "bulges": [1, 0, 0, 0]}
        result = edit_design([outline], "scale", {"ids": ["outline"], "origin": [0, 0], "sx": 1, "sy": 2})
        curve, = result["entities"]
        self.assertTrue(curve["closed"] and result["warnings"])
        self.assertNotIn("bulges", curve)
        self.assertIn([20, 40], curve["points"])
        self.assertAlmostEqual(min(y for _, y in curve["points"]), -20, delta=.05)

    def import_document(self, document, fmt="asc"):
        with TemporaryDirectory() as directory:
            drawing = Path(directory) / "чертеж.dxf"
            document.saveas(drawing, fmt=fmt)
            data = drawing.read_bytes()
        return import_design(data, drawing.name)

    def test_native_entities_and_stable_ids_in_binary_dxf(self):
        document = ezdxf.new(units=units.IN)
        document.layers.new("Hidden").off()
        model = document.modelspace()
        model.add_line((0, 0), (1, 0))
        model.add_circle((1, 1), 0.25)
        model.add_arc((0, 0), 1, 350, 260)
        model.add_ellipse((0, 0), major_axis=(3, 0), ratio=0.5)
        model.add_lwpolyline([(0, 0, 0), (1, 0, 0), (1, 1, -0.5)], format="xyb", close=True)
        model.add_polyline2d([(2, 0), (3, 1)])
        model.add_rational_spline([(1, 0), (1, 1), (0, 1)], [1, math.sqrt(0.5), 1], degree=2)
        model.add_line((0, 0, 0), (0, 1, 1))
        model.add_line((0, 0), (1, 0), dxfattribs={"layer": "Hidden"})

        drawing = self.import_document(document, "bin")
        entities = drawing["entities"]
        self.assertEqual(entities, self.import_document(document, "bin")["entities"])
        self.assertEqual([entity["type"] for entity in entities],
                         ["line", "circle", "arc", "ellipse", "polyline", "polyline", "spline"])
        self.assertEqual(drawing["units"], "mm")
        self.assertAlmostEqual(entities[0]["end"][0], 25.4)
        self.assertAlmostEqual(entities[1]["radius"], 6.35)
        self.assertEqual(entities[2]["startAngle"], 350)
        self.assertEqual(entities[2]["sweep"], 270)
        self.assertAlmostEqual(entities[3]["ratio"], 0.5)
        self.assertEqual(entities[4]["bulges"], [0, 0, -0.5])
        self.assertTrue(entities[4]["closed"])
        self.assertEqual(entities[6]["degree"], 2)
        self.assertEqual(entities[6]["weights"], [1, math.sqrt(0.5), 1])
        self.assertIn("скрытые объекты", drawing["warnings"][0])
        self.assertIn("вне плоскости XY", drawing["warnings"][0])
        paths = prepare_design(entities)["paths"]
        self.assertAlmostEqual(math.hypot(*paths[-1]["points"][len(paths[-1]["points"]) // 2]), 25.4, places=5)

    def test_headerless_dxf_defaults_to_millimetres(self):
        data = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\n0\n20\n0\n11\n50\n21\n25\n0\nENDSEC\n0\nEOF\n"
        drawing = import_design(data, "detail.dxf")
        self.assertEqual(drawing["entities"][0]["end"], [50, 25])
        self.assertEqual(drawing["warnings"], [])

    def test_explicit_meter_units_scale_in_ascii_and_binary_dxf(self):
        document = ezdxf.new(units=units.M)
        document.modelspace().add_line((0, 0), (0.05, 0.025))
        for fmt in ("asc", "bin"):
            with self.subTest(fmt=fmt):
                drawing = self.import_document(document, fmt)
                self.assertEqual(drawing["entities"][0]["end"], [50, 25])
                self.assertEqual(drawing["warnings"], [])

    def test_block_reflection_and_nonuniform_arc_stay_editable(self):
        document = ezdxf.new(units=units.MM)
        document.layers.new("Крой")
        block = document.blocks.new("Detail")
        block.add_arc((0, 0), 10, 0, 270)
        block.add_lwpolyline([(0, 0, 1), (20, 0, 0)], format="xyb")
        model = document.modelspace()
        model.add_blockref("Detail", (10, 20), dxfattribs={"xscale": -1, "layer": "Крой"})
        model.add_blockref("Detail", (0, 0), dxfattribs={"xscale": 2, "yscale": 0.5})

        entities = self.import_document(document)["entities"]
        reflected, reflected_polyline, ellipse, bulged_ellipse = entities
        self.assertEqual(reflected["center"], [10, 20])
        self.assertEqual(reflected["sweep"], -270)
        self.assertEqual(reflected["layer"], "Крой")
        self.assertEqual(reflected_polyline["bulges"], [-1, 0])
        self.assertEqual(ellipse["type"], "ellipse")
        self.assertAlmostEqual(ellipse["ratio"], 0.25)
        self.assertEqual(bulged_ellipse["type"], "ellipse")
        paths = prepare_design(entities)["paths"]
        self.assertEqual(paths[2]["points"][0], [20, 0])
        self.assertEqual(paths[2]["points"][-1], [0, -5])
        self.assertAlmostEqual(min(y for _, y in paths[3]["points"]), -5, delta=0.05)

    def test_preparation_preserves_gap_and_excludes_reference_notch_family(self):
        reference = {**square(), "reference": True}
        notch = {**line("notch", [5, 0], [5, 1]), "notch": True, "parentId": reference["id"]}
        fragment = line("fragment", [30, 30], [35, 30])
        result = prepare_design([reference, notch, fragment])
        self.assertEqual(result["paths"], [{"points": [[30, 30], [35, 30]], "closed": False, "layer": "0"}])

    def test_cut_sampling_keeps_curves_connected_with_finer_detail(self):
        radius = 1200
        arc = {"id": "a", "type": "arc", "center": [0, 0], "radius": radius, "startAngle": 0, "sweep": 150}
        (arc_path,) = prepare_design([arc])["paths"]
        points = arc_path["points"]
        self.assertFalse(arc_path["closed"])
        self.assertEqual(points[0], [radius, 0])
        self.assertAlmostEqual(points[-1][0], -radius * math.sqrt(3) / 2, places=6)
        self.assertEqual(points[-1][1], radius / 2)
        for start, end in zip(points, points[1:]):
            midpoint = [(a + b) / 2 for a, b in zip(start, end)]
            self.assertLessEqual(radius - math.hypot(*midpoint), 0.010001)

        spline = {"id": "s", "type": "spline", "fitPoints": [[0, 0], [10, 10], [20, 0]]}
        (spline_path,) = prepare_design([spline])["paths"]
        self.assertFalse(spline_path["closed"])
        self.assertIn([10, 10], spline_path["points"])
        self.assertEqual(spline_path["points"][-1], [20, 0])
        segments = LineString(spline_path["points"])
        for t in (index / 200 for index in range(201)):
            sample = Point(5 * t + 10 * t**2 - 5 * t**3, 5 * t + 20 * t**2 - 15 * t**3)
            self.assertLessEqual(segments.distance(sample), 0.0102)

    def test_cut_sampling_retains_existing_point_budget(self):
        arc = {"id": "a", "type": "arc", "center": [0, 0], "radius": 1200, "startAngle": 0, "sweep": 150}
        # High detail fits either arc on its own, but both require the legacy step to share the budget.
        other = {**arc, "id": "b", "center": [3000, 0]}
        full = prepare_design([arc, other])["paths"]
        with patch("designer_cad.MAX_POINTS", 400):
            fallback = prepare_design([arc, other])["paths"]
        self.assertEqual(len(fallback), 2)
        self.assertLess(sum(len(path["points"]) for path in fallback), 400)
        for detailed, coarse in zip(full, fallback):
            self.assertGreater(len(detailed["points"]), len(coarse["points"]))
            self.assertEqual([detailed["points"][0], detailed["points"][-1]], [coarse["points"][0], coarse["points"][-1]])

    def test_preparation_handles_authored_closed_spline_and_signed_arc(self):
        spline = {"id": "s", "type": "spline", "closed": True,
                  "fitPoints": [[0, 0], [6, 0], [6, 6], [0, 6]]}
        arc = {"id": "a", "type": "arc", "center": [0, 0],
               "radius": 10, "startAngle": 0, "sweep": -270}
        spline_path, arc_path = prepare_design([spline, arc])["paths"]
        self.assertTrue(spline_path["closed"])
        self.assertIn([6, 6], spline_path["points"])
        self.assertFalse(arc_path["closed"])
        self.assertEqual(arc_path["points"][0], [10, 0])
        self.assertEqual(arc_path["points"][-1], [0, 10])
        self.assertLess(arc_path["points"][1][1], 0)

    def test_centripetal_spline_passes_through_uneven_fit_points(self):
        points = [[0, 0], [0.2, 0.1], [2, 8], [25, 4], [25.1, 4.3]]
        spline = {"id": "s", "type": "spline", "fitPoints": points, "fitMethod": "centripetal"}
        point = {"id": "construction", "type": "point", "layer": "0", "point": [5, 7], "reference": True}
        paths = prepare_design([spline, point])["paths"]
        self.assertEqual(len(paths), 1)
        self.assertTrue(all(value in paths[0]["points"] for value in points))
        legacy = prepare_design([{key: value for key, value in spline.items() if key != "fitMethod"}])["paths"]
        self.assertNotEqual(paths, legacy)

    def test_offset_retains_reference_and_moves_inward_notch(self):
        outline = {**square(), "groupPath": ["part", "outline-group"]}
        added = edit_design([outline], "notch", {"id": "outline", "point": [8, 1], "depth": 3})
        self.assertEqual(added["entities"][-1]["end"], [8, 3])
        offset = edit_design(added["entities"], "offset", {"id": "outline", "distance": 2})
        old, old_notch, new, new_notch = offset["entities"]
        self.assertTrue(old["reference"])
        self.assertTrue(old_notch["reference"])
        self.assertEqual(offset["selectedIds"], [new["id"]])
        self.assertTrue(new["joined"])
        self.assertTrue(new["closed"])
        self.assertEqual(new["groupPath"], outline["groupPath"])
        self.assertNotIn("reference", new)
        self.assertEqual(new_notch["parentId"], new["id"])
        self.assertEqual(new_notch["start"], [8, -2])
        self.assertEqual(new_notch["end"], [8, 1])
        cut = prepare_design(offset["entities"])["paths"]
        self.assertEqual(len(cut), 2)
        self.assertEqual(min(x for x, _ in cut[0]["points"]), -2)
        self.assertTrue(cut[1]["notch"])

    def test_offset_preserves_analytic_circle_and_handles_collapse(self):
        circle = {"id": "circle", "type": "circle", "center": [2, 3], "radius": 5}
        options = {"id": "circle", "distance": -2, "keepOriginal": False}
        new, = edit_design([circle], "offset", options)["entities"]
        self.assertEqual((new["type"], new["center"], new["radius"]), ("circle", [2, 3], 3))
        with self.assertRaises(CadDesignError):
            edit_design([circle], "offset", {**options, "distance": -5})

    def test_offset_connected_chain_has_one_contour_and_a_stable_anchor_sign(self):
        group = ["piece"]
        base = {**line("base", [0, 0], [10, 0]), "groupPath": group}
        reversed_side = {**line("side", [10, 10], [10, 0]), "groupPath": group}
        notch = {**line("mark", [4, 0], [4, -1]), "notch": True, "parentId": "base"}
        entities = [base, reversed_side, notch]
        ids = ["base", "side"]

        self.assertEqual(edit_design(entities, "offset-anchor", {"ids": ids, "anchorId": "base"}),
                         {"normalSign": 1, "closed": False})
        self.assertEqual(edit_design(entities, "offset-anchor", {"ids": ids, "anchorId": "side"}),
                         {"normalSign": -1, "closed": False})

        result = edit_design(entities, "offset", {"ids": ids, "distance": 2, "keepOriginal": True})
        offset_id, = result["selectedIds"]
        self.assertEqual(result["originalIds"], ids)
        self.assertEqual(result["entities"][:3], [{**entity, "reference": True} for entity in entities])
        outline, new_notch = result["entities"][3:]
        self.assertEqual(outline["id"], offset_id)
        self.assertEqual(outline["groupPath"], group)
        self.assertEqual(outline["points"], [[0, 2], [8, 2], [8, 10]])
        self.assertFalse(outline["closed"])
        self.assertEqual(new_notch["parentId"], offset_id)
        self.assertNotIn("reference", new_notch)
        self.assertEqual(len(prepare_design(result["entities"])["paths"]), 2)

    def test_offset_closed_chain_anchor_points_outward_and_negative_is_inward(self):
        edges = square_edges("edge")
        edges[1]["start"], edges[1]["end"] = edges[1]["end"], edges[1]["start"]
        ids = [edge["id"] for edge in edges]
        for anchor, sign in ((ids[0], -1), (ids[1], 1)):
            self.assertEqual(edit_design(edges, "offset-anchor", {"ids": ids, "anchorId": anchor}),
                             {"normalSign": sign, "closed": True})
        for distance, bounds in ((2, (-2, -2, 22, 22)), (-2, (2, 2, 18, 18))):
            with self.subTest(distance=distance):
                result = edit_design(edges, "offset", {"ids": ids, "distance": distance, "keepOriginal": False})
                contour, = result["entities"]
                self.assertTrue(contour["closed"])
                self.assertEqual(LineString(contour["points"]).bounds, bounds)

    def test_offset_chain_refuses_branches_gaps_and_mixed_groups(self):
        base = line("base", [0, 0], [10, 0])
        side = line("side", [10, 0], [10, 10])
        branch = line("branch", [10, 0], [20, 0])
        gap = {**side, "start": [10, 0.01]}
        grouped = {**side, "groupPath": ["other"]}
        crossed = [base, line("diagonal", [10, 0], [0, 10]), line("across", [0, 10], [0, -5])]
        for entities in ([base, side, branch], [base, gap], [base, grouped], crossed):
            with self.subTest(ids=[entity["id"] for entity in entities]):
                options = {"ids": [entity["id"] for entity in entities], "anchorId": "base", "distance": 2}
                with self.assertRaises(CadDesignError):
                    edit_design(entities, "offset-anchor", options)
                with self.assertRaises(CadDesignError):
                    edit_design(entities, "offset", options)

    def test_offset_keeps_analytic_arc_and_directed_straight_line(self):
        arc = {"id": "arc", "type": "arc", "center": [2, 3], "radius": 5, "startAngle": 10, "sweep": -120}
        offset_arc, = edit_design([arc], "offset", {"id": "arc", "distance": 2, "keepOriginal": False})["entities"]
        self.assertEqual({**offset_arc, "id": "arc"}, {**arc, "layer": "0", "radius": 7})
        segment = line("line", [12, 0], [3, 0])
        result = edit_design([segment], "offset", {"id": "line", "distance": -2, "keepOriginal": False})
        shape, = result["entities"]
        self.assertEqual((shape["type"], shape["start"], shape["end"]), ("line", [12, 2], [3, 2]))

    def test_offset_continuous_spline_is_one_joined_polyline(self):
        spline = {"id": "curve", "type": "spline", "fitMethod": "centripetal",
                  "fitPoints": [[0, 0], [12, 10], [25, 10], [40, 3]]}
        result = edit_design([spline], "offset", {"id": "curve", "distance": 2, "keepOriginal": False})
        shape, = result["entities"]
        self.assertEqual(result["selectedIds"], [shape["id"]])
        self.assertEqual(shape["type"], "polyline")
        self.assertTrue(shape["joined"])
        self.assertFalse(shape["closed"])
        self.assertGreater(len(shape["points"]), len(spline["fitPoints"]))

    def test_offset_merges_shared_ends_and_warns_about_a_remaining_gap(self):
        from unittest.mock import patch

        from shapely.geometry import LineString, MultiLineString

        outline = {"id": "outline", "type": "polyline", "points": [[0, 0], [4, 0], [12, 0]]}
        pieces = MultiLineString([[(0, 2), (4, 2)], [(4, 2), (8, 2)], [(8.0001, 2), (12, 2)]])
        with patch.object(LineString, "offset_curve", return_value=pieces):
            result = edit_design([outline], "offset", {"id": "outline", "distance": 2, "keepOriginal": False})
        paths = sorted(result["entities"], key=lambda shape: shape["points"][0][0])
        self.assertEqual([shape["points"] for shape in paths], [[[0, 2], [4, 2], [8, 2]], [[8.0001, 2], [12, 2]]])
        self.assertTrue(all(shape["type"] == "polyline" and shape["joined"] and not shape["closed"] for shape in paths))
        self.assertCountEqual(result["selectedIds"], [shape["id"] for shape in paths])
        self.assertEqual(result["warnings"], ["Смещение дало несколько отдельных контуров."])

    def test_offset_split_preserves_open_section_and_closed_loop(self):
        outline = {"id": "outline", "type": "polyline", "points": [[18, 2], [15, 8], [1, 0], [4, 18], [15, 11]]}
        result = edit_design([outline], "offset", {"id": "outline", "distance": -2, "keepOriginal": False})
        open_path, loop = sorted(result["entities"], key=lambda shape: shape["closed"])
        self.assertFalse(open_path["closed"])
        self.assertTrue(loop["closed"])
        self.assertNotEqual(loop["points"][0], loop["points"][-1])
        self.assertTrue(all(shape["joined"] for shape in (open_path, loop)))
        self.assertEqual(result["warnings"], ["Смещение дало несколько отдельных контуров."])

    def test_insert_points_projects_line_and_preserves_ids_and_attachment(self):
        original = {**line("owner", [0, 0], [12, 0]), "groupPath": ["part"], "reference": True}
        notch = {**line("notch", [8, 0], [8, 2]), "notch": True, "parentId": "owner"}
        result = edit_design([original, notch], "insert-points", {"id": "owner", "point": [3, 1]})
        contour = result["entities"][0]
        self.assertEqual(result["selectedIds"], ["owner"])
        self.assertEqual(contour["points"], [[0, 0], [3, 0], [12, 0]])
        self.assertEqual(contour["type"], "polyline")
        self.assertTrue(contour["joined"])
        self.assertTrue(contour["reference"])
        self.assertEqual(contour["groupPath"], ["part"])
        self.assertEqual(result["entities"][1], validate_design([original, notch])[1])
        multiple = edit_design([original], "insert-points", {"id": "owner", "point": [2, 1], "count": 2})
        self.assertEqual(multiple["entities"][0]["points"], [[0, 0], [4, 0], [8, 0], [12, 0]])

    def test_insert_points_splits_closing_bulge_without_changing_circle(self):
        from designer_cad import _curve_edge

        source = {"id": "outline", "type": "polyline", "closed": True,
                  "points": [[-5, 0], [0, 8], [5, 0]], "bulges": [0, 0, -0.75]}
        result = edit_design([source], "insert-points", {"id": "outline", "point": [1, 0], "edge": 2, "count": 3})
        contour, = result["entities"]
        self.assertTrue(contour["closed"])
        self.assertEqual(contour["points"][:3], source["points"])
        self.assertEqual(contour["bulges"][:2], [0, 0])
        original = _curve_edge(source["points"][2], source["points"][0], -0.75)
        points = contour["points"]
        for index, bulge in enumerate(contour["bulges"][2:], 2):
            section = _curve_edge(points[index], points[(index + 1) % len(points)], bulge)
            self.assertAlmostEqual(section.sweep, original.sweep / 4)
            self.assertAlmostEqual(section.radius, original.radius)
            self.assertAlmostEqual(math.dist(section.center, original.center), 0)

    def test_insert_points_keeps_fit_and_weighted_spline_geometry(self):
        from designer_cad import _fit_spline_spans, _native_spline

        fit = {"id": "fit", "type": "spline", "closed": True, "fitMethod": "centripetal",
               "fitPoints": [[0, 0], [3, 8], [10, 0]]}
        rational = {"id": "rational", "type": "spline", "degree": 2,
                    "controlPoints": [[0, 0], [5, 8], [10, 0]], "weights": [1, .7, 1],
                    "knots": [0, 0, 0, 1, 1, 1]}
        canonical_fit, canonical_rational = validate_design([fit, rational])
        fit_spans = list(_fit_spline_spans(canonical_fit))
        old_rational = _native_spline(canonical_rational)
        for source, click in [(fit, [1, 4]), (rational, [5, 6])]:
            result = edit_design([source], "insert-points", {"id": source["id"], "point": click, "count": 2})
            shape, = result["entities"]
            self.assertEqual(result["selectedIds"], [source["id"]])
            self.assertEqual(shape["closed"], source.get("closed", False))
            self.assertEqual(shape["type"], "spline")
            refined = _native_spline(shape)
            for index in range(121):
                parameter = index / 120
                if source is fit:
                    span = min(len(fit_spans) - 1, int(parameter * len(fit_spans)))
                    at = parameter * len(fit_spans) - span
                    begin, first, last, end = fit_spans[span]
                    expected = (begin * ((1 - at) ** 2 * (1 + 2 * at)) + first * (at * (1 - at) ** 2)
                                + end * (at ** 2 * (3 - 2 * at)) - last * (at ** 2 * (1 - at)))
                else:
                    expected = old_rational.point(parameter)
                self.assertLess((refined.point(parameter) - expected).magnitude, 1e-10)
            original_knots = {0, 1 / 3, 2 / 3, 1} if source is fit else {0, 1}
            new_knots = set(shape["knots"]) - original_knots
            self.assertEqual(len(new_knots), 2)
            for knot in new_knots:
                at = tuple(refined.point(knot))[:2]
                self.assertTrue(any(math.dist(point, at) < 1e-10 for point in shape["controlPoints"]))

    def test_insert_points_rejects_duplicate_and_invalid_requests(self):
        segment = line("line", [0, 0], [10, 0])
        defaults = {"id": "line", "point": [5, 0]}
        for options in ({"point": [0, 0]}, {"count": 0}, {"count": 101}, {"edge": 1}, {"edge": True}):
            with self.subTest(options=options), self.assertRaises(CadDesignError):
                edit_design([segment], "insert-points", {**defaults, **options})
        spline = {"id": "spline", "type": "spline", "fitPoints": [[0, 0], [5, 5], [10, 0]]}
        with self.assertRaises(CadDesignError):
            edit_design([spline], "insert-points", {"id": "spline", "point": [5, 5]})

    def test_circle_notch_starts_on_exact_boundary(self):
        circle = {"id": "circle", "type": "circle", "center": [2, 3], "radius": 5}
        result = edit_design([circle], "notch", {"id": "circle", "point": [2, 7], "depth": 1.5})
        notch = result["entities"][-1]
        self.assertAlmostEqual(math.dist(notch["start"], [2, 8]), 0)
        self.assertAlmostEqual(math.dist(notch["end"], [2, 6.5]), 0)

    def test_notch_series_on_selected_side_spaces_marks_between_ends(self):
        result = edit_design([square()], "notch-series", {
            "id": "outline", "scope": "edge", "point": [19, 13], "count": 3, "depth": 2,
        })
        marks = result["entities"][1:]
        self.assertEqual([mark["start"] for mark in marks], [[20, 5], [20, 10], [20, 15]])
        self.assertEqual([mark["end"] for mark in marks], [[18, 5], [18, 10], [18, 15]])
        self.assertTrue(all(mark["parentId"] == "outline" and mark["notch"] for mark in marks))
        self.assertEqual(result["selectedIds"], [mark["id"] for mark in marks])
        self.assertEqual(sum(path.get("notch", False) for path in prepare_design(result["entities"])["paths"]), 3)

    def test_notch_series_on_bulged_segment_follows_the_arc(self):
        parent = {"id": "edge", "type": "polyline", "points": [[0, 0], [20, 0], [20, 10]],
                  "bulges": [1, 0, 0], "closed": False}
        result = edit_design([parent], "notch-series", {
            "id": "edge", "scope": "edge", "point": [10, -10], "count": 3, "depth": 2,
        })
        left, center, right = result["entities"][1:]
        self.assertAlmostEqual(center["start"][0], 10, delta=0.01)
        self.assertAlmostEqual(center["start"][1], -10, delta=0.05)
        self.assertAlmostEqual(center["end"][1] - center["start"][1], 2, delta=0.01)
        self.assertAlmostEqual(left["start"][0] + right["start"][0], 20, delta=0.01)

    def test_notch_series_uses_arc_length_and_closed_anchor_phase(self):
        rectangle = {**square(), "points": [[0, 0], [30, 0], [30, 10], [0, 10]]}
        result = edit_design([rectangle], "notch-series", {
            "id": "outline", "scope": "contour", "point": [2, 0], "count": 4, "depth": 2,
        })
        self.assertEqual([mark["start"] for mark in result["entities"][1:]], [[12, 0], [30, 2], [18, 10], [0, 8]])

    def test_notch_depth_and_reanchoring_preserve_identity_and_reference(self):
        parent = {**square(), "reference": True}
        added = edit_design([parent], "notch", {"id": "outline", "point": [8, 0], "depth": 2})
        original = added["entities"][-1]
        resized = edit_design(added["entities"], "notch-edit", {"id": original["id"], "depth": 3})
        self.assertEqual(resized["entities"][-1], {**original, "end": [8, 3]})
        moved = edit_design(resized["entities"], "notch-edit", {
            "id": original["id"], "depth": 4, "point": [20.2, 12],
        })
        self.assertEqual(moved["entities"][-1], {**original, "start": [20, 12], "end": [16, 12]})
        self.assertTrue(moved["entities"][-1]["reference"])
        self.assertEqual(moved["selectedIds"], [original["id"]])

    def test_notch_edit_attaches_unbound_polyline_from_either_endpoint(self):
        mark = {"id": "imported", "type": "polyline", "layer": "Насечки", "notch": True, "reference": True,
                "points": [[10, 2], [10, 1], [10, 0.03]], "closed": False}
        result = edit_design([square(), mark], "notch-edit", {"id": "imported", "depth": 3})
        self.assertEqual(result["entities"][-1], {**line("imported", [10, 0], [10, 3]), "layer": "Насечки",
                                                  "reference": True, "notch": True, "parentId": "outline"})
        self.assertEqual(result["selectedIds"], ["imported"])

    def test_ambiguous_or_floating_unbound_notch_requires_explicit_attachment(self):
        mark = {**line("imported", [10, 0.02], [10, 2]), "notch": True}
        duplicate = {**square(), "id": "other"}
        with self.assertRaisesRegex(CadDesignError, "нескольким сторонам"):
            edit_design([square(), duplicate, mark], "notch-edit", {"id": "imported", "depth": 3})
        corner = {**mark, "start": [0.02, 0.02], "end": [1, 1]}
        with self.assertRaisesRegex(CadDesignError, "нескольким сторонам"):
            edit_design([square(), corner], "notch-edit", {"id": "imported", "depth": 3})
        bound = edit_design([square(), duplicate, mark], "notch-edit", {
            "id": "imported", "depth": 3, "parentId": "outline",
        })
        self.assertEqual(bound["entities"][-1]["parentId"], "outline")
        floating = {**line("imported", [9, 8], [10, 8]), "notch": True}
        with self.assertRaisesRegex(CadDesignError, "Не найден соседний край"):
            edit_design([square(), floating], "notch-edit", {"id": "imported", "depth": 3})
        placed = edit_design([square(), floating], "notch-edit", {
            "id": "imported", "depth": 3, "point": [20.04, 15], "parentId": "outline",
        })
        self.assertEqual(placed["entities"][-1]["start"], [20, 15])
        self.assertEqual(placed["entities"][-1]["end"], [17, 15])

    def test_notches_reject_opposite_boundary_and_conflicts_atomically(self):
        small = {**square(), "points": [[0, 0], [4, 0], [4, 1], [0, 1]]}
        with self.assertRaisesRegex(CadDesignError, "пересекает границу"):
            edit_design([small], "notch", {"id": "outline", "point": [2, 0], "depth": 1})
        circle = {"id": "circle", "type": "circle", "center": [0, 0], "radius": 2}
        with self.assertRaisesRegex(CadDesignError, "противоположную сторону"):
            edit_design([circle], "notch", {"id": "circle", "point": [2, 0], "depth": 4})

        added = edit_design([square()], "notch", {"id": "outline", "point": [10, 0], "depth": 2})
        with self.assertRaisesRegex(CadDesignError, "ближе 0,5"):
            edit_design(added["entities"], "notch", {"id": "outline", "point": [10.25, 0], "depth": 3})
        with self.assertRaisesRegex(CadDesignError, "ближе 0,5"):
            edit_design(added["entities"], "notch-series", {
                "id": "outline", "scope": "edge", "point": [3, 0], "count": 3, "depth": 2,
            })
        with self.assertRaisesRegex(CadDesignError, "слишком короткая"):
            edit_design([small], "notch-series", {
                "id": "outline", "scope": "edge", "point": [2, 0], "count": 12, "depth": 0.2,
            })
        with self.assertRaisesRegex(CadDesignError, "вдали от угла"):
            edit_design([small], "notch-series", {
                "id": "outline", "scope": "edge", "point": [4, 0], "count": 1, "depth": 0.2,
            })
        self.assertEqual(len(added["entities"]), 2)

    def test_trim_click_and_join_preserve_intent(self):
        horizontal = line("target", [0, 0], [20, 0])
        left = line("left", [5, -5], [5, 5])
        right = line("right", [15, -5], [15, 5])
        trimmed = edit_design([horizontal, left, right], "trim", {
            "id": "target", "cutterIds": ["left", "right"], "point": [10, 0],
        })
        self.assertEqual([entity["end"] for entity in trimmed["entities"][2:]], [[5, 0], [20, 0]])
        self.assertEqual([entity["start"] for entity in trimmed["entities"][2:]], [[0, 0], [15, 0]])

        arc = {"id": "arc", "type": "arc", "center": [10, 5], "radius": 5,
               "startAngle": -90, "sweep": 180}
        segment = line("segment", [0, 0], [10, 0])
        joined = edit_design([arc, segment], "join", {"ids": ["segment", "arc"]})
        contour, = joined["entities"]
        self.assertEqual(contour["points"], [[0, 0], [10, 0], [10, 10]])
        self.assertAlmostEqual(contour["bulges"][1], 1)

    def test_join_two_reversed_semicircles_as_one_editable_closed_contour(self):
        top = {"id": "top", "type": "arc", "center": [10, 10], "radius": 10,
               "startAngle": 0, "sweep": 180}
        bottom = {"id": "bottom", "type": "arc", "center": [10, 10], "radius": 10,
                  "startAngle": 0, "sweep": -180}
        result = edit_design([top, bottom], "join", {"ids": ["top", "bottom"]})
        contour, = validate_design(result["entities"])
        self.assertEqual(result["selectedIds"], [contour["id"]])
        self.assertTrue(contour["joined"])
        self.assertTrue(contour["closed"])
        self.assertEqual(contour["points"], [[20, 10], [0, 10]])
        self.assertEqual(len(contour["bulges"]), 2)
        for bulge in contour["bulges"]:
            self.assertAlmostEqual(bulge, 1)
        (path,) = prepare_design([contour])["paths"]
        self.assertTrue(path["closed"])

    def test_join_does_not_bridge_disconnected_fragments(self):
        left = line("left", [0, 0], [5, 0])
        right = line("right", [6, 0], [11, 0])
        with self.assertRaisesRegex(CadDesignError, "есть разрыв"):
            edit_design([left, right], "join", {"ids": ["left", "right"]})

    def test_validation_rejects_invalid_geometry_and_binding(self):
        with self.assertRaises(CadDesignError):
            validate_design([line("line", [0, 0], [float("nan"), 1])])
        with self.assertRaises(CadDesignError):
            validate_design([line("same", [0, 0], [1, 1]), line("same", [0, 0], [1, 1])])
        with self.assertRaises(CadDesignError):
            validate_design([{**line("new", [0, 0], [1, 1]), "notch": True, "parentId": "missing"}])


if __name__ == "__main__":
    unittest.main()
