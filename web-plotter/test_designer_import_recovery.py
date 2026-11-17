from codecs import BOM_UTF8
from io import BytesIO, StringIO
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ezdxf
from ezdxf import recover, units
from shapely.geometry import LineString

from cad import CadImportError, import_drawing
from designer_cad import CadDesignError, IMPORT_AUDIT, IMPORT_DOUBLED_LINE, edit_design, import_design, prepare_design, validate_design


def drawing(*entities, header=(), eof=True):
    tags = list(header) + [(0, "SECTION"), (2, "ENTITIES")]
    for entity in entities:
        tags.extend(entity)
    tags.append((0, "ENDSEC"))
    if eof:
        tags.append((0, "EOF"))
    return "".join(f"{code}\n{value}\n" for code, value in tags).encode("utf-8")


LINE = [(0, "LINE"), (8, "0"), (10, 0), (20, 0), (11, 2), (21, 1)]


def segment(start, end):
    return [(0, "LINE"), (8, "0"), (10, start[0]), (20, start[1]), (11, end[0]), (21, end[1])]


def units_header(value):
    return [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1015"),
            (9, "$INSUNITS"), (70, value), (0, "ENDSEC")]


def polyline(points, closed=False):
    tags = [(0, "LWPOLYLINE"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbPolyline"),
            (90, len(points)), (70, int(closed))]
    for x, y, bulge in points:
        tags.extend([(10, x), (20, y), (42, bulge)])
    return tags


class DesignerImportRecoveryTests(unittest.TestCase):
    def test_dxf_dense_outline_is_simplified_without_closing_the_gap_or_detaching_marks(self):
        curve = [(index / 20, -math.sin(math.pi * index / 400)) for index in range(401)]
        points = [*curve, (20, 20), (0, 20), (0, .796)]
        outline = polyline([(x, y, 0) for x, y in points])
        imported = import_design(drawing(outline, header=units_header(units.MM)), "curve.dxf")
        reduced, = imported["entities"]
        self.assertFalse(reduced["closed"])
        self.assertAlmostEqual(math.dist(reduced["points"][0], reduced["points"][-1]), .796)
        self.assertLess(len(reduced["points"]), 40)
        self.assertEqual(LineString(points).bounds, LineString(reduced["points"]).bounds)
        self.assertLessEqual(LineString(points).hausdorff_distance(LineString(reduced["points"])), .02)

        contact = curve[100]
        with_mark = drawing(outline, segment(contact, (5, -2)), header=units_header(units.MM))
        protected, mark = import_design(with_mark, "marked.dxf")["entities"]
        self.assertIn(list(contact), protected["points"])
        self.assertEqual(mark["start"], list(contact))

    def test_cad_dxf_import_repairs_tiny_corner_on_closed_native_polyline_or_line_chain(self):
        points = [(100, 0), (-.0015, 0), (.0002, .001), (-.1, -100), (100, -100)]
        crossing_x = .0002 + .001 * (-.1 - .0002) / (100 + .001)
        for kind in ("closed polyline", "repeated seam", "line cycle"):
            with self.subTest(kind=kind):
                if kind == "line cycle":
                    entities = [segment(start, end) for start, end in zip(points, points[1:] + points[:1])]
                else:
                    vertices = points if kind == "closed polyline" else [*points, points[0]]
                    entities = [polyline([(x, y, 0) for x, y in vertices], closed=kind == "closed polyline")]
                result = import_design(drawing(*entities, header=units_header(4)), "small-loop.dxf")
                self.assertIn("Удалено малых петель в углах — 1.", result["repairSummary"])
                self.assertNotIn("diagnostics", result)
                if kind == "line cycle":
                    self.assertEqual([shape["id"] for shape in result["entities"]], ["dxf-1", "dxf-3", "dxf-4", "dxf-5"])
                    self.assertTrue(all(shape["type"] == "line" for shape in result["entities"]))
                    self.assertEqual(result["entities"][0]["end"], result["entities"][1]["start"])
                    corner = result["entities"][0]["end"]
                else:
                    outline, = result["entities"]
                    self.assertEqual(outline["id"], "dxf-1")
                    self.assertTrue(outline["closed"])
                    self.assertEqual(len(outline["points"]), 4)
                    corner = outline["points"][1]
                self.assertAlmostEqual(corner[0], crossing_x, places=12)
                self.assertAlmostEqual(corner[1], 0, places=12)

    def test_cad_dxf_import_preserves_large_protected_and_genuinely_open_corner_geometry(self):
        corners = [(0, 0, 0), (20.04, 0, 0), (20, -.05, 0), (20, 20, 0), (0, 20, 0)]
        large = [corners[0], (20.11, 0, 0), *corners[2:]]
        attachment = [(0, "ARC"), (8, "0"), (10, 20.04), (20, 1), (40, 1), (50, 270), (51, 315)]
        crossing = segment((20.03, -.02), (20.03, .02))
        tangent = [(0, "CIRCLE"), (8, "0"), (10, 20.03), (20, .005), (40, .005)]
        open_seam = [corners[2], *corners[3:], *corners[:2]]
        for shape, closed, neighbors in ((large, True, []), (corners, True, [attachment]),
                                         (corners, True, [crossing]), (corners, True, [tangent]),
                                         (open_seam, False, [])):
            with self.subTest(shape=shape, closed=closed, neighbors=neighbors):
                data = drawing(polyline(shape, closed=closed), *neighbors, header=units_header(4))
                result = import_design(data, "kept-loop.dxf")
                self.assertNotIn("Удалено малых петель", result.get("repairSummary", ""))
                self.assertEqual(result["entities"][0]["points"], [[x, y] for x, y, _ in shape])
                self.assertEqual(result["entities"][0]["closed"], closed)
                self.assertTrue(any(issue["kind"] == "intersection" for issue in result["diagnostics"]))

        joined = segment((19.98, -.02), (20.02, .02))
        data = drawing(polyline(corners, closed=True), joined, header=units_header(4))
        result = import_design(data, "preserved-junction.dxf")
        self.assertIn("Удалено малых петель в углах — 1.", result["repairSummary"])
        self.assertEqual(result["entities"][0]["points"][1], [20, 0])

    def test_cad_dxf_import_automatically_repairs_safe_conflicts_and_keeps_warnings_and_notches(self):
        corners = [(0, 0), (20, 0), (20, 20), (0, 20)]
        native = [segment(start, end) for start, end in zip(corners, corners[1:] + corners[:1])]
        duplicate = segment((20, 0), (0, 0))
        branch = segment((0, 0), (-20, -20))
        notch = polyline([(0, 0, 0), (2, 2, 0)], closed=True)
        crossed = polyline([(60, 0, 0), (80, 20, 0), (60, 20, 0), (80, 0, 0)], closed=True)
        text = [(0, "TEXT"), (10, 3), (20, 3), (40, 1), (1, "note")]
        data = drawing(*native, duplicate, branch, notch, crossed, text, header=units_header(4))

        result = import_design(data, "outline.dxf")

        self.assertEqual([entity["id"] for entity in result["entities"]],
                         ["dxf-1", "dxf-2", "dxf-3", "dxf-4", "dxf-7", "dxf-8"])
        self.assertEqual([entity["start"] for entity in result["entities"][:4]], [list(point) for point in corners])
        self.assertEqual(result["entities"][4]["importIssues"], [IMPORT_DOUBLED_LINE])
        self.assertEqual(result["entities"][4]["points"], [[0, 0], [2, 2]])
        self.assertEqual(result["warnings"], [
            "Восстановлены повреждённые объекты: LWPOLYLINE (1). Проверьте выделенные контуры.",
            "Особенности импорта: TEXT (1).",
        ])
        self.assertIn("Исправлено конфликтов — 2.", result["repairSummary"])
        self.assertIn({"point": [70, 10], "kind": "intersection", "ids": ["dxf-8"]}, result["diagnostics"])
        self.assertEqual(set(result["diagnosticIds"]), {entity["id"] for entity in result["entities"]})

        cutter_result = import_drawing(data, "outline.dxf")
        self.assertNotIn("repairSummary", cutter_result)
        self.assertTrue(any([-20, -20] in path["points"] for path in cutter_result["paths"]))

    def test_automatic_cleanup_keeps_exact_overlap_between_distinct_dxf_sources(self):
        corners = [(0, 0), (20, 0), (20, 20), (0, 20)]
        for kind in ("block", "group"):
            with self.subTest(kind=kind):
                document = ezdxf.new(units=units.MM)
                model = document.modelspace()
                target = document.blocks.new("Part") if kind == "block" else model
                members = [target.add_line(start, end) for start, end in zip(corners, corners[1:] + corners[:1])]
                if kind == "block":
                    model.add_blockref("Part", (0, 0))
                else:
                    document.groups.new("Part").set_data(members)
                separate = model.add_line((0, 0), (20, 0))
                if kind == "group":
                    document.groups.new("Overlay").set_data([separate])
                stream = StringIO()
                document.write(stream)

                result = import_design(stream.getvalue().encode("ascii"), "separate.dxf")

                self.assertEqual(len(result["entities"]), 5)
                self.assertTrue(all("groupPath" not in entity for entity in result["entities"]))
                self.assertNotIn("repairSummary", result)
                self.assertNotIn("diagnostics", result)

    def test_automatic_cleanup_failure_and_budget_keep_the_original_import(self):
        data = drawing(LINE, LINE, header=units_header(4), eof=False)
        for check in (patch("designer_cad.MAX_TRIM_EDGES", 1),
                      patch("designer_cad._repair_conflicts", side_effect=CadDesignError("test verification failure"))):
            with check:
                result = import_design(data, "untouched.dxf")
            self.assertEqual([entity["id"] for entity in result["entities"]], ["dxf-1", "dxf-2"])
            self.assertTrue(any("Структура DXF восстановлена" in warning for warning in result["warnings"]))
            self.assertIn("геометрия сохранена", result["repairSummary"].lower())
            self.assertNotIn("diagnostics", result)

    def test_utf8_bom_preserves_the_layer_name_and_explicit_scale(self):
        line = [(code, "Контур" if code == 8 else value) for code, value in LINE]
        data = BOM_UTF8 + drawing(line, header=units_header(6))
        result = import_design(data, "drawing.DXF")
        self.assertEqual(result["entities"], [
            {"id": "dxf-1", "type": "line", "layer": "Контур", "start": [0, 0], "end": [2000, 1000]},
        ])
        self.assertEqual(result["warnings"], [])

    def test_recover_missing_eof_and_preserve_declared_units(self):
        result = import_design(drawing(LINE, header=units_header(5), eof=False), "truncated.dxf")
        self.assertEqual(result["entities"][0]["end"], [20, 10])
        self.assertTrue(any("Структура DXF восстановлена" in message for message in result["warnings"]))
        self.assertEqual(prepare_design(result["entities"])["paths"][0]["points"], [[0, 0], [20, 10]])

    def test_invalid_objects_are_reported_while_valid_entities_remain_editable(self):
        bad_circle = [(0, "CIRCLE"), (10, "nan"), (20, 1), (40, 4)]
        too_large = [(0, "LINE"), (10, 0), (20, 0), (11, 2_000_000_000), (21, 0)]
        unsupported = [(0, "TEXT"), (10, 0), (20, 0), (40, 1), (1, "note")]
        line = [(code, 8 if code == 11 else value) for code, value in LINE]
        data = drawing(bad_circle, LINE, unsupported, too_large, line, header=units_header(4))
        result = import_design(data, "drawing.dxf")
        self.assertEqual([entity["id"] for entity in result["entities"]], ["dxf-1", "dxf-2"])
        self.assertEqual([entity["end"] for entity in result["entities"]], [[2, 1], [8, 1]])
        warning = " ".join(result["warnings"])
        self.assertIn("повреждённые объекты CIRCLE (1)", warning)
        self.assertIn("повреждённые объекты LINE (1)", warning)
        self.assertIn("TEXT (1)", warning)

    def test_recovery_cannot_guess_an_invalid_unit_code(self):
        for value in (-1, 80, "wrong"):
            with self.subTest(value=value), self.assertRaises(CadImportError):
                import_design(drawing(LINE, header=units_header(value), eof=False), "wrong-units.dxf")

    def test_default_millimetres_are_silent_when_units_missing_or_zero(self):
        for header in ([], units_header(0)):
            with self.subTest(header=header):
                result = import_design(drawing(LINE, header=header), "drawing.dxf")
                self.assertEqual(result["entities"][0]["end"], [2, 1])
                self.assertEqual(result["warnings"], [])

    def test_duplicate_polyline_vertices_keep_remaining_bulges_and_close(self):
        points = [(0, 0, "nan"), (0.0000001, 0, 0.5), (20, 0, 0), (20, 20, 0), (0, 0, 0)]
        result = import_design(drawing(polyline(points, closed=True), header=units_header(0)), "duplicate.dxf")
        shape, = result["entities"]
        self.assertEqual(shape["points"], [[0, 0], [20, 0], [20, 20]])
        self.assertEqual(shape["bulges"], [0.5, 0, 0])
        self.assertTrue(shape["closed"])
        self.assertTrue(shape["importDamaged"])
        self.assertEqual(validate_design([shape]), [shape])
        self.assertIn("Восстановлены повреждённые объекты: LWPOLYLINE (1)", result["warnings"][0])

    def test_accepts_collapsed_doubled_dxf_edge_as_a_single_segment(self):
        outline = polyline([(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0)], closed=True)
        mark = polyline([(10, 0, 0), (10, 2, 0)], closed=True)
        contour, segment = import_design(drawing(outline, mark, header=units_header(4)), "mark.dxf")["entities"]
        self.assertEqual(segment["importIssues"], [IMPORT_DOUBLED_LINE])
        self.assertFalse(segment["closed"])

        result = edit_design([contour, segment], "repair-part", {"ids": [segment["id"]]})

        self.assertTrue(result["changed"])
        self.assertEqual(result["selectedIds"], [segment["id"]])
        self.assertEqual(result["entities"], [contour, {
            "id": segment["id"], "layer": segment["layer"], "type": "line", "start": [10, 0], "end": [10, 2],
        }])
        self.assertEqual(prepare_design(result["entities"]), prepare_design([contour, segment]))

    def test_accepting_doubled_dxf_edge_preserves_other_issues_and_group(self):
        mark = polyline([(10, 0, 0), (10, 2, 0)], closed=True)
        segment, = import_design(drawing(mark, header=units_header(4)), "mark.dxf")["entities"]
        segment.update(reference=True, groupPath=["marks"], importIssues=[IMPORT_DOUBLED_LINE, IMPORT_AUDIT])
        result = edit_design([segment], "repair-part", {"ids": [segment["id"]]})
        line, = result["entities"]
        self.assertEqual(line["type"], "line")
        self.assertEqual(line["importIssues"], [IMPORT_AUDIT])
        self.assertTrue(line["importDamaged"])
        self.assertTrue(line["reference"])
        self.assertEqual(line["groupPath"], ["marks"])
        self.assertFalse(edit_design([line], "repair-part", {"ids": [line["id"]]})["changed"])

    def test_bad_vertex_and_bulge_split_into_unambiguous_open_segments(self):
        points = [(0, 0, 1), (20, 0, 0), ("nan", 10, 0), (30, 10, 0),
                  (40, 10, "nan"), (50, 10, 0.5), (60, 10, 0)]
        result = import_design(drawing(polyline(points), LINE), "damaged.dxf")
        first, second, third, healthy = result["entities"]
        self.assertEqual([shape["points"] for shape in (first, second, third)],
                         [[[0, 0], [20, 0]], [[30, 10], [40, 10]], [[50, 10], [60, 10]]])
        self.assertEqual(first["bulges"], [1, 0])
        self.assertEqual(third["bulges"], [0.5, 0])
        self.assertTrue(all(shape["importDamaged"] and not shape["closed"] for shape in (first, second, third)))
        self.assertNotIn("importDamaged", healthy)

    def test_damaged_closed_polyline_keeps_real_edges_across_stored_start(self):
        outline = polyline([(10, 0, 0), (20, 0, "nan"), (20, 20, 0), (10, 20, 0)], closed=True)
        shape, = import_design(drawing(outline, header=units_header(0)), "outline.dxf")["entities"]
        self.assertEqual(shape["points"], [[20, 20], [10, 20], [10, 0], [20, 0]])
        self.assertFalse(shape["closed"])
        self.assertTrue(shape["importDamaged"])

    def test_recovery_flags_only_entities_with_an_audit_entry(self):
        data = drawing(LINE, LINE, eof=False)
        document, audit = recover.read(BytesIO(data))
        entity = list(document.modelspace())[1]
        audit.fixes.append(SimpleNamespace(entity=entity))
        with patch("designer_cad.recover.read", return_value=(document, audit)):
            result = import_design(data, "audited.dxf")
        first, second = result["entities"]
        self.assertNotIn("importDamaged", first)
        self.assertTrue(second["importDamaged"])

    def test_corrupt_file_and_aggregate_geometry_limit_still_reject(self):
        with self.assertRaises((CadImportError, CadDesignError)):
            import_design(b"not a DXF file", "drawing.dxf")
        with patch("designer_cad.MAX_POINTS", 3), self.assertRaisesRegex(CadDesignError, "слишком много"):
            import_design(drawing(LINE, LINE), "drawing.dxf")


if __name__ == "__main__":
    unittest.main()
