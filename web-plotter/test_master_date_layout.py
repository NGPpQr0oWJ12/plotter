from copy import deepcopy
from io import BytesIO
from math import ceil
import unittest

from PIL import Image, ImageChops, ImageDraw, ImageFont

from label_layout import FONT_PATH, PIXELS_PER_MM, render_label


def pixels(mm):
    return round(mm * PIXELS_PER_MM)


class MasterDateLayoutTests(unittest.TestCase):
    stamp = "26.11.2026 21:42"
    date = {"id": "date", "field": "date", "x": 42.1, "y": 36.3, "w": 15.9, "h": 3.2, "fontMm": 3}

    def render(self, elements, master=True):
        template = {"widthMm": 58, "heightMm": 40, "elements": deepcopy(elements)}
        fields = {"date": self.stamp, **({"_labelKind": "master"} if master else {})}
        data, warnings = render_label(template, fields)
        self.assertEqual(template["elements"], elements)
        return Image.open(BytesIO(data)).convert("L"), warnings

    def reference(self, size, left):
        font = ImageFont.truetype(str(FONT_PATH), size)
        image = Image.new("L", (pixels(58), pixels(40)), "white")
        ImageDraw.Draw(image).text((left, pixels(36.3)), self.stamp, font=font, fill="black", anchor="lt")
        return image

    def test_complete_stamp_uses_spare_space_to_the_left_at_requested_size(self):
        image, warnings = self.render([self.date])
        font = ImageFont.truetype(str(FONT_PATH), pixels(3))
        start = pixels(58) - ceil(max(font.getlength(self.stamp), font.getbbox(self.stamp, anchor="lt")[2]))
        self.assertLess(start, pixels(42.1))
        self.assertFalse(warnings)
        self.assertIsNone(ImageChops.difference(image, self.reference(font.size, start)).getbbox())

    def test_full_stamp_fits_without_using_neighbouring_field(self):
        neighbour = {"id": "count", "field": "text", "text": "17", "x": 10, "y": 35, "w": 32.1, "h": 5}
        image, warnings = self.render([neighbour, self.date])
        baseline, _ = self.render([neighbour])
        edge = pixels(42.1)
        self.assertFalse(warnings)
        self.assertEqual(image.crop((0, 0, edge, image.height)).tobytes(),
                         baseline.crop((0, 0, edge, image.height)).tobytes())
        right = (edge, 0, image.width, image.height)
        self.assertTrue(any(self.reference(size, edge).crop(right).tobytes() == image.crop(right).tobytes()
                            for size in range(pixels(1.5), pixels(3))))

    def test_part_date_keeps_its_own_field_bounds_and_overflow_warning(self):
        image, warnings = self.render([self.date], master=False)
        self.assertTrue(warnings)
        self.assertGreaterEqual(ImageChops.invert(image).getbbox()[0], pixels(42.1))
