import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import ezdxf
from ezdxf import units

from designer_cad import (CadDesignError, IMPORT_DUPLICATE_POINTS, IMPORT_INVALID_VERTEX,
                          edit_design, import_design, prepare_design, validate_design)


def line(identifier, start, end):
    return {"id": identifier, "type": "line", "layer": "0", "start": start, "end": end}


def square():
    return {"id": "outline", "type": "polyline", "layer": "Крой", "closed": True,
            "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}


class DesignerCadTests(unittest.TestCase):
    def test_repair_small_gap_and_duplicate_clears_only_resolved_import_issue(self):
        shape = {**square(), "closed": False, "points": [[0, 0], [0, 0], [20, 0], [20, 20], [0, 20], [0, 0.01]],
                 "importIssues": [IMPORT_DUPLICATE_POINTS, IMPORT_INVALID_VERTEX]}
        result = edit_design([shape], "repair-part", {"ids": [shape["id"]]})
        repaired, = result["entities"]
        self.assertTrue(result["changed"])
        self.assertTrue(repaired["closed"])
        self.assertEqual(repaired["id"], shape["id"])
        self.assertEqual(repaired["points"], [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0.01]])
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
        added = edit_design([square()], "notch", {"id": "outline", "point": [8, 1], "depth": 3})
        self.assertEqual(added["entities"][-1]["end"], [8, 3])
        offset = edit_design(added["entities"], "offset", {"id": "outline", "distance": 2})
        old, old_notch, new, new_notch = offset["entities"]
        self.assertTrue(old["reference"])
        self.assertTrue(old_notch["reference"])
        self.assertEqual(offset["selectedIds"], [new["id"]])
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
