from base64 import b64encode
from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from PIL import Image, ImageChops

from label_layout import DPI, defaults, label_context, label_pdf, render_label, store_label_context, validate_template
from production_store import ProductionStore


def path(points, **fields):
    return {"points": points, "closed": True, **fields}


def template(field, *, x=1, y=1, w=18, h=12, **style):
    return {"widthMm": 20, "heightMm": 15, "elements": [
        {"id": "sample", "field": field, "x": x, "y": y, "w": w, "h": h, **style},
    ]}


def image_of(png):
    with Image.open(BytesIO(png)) as image:
        image.load()
        return image


def ink_bounds(image):
    return ImageChops.difference(image, Image.new("RGB", image.size, "white")).getbbox()


class LabelLayoutTests(unittest.TestCase):
    def test_defaults_are_independent_and_validate(self):
        first = defaults()
        first["part"]["elements"][0]["x"] = 500
        with self.assertRaises(ValueError):
            validate_template("part", first["part"])
        for kind, design in defaults().items():
            self.assertEqual(validate_template(kind, design), design)

    def test_template_rejects_bad_geometry_and_image(self):
        for design in (
                template("number", x=float("nan")), template("number", x=10), template("unknown"),
                template("image", image="https://example.invalid/logo.png"),
                template("image", image="data:image/png;base64,invalid!"),
                template("number", text="Префикс")):
            with self.subTest(design=design), self.assertRaises(ValueError):
                validate_template("part", design)

    def test_context_resolves_visible_parts_and_uses_filtered_legacy_index(self):
        outline = [[13, 12], [81, 12], [81, 46], [13, 46]]
        hole = [[19, 15], [23, 15], [23, 21], [19, 21]]
        paths = [path(outline, partNumber=6),
                 path(outline, partNumber=4, partDisplayNumber="5/1", partName="Перед", partAngle=180),
                 path(hole, partNumber=4, partAngle=180), path(outline, partNumber=5, partDisplayNumber="5/2"),
                 path(outline), path([[1, 2], [1, 3]], closed=False, notch=True)]
        record = {"cutRevision": 7, "name": "Велюр", "order": {"number": "DEMO-042", "designation": "Диван"},
                  "workspace": {"paths": paths, "excluded": [0], "optimization": {"napReversed": True}}}
        registry = {4: {"pattern": {"number": "А", "direction": "nap", "side": "left", "fabricName": "Обивка",
                                    "sewing": [{"name": "Кант", "other": "Б"}]},
                        "partLegend": {"developer": "Разработчик", "date": "11.2026", "comments": "Отметка"}},
                    5: {"pattern": {"number": "Б"}}}
        context = label_context(record, registry)
        self.assertEqual(context["cutRevision"], 7)
        self.assertEqual(list(context["parts"]), ["4", "5", "path:3"])
        self.assertEqual(context["master"], {"order": "DEMO-042", "ticket": "DEMO-042", "product": "Диван",
                                             "material": "Велюр", "fabric": "Велюр", "name": "Диван",
                                             "count": 3, "date": ""})
        first = context["parts"]["4"]
        self.assertEqual((first["number"], first["patternNumber"], first["name"], first["seam"]),
                         ("5/1", "А", "Перед", "Кант → №5/2"))
        self.assertEqual((first["developer"], first["date"], first["comment"], first["side"]),
                         ("Разработчик", "11.2026", "Отметка", "Левая"))
        self.assertEqual(first["nap"], {"kind": "nap", "angle": 180})
        self.assertEqual(context["parts"]["5"]["nap"], {"kind": "nap", "angle": 180})
        self.assertEqual(first["contour"], [path(outline), path(hole)])

    def test_dimensions_measure_the_full_piece_before_layout_rotation(self):
        from math import cos, radians, sin
        c, s = cos(radians(37)), sin(radians(37))
        def placed(points):
            return [[100 + x*c - y*s, 55 + x*s + y*c] for x, y in points]
        body = path(placed([(0, 0), (150.5, 0), (150.5, 32), (0, 32)]), partNumber=7, partAngle=37)
        notch = path(placed([(0, 0), (-5, 0)]), partNumber=7, partAngle=37, closed=False, notch=True)
        fields = label_context({"workspace": {"paths": [body, notch]}})["parts"]["7"]
        self.assertEqual(fields["dimensions"], "150,5 × 32 мм")

    def test_store_context_uses_registry_record_only_for_matching_geometry(self):
        with TemporaryDirectory() as directory:
            store = ProductionStore(Path(directory) / "production.sqlite3")
            part = store.save_registry({"kind": "part", "name": "Спинка",
                                        "entities": [{"id": "edge", "layer": "Крой", "type": "polyline", "closed": True,
                                                      "points": [[0, 0], [20, 0], [20, 10], [0, 10]]}],
                                        "partLegend": {"developer": "Конструктор", "comments": "Надпись"}})
            order = store.save_order({"number": "R-42", "designation": "Диван", "quantity": 2})
            order = store.save_cut({"orderId": order["id"], "orderRevision": order["revision"], "name": "Велюр",
                                    "widthMm": 1400, "members": [{"id": part["id"], "quantity": 1}]})
            cut_id = order["cutId"]
            context = store_label_context(store, cut_id, "design")
            self.assertEqual(context["master"]["count"], 2)
            self.assertEqual([item["number"] for item in context["parts"].values()], ["2/1", "2/2"])
            self.assertEqual(context["parts"]["1"]["developer"], "Конструктор")
            self.assertEqual(context["parts"]["2"]["comment"], "Надпись")

            previous = store.workspace(cut_id, "design")
            draft = previous["workspace"]
            draft["paths"][1]["points"][1][0] += 2
            with store.connect(write=True) as db:
                saved = store.put_workspace(db, cut_id, "design", draft, store.find_workspace(db, cut_id, "design"))
            changed = store_label_context(store, cut_id, "design")
            self.assertEqual(changed["cutRevision"], saved["cutRevision"])
            self.assertEqual(changed["parts"]["1"]["developer"], "Конструктор")
            self.assertEqual(changed["parts"]["2"]["developer"], "")

    def test_raster_measures_203_dpi_and_renders_distinct_cyrillic(self):
        design = template("name", fontMm=3)
        png, warnings = render_label(design, {"name": "Бархат"})
        image = image_of(png)
        another = image_of(render_label(design, {"name": "Жаккард"})[0])
        self.assertEqual(image.size, (160, 120))
        self.assertAlmostEqual(image.info["dpi"][0], DPI, delta=.01)
        self.assertIsNotNone(ink_bounds(image))
        self.assertIsNotNone(ImageChops.difference(image, another).getbbox())
        self.assertEqual(warnings, [])

    def test_overflow_warns_and_preserves_element_boundary(self):
        design = template("name", x=3, y=4, w=11, h=4, fontMm=3.2)
        data, warnings = render_label(design, {"name": "Длинное название детали из мягкой ткани"})
        self.assertTrue(warnings and "sample" in warnings[0])
        left, top, right, bottom = ink_bounds(image_of(data))
        self.assertGreaterEqual(left, 24)
        self.assertGreaterEqual(top, 32)
        self.assertLessEqual(right, 112)
        self.assertLessEqual(bottom, 64)

    def test_full_contour_retains_closed_footprint_and_hole(self):
        design = template("contour")
        outer = path([[100, 100], [170, 100], [170, 150], [100, 150]])
        hole = path([[120, 120], [140, 120], [140, 135], [120, 135]])
        full, warnings = render_label(design, {"contour": [outer, hole]})
        outline, _ = render_label(design, {"contour": [outer]})
        full, outline = image_of(full), image_of(outline)
        bounds = ink_bounds(full)
        changed = ImageChops.difference(full, outline).getbbox()
        self.assertLess(bounds[0], changed[0])
        self.assertLess(bounds[1], changed[1])
        self.assertGreater(bounds[2], changed[2])
        self.assertGreater(bounds[3], changed[3])
        self.assertEqual(ink_bounds(outline), bounds)
        self.assertGreater(bounds[0], 8)
        self.assertGreater(bounds[1], 8)
        self.assertLess(bounds[2], 152)
        self.assertLess(bounds[3], 104)
        self.assertEqual(warnings, [])

    def test_png_element_preserves_aspect_and_pdf_uses_actual_mm(self):
        buffer = BytesIO()
        Image.new("RGB", (4, 2), "black").save(buffer, "PNG")
        design = template("image", image="data:image/png;base64," + b64encode(buffer.getvalue()).decode("ascii"))
        png, warnings = render_label(design, {})
        left, top, right, bottom = ink_bounds(image_of(png))
        self.assertAlmostEqual((right - left) / (bottom - top), 2, places=1)
        self.assertEqual(warnings, [])
        pdf = label_pdf([(design, {}), (defaults()["master"], {"order": "42"})])
        boxes = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        self.assertEqual(len(boxes), 2)
        for (width, height), expected in zip(boxes, ((20, 15), (58, 40))):
            self.assertAlmostEqual(float(width) * 25.4 / 72, expected[0], places=3)
            self.assertAlmostEqual(float(height) * 25.4 / 72, expected[1], places=3)


if __name__ == "__main__":
    unittest.main()
