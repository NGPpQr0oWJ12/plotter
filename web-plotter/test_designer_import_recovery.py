from codecs import BOM_UTF8
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ezdxf import recover

from cad import CadImportError
from designer_cad import CadDesignError, import_design, prepare_design, validate_design


def drawing(*entities, header=(), eof=True):
    tags = list(header) + [(0, "SECTION"), (2, "ENTITIES")]
    for entity in entities:
        tags.extend(entity)
    tags.append((0, "ENDSEC"))
    if eof:
        tags.append((0, "EOF"))
    return "".join(f"{code}\n{value}\n" for code, value in tags).encode("utf-8")


LINE = [(0, "LINE"), (8, "0"), (10, 0), (20, 0), (11, 2), (21, 1)]


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
