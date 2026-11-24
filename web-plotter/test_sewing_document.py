from copy import deepcopy
from re import findall
from threading import Lock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from xml.etree.ElementTree import fromstring

from sewing_document import _part_sheet, registry_part_document
from sewing_pdf import render_part_preview_svg, render_sewing_pdf
from server import Handler


OUTLINE = {"closed": True, "points": [[20, 40], [420, 40], [420, 340], [20, 340]]}


class SewingDocumentTests(TestCase):
    def test_reference_notches_are_document_geometry_only(self):
        part = {"name": "Боковина", "paths": [OUTLINE], "entities": [
            {"id": "symbol", "type": "line", "layer": "Метки", "notch": True, "reference": True,
             "start": [120, 40], "end": [120, 38]},
            {"id": "pair", "type": "point", "layer": "Метка П2", "point": [120, 40], "reference": True},
        ]}
        previous = deepcopy(part)
        sheet = _part_sheet(3, part["paths"], part, source_geometry=True)
        self.assertEqual(part, previous)
        self.assertEqual(len(sheet["paths"]), 2)
        self.assertTrue(sheet["paths"][1]["notch"])
        self.assertEqual(sheet["marks"], [{"point": [120, 40]}])

    def test_all_pdf_sections_stay_on_portrait_a4(self):
        path = {"points": [[0, 0], [2100, 0], [2100, 100], [0, 100]], "closed": True, "partNumber": 1}
        part = _part_sheet(1, [path])
        pdf = render_sewing_pdf("Комплект", [part], workspace={"paths": [path]}, context={"widthMm": 1500})
        boxes = findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        self.assertGreaterEqual(len(boxes), 6)
        self.assertEqual({tuple(round(float(value)) for value in box) for box in boxes}, {(595, 842)})

    def test_registry_part_sheet_uses_source_geometry_and_full_properties(self):
        entry = {"kind": "part", "name": "Боковина <левая> & правая", "paths": [OUTLINE], "entities": [
            {"id": "notch", "type": "line", "layer": "Метки", "notch": True, "reference": True,
             "start": [120, 40], "end": [120, 38]},
        ], "partLegend": {"developer": "В. В. Иванов", "date": "11.2026",
                          "comments": "Проверить кромку. " * 50 + "\nПоследняя контрольная строка."}}
        preview = registry_part_document(entry)
        self.assertIsNone(preview["number"])
        self.assertTrue(preview["paths"][1]["notch"])
        root = fromstring(render_part_preview_svg(preview))
        labels = [node.text for node in root.findall(".//{*}text")]
        self.assertEqual(root.find("{*}title").text, entry["name"])
        for removed in (entry["name"], "Чертёж детали", "Наименование листа", "Лист", "Листов", "Формат", "А4"):
            self.assertNotIn(removed, labels)
        self.assertFalse(any("Масштаб" in label for label in labels))
        self.assertIn("В. В. Иванов", labels)
        self.assertIn("11.2026", labels)
        self.assertIn("Последняя контрольная строка.", labels)
        self.assertIn("400", labels)
        self.assertIn("300", labels)
        self.assertNotIn("№", labels)
        self.assertTrue(root.findall(".//{*}clipPath"))
        self.assertFalse(root.findall(".//{*}script"))

    def test_part_preview_compact_aspect_follows_contour(self):
        def dimensions(width, height):
            part = {"name": "Боковина", "number": None, "paths": [
                {"closed": True, "points": [[0, 0], [width, 0], [width, height], [0, height]]}
            ]}
            root = fromstring(render_part_preview_svg(part))
            return float(root.get("width")), float(root.get("height"))

        wide_width, wide_height = dimensions(1250, 160)
        tall_width, tall_height = dimensions(160, 1250)
        self.assertGreater(wide_width, 2 * wide_height)
        self.assertGreater(tall_height, tall_width)
        self.assertLessEqual(wide_width, 644)
        self.assertLess(tall_height, 550)

    def test_part_svg_companion_uses_registry_proxy_and_returns_inline_image(self):
        entry = {"kind": "part", "name": "Спинка", "paths": [OUTLINE], "pattern": {"number": "Д02"}}
        proxy = Mock(return_value=entry)
        backend = SimpleNamespace(local_companion_ui=True, workspace=SimpleNamespace(production_request=proxy),
                                  sewing_pdf_lock=Lock())
        handler = SimpleNamespace(server=backend, respond=Mock())
        identifier = "1" * 32
        Handler.get_part_preview(handler, "id=" + identifier)
        proxy.assert_called_once_with("/api/registry?id=" + identifier)
        contents, mime = handler.respond.call_args.args
        self.assertEqual(mime, "image/svg+xml; charset=utf-8")
        labels = [node.text for node in fromstring(contents).findall(".//{*}text")]
        self.assertIn("Д02", labels)
        self.assertNotIn("Спинка", labels)
