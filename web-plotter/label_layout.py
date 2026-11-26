"""Physical label templates and rendering shared by preview, PDF and printing."""

from base64 import b64decode
from binascii import Error as Base64Error
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from math import ceil, cos, isfinite, radians, sin
import re

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from nesting import identify_parts
from production_store import MOSCOW_TIME, RecordNotFound, paths_by_part, same_part_geometry
from sewing_pdf import FONT_PATH
from workspace import MAX_REVISION


DPI = 203
PIXELS_PER_MM = DPI / 25.4
MAX_IMAGE_BYTES = 1_000_000
MAX_IMAGE_PIXELS = 8_000_000
PNG_DATA_URL = "data:image/png;base64,"
FIELDS_BY_KIND = {
    "part": ("order", "number", "name", "dimensions", "fabric", "date", "comment", "nap", "contour",
             "image", "text", "product", "material"),
    "master": ("order", "product", "material", "count", "date", "text", "image"),
}
# Historical print jobs keep their original template and may reference retired fields.
FIELDS = frozenset((*FIELDS_BY_KIND["part"], *FIELDS_BY_KIND["master"], "ticket",
                    "patternNumber", "side", "seam", "developer"))


def defaults():
    return {
        "part": {"widthMm": 58, "heightMm": 40, "elements": [
            {"id": "number", "field": "number", "x": 2, "y": 1.5, "w": 19, "h": 6, "fontMm": 4.5},
            {"id": "dimensions", "field": "dimensions", "x": 22, "y": 1.5, "w": 34, "h": 6, "fontMm": 3.5},
            {"id": "name", "field": "name", "x": 2, "y": 8, "w": 54, "h": 6, "fontMm": 3.5},
            {"id": "contour", "field": "contour", "x": 2, "y": 15, "w": 27, "h": 23},
            {"id": "order-label", "field": "text", "x": 31, "y": 15, "w": 25, "h": 4, "fontMm": 2.8, "text": "Заказ №"},
            {"id": "order", "field": "order", "x": 31, "y": 19, "w": 25, "h": 6, "fontMm": 3.7},
            {"id": "nap", "field": "nap", "x": 31, "y": 26, "w": 10, "h": 8},
            {"id": "fabric", "field": "fabric", "x": 42, "y": 26, "w": 14, "h": 11, "fontMm": 2.6},
        ]},
        "master": {"widthMm": 58, "heightMm": 40, "elements": [
            {"id": "title", "field": "text", "x": 2, "y": 2, "w": 17, "h": 6, "fontMm": 3.5, "text": "Комплект"},
            {"id": "date", "field": "date", "x": 22, "y": 2, "w": 34, "h": 6, "fontMm": 3.5},
            {"id": "product", "field": "product", "x": 2, "y": 10, "w": 54, "h": 9, "fontMm": 3.7},
            {"id": "material", "field": "material", "x": 2, "y": 21, "w": 54, "h": 8, "fontMm": 3},
            {"id": "count-label", "field": "text", "x": 2, "y": 32, "w": 12, "h": 6, "fontMm": 3, "text": "Деталей"},
            {"id": "count", "field": "count", "x": 15, "y": 32, "w": 10, "h": 6, "fontMm": 3.7},
            {"id": "order-label", "field": "text", "x": 26, "y": 32, "w": 12, "h": 6, "fontMm": 3, "text": "Заказ №"},
            {"id": "order", "field": "order", "x": 39, "y": 32, "w": 17, "h": 6, "fontMm": 3.7},
        ]},
    }


def _dimension(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError("Проверьте размеры и координаты элементов бирки в миллиметрах.")
    return value


def _read_png(value):
    if not isinstance(value, str) or not value.startswith(PNG_DATA_URL):
        raise ValueError("Добавьте изображение в формате PNG.")
    encoded = value[len(PNG_DATA_URL):]
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("PNG для бирки превышает 1 МБ.")
    try:
        data = b64decode(encoded, validate=True)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("PNG для бирки превышает 1 МБ.")
        with Image.open(BytesIO(data), formats=("PNG",)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("PNG должен содержать один кадр до 8 миллионов пикселей.")
            return image.convert("RGBA")
    except (Base64Error, Image.DecompressionBombError, OSError, SyntaxError) as error:
        raise ValueError("Повреждённое изображение PNG для бирки.") from error


def validate_template(kind, template):
    if kind not in ("part", "master") or not isinstance(template, dict) or set(template) != {"widthMm", "heightMm", "elements"}:
        raise ValueError("Некорректный шаблон бирки.")
    width = _dimension(template["widthMm"], 5, 200)
    height = _dimension(template["heightMm"], 5, 200)
    elements = template["elements"]
    if not isinstance(elements, list) or len(elements) > 40:
        raise ValueError("В шаблоне бирки допустимо до 40 элементов.")
    clean, seen = [], set()
    for element in elements:
        if (not isinstance(element, dict) or set(element) - {"id", "field", "x", "y", "w", "h", "fontMm", "text", "image"}
                or not isinstance(element.get("field"), str) or element["field"] not in FIELDS):
            raise ValueError("Неизвестное поле шаблона бирки.")
        identifier = element.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[\w-]{1,64}", identifier, flags=re.ASCII) or identifier in seen:
            raise ValueError("Идентификаторы элементов бирки должны быть уникальны.")
        seen.add(identifier)
        x, y = _dimension(element.get("x"), 0, width), _dimension(element.get("y"), 0, height)
        w, h = _dimension(element.get("w"), 0.1, width), _dimension(element.get("h"), 0.1, height)
        if x + w > width + 1e-6 or y + h > height + 1e-6:
            raise ValueError("Элемент выходит за границы бирки.")
        item = {**element, "x": x, "y": y, "w": w, "h": h}
        if "fontMm" in item:
            _dimension(item["fontMm"], 0.5, 50)
        if "text" in item:
            value = item["text"]
            if (item["field"] != "text" or not isinstance(value, str) or len(value) > 500
                    or any(ord(ch) < 32 and ch not in "\r\n\t" or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)):
                raise ValueError("Введите до 500 символов для статического текста бирки.")
        if "image" in item:
            if item["field"] != "image":
                raise ValueError("Добавьте PNG через поле изображения.")
            if item["image"]:
                _read_png(item["image"])
            elif item["image"] != "":
                raise ValueError("Добавьте изображение в формате PNG.")
        clean.append(item)
    return {"widthMm": width, "heightMm": height, "elements": clean}


def editable_template(kind, template):
    template = validate_template(kind, template)
    allowed = FIELDS_BY_KIND[kind]
    return {**template, "elements": [item for item in template["elements"] if item["field"] in allowed]}


def _part_number(path):
    number = path.get("partNumber")
    return number if type(number) is int and 1 <= number <= MAX_REVISION else None


def _path_field(paths, field):
    return next((path[field] for path in paths if path.get(field)), "")


def _part_dimensions(contours, angle):
    edges = [path for path in contours if not path.get("notch")]
    outlines = [path for path in edges if path.get("closed")] or edges
    # Undo the placement rotation before measuring the complete piece in its own axes.
    c, s = cos(radians(angle)), sin(radians(angle))
    points = ((x * c + y * s, y * c - x * s) for path in outlines for x, y in path["points"])
    xs, ys = zip(*points)
    sizes = sorted((max(xs) - min(xs), max(ys) - min(ys)), reverse=True)
    return " × ".join(f"{size:.1f}".rstrip("0").rstrip(".").replace(".", ",") for size in sizes) + " мм"


def label_part_keys(paths):
    """Group unnumbered holes or marks using the same instances as nesting."""
    keys = [str(number) if (number := _part_number(path)) else f"path:{i}" for i, path in enumerate(paths)]
    if any(key.startswith("path:") for key in keys):
        for group in identify_parts(paths):
            indices = group["indices"]
            key = next((keys[i] for i in indices if _part_number(paths[i])), keys[min(indices)])
            for i in indices:
                keys[i] = key
    return keys


def label_context(record, registry_parts=None):
    """Capture label fields from the saved cut revision and verified registry records."""
    workspace = record.get("workspace") or {}
    order = record.get("order") or {}
    registry_parts = registry_parts or {}
    excluded = set(workspace.get("excluded", ()))
    paths = [path for index, path in enumerate(workspace.get("paths", ())) if index not in excluded]
    groups = defaultdict(list)
    for key, path in zip(label_part_keys(paths), paths):
        groups[key].append(path)

    material = record.get("name") or ""
    fabric_angle = 180 if workspace.get("optimization", {}).get("napReversed") else 0
    order_number = order.get("number") or ""
    products = {path["productName"] for path in paths if path.get("productName")}
    product = order.get("designation") or order.get("modelName") or (next(iter(products)) if len(products) == 1 else "")
    common = {"order": order_number, "ticket": order_number, "product": product, "material": material}
    parts, patterns, by_pattern = {}, {}, defaultdict(list)
    for key, contours in groups.items():
        if all(path.get("notch") for path in contours):
            continue
        number = int(key) if not key.startswith("path:") else None
        entry = registry_parts.get(number) or registry_parts.get(key) or {}
        pattern, legend = entry.get("pattern") or {}, entry.get("partLegend") or {}
        angle = next((path["partAngle"] for path in contours if "partAngle" in path), 0)
        direction = pattern.get("direction")
        display = _path_field(contours, "partDisplayNumber") or (str(number) if number is not None else str(int(key[5:]) + 1))
        fields = {**common, "number": display, "name": _path_field(contours, "partName") or entry.get("name") or "Деталь",
                  "dimensions": _part_dimensions(contours, angle),
                  "patternNumber": pattern.get("number") or "", "fabric": pattern.get("fabricName") or material,
                  "side": {"left": "Левая", "right": "Правая"}.get(pattern.get("side"), ""),
                  "seam": "", "developer": legend.get("developer") or "", "date": legend.get("date") or "",
                  "comment": legend.get("comments") or "", "nap": {"kind": direction, "angle": angle}
                  if direction in ("nap", "grain") else {"kind": "nap", "angle": fabric_angle},
                  "contour": [{"points": path["points"], "closed": bool(path.get("closed"))} for path in contours]}
        if _path_field(contours, "productName"):
            fields["product"] = _path_field(contours, "productName")
        parts[key], patterns[key] = fields, pattern
        if fields["patternNumber"]:
            by_pattern[fields["patternNumber"]].append(key)

    for key, fields in parts.items():
        seams = []
        for edge in patterns[key].get("sewing", ()):
            other = edge["other"]
            partners = [f"№{parts[partner]['number']}" for partner in by_pattern.get(other, ()) if partner != key]
            relation = ", ".join(partners) if partners else other
            seams.append(f"{edge['name']} → {relation}")
        fields["seam"] = "\n".join(seams)

    master = {**common, "fabric": material, "name": product, "date": "", "count": len(parts)}
    return {"master": master, "parts": parts, "cutRevision": record.get("cutRevision")}


def master_label_fields(fields, completed_ms):
    # Historical runs may predate the stored completion timestamp.
    date = (datetime.fromtimestamp(completed_ms / 1000, MOSCOW_TIME).strftime("%d.%m.%Y %H:%M")
            if completed_ms is not None else "—")
    return {**fields, "date": date, "_labelKind": "master"}


def _registry_parts(store, db, cut, order, workspace):
    records = {}
    try:
        originals = paths_by_part(store.expand_parts(db, cut["members"], order["quantity"], part_records=records))
    except (ValueError, RecordNotFound):
        originals, records = {}, {}
    by_name = {}
    verified = {}
    for number, contours in paths_by_part(workspace["paths"]).items():
        if number is None:
            continue
        if number in originals and same_part_geometry(originals[number], contours):
            verified[number] = records[number]
            continue
        title = _path_field(contours, "partName")
        if not title:
            continue
        if title not in by_name:
            candidates = []
            for (identifier,) in db.execute("SELECT id FROM registry WHERE kind='part' AND name=?", (title,)):
                entries = {}
                try:
                    geometry = store.expand_parts(db, [{"id": identifier, "quantity": 1}], part_records=entries)
                except (ValueError, RecordNotFound):
                    continue
                candidates.append((geometry, entries[1]))
            by_name[title] = candidates
        matches = [entry for geometry, entry in by_name[title] if same_part_geometry(geometry, contours)]
        if len(matches) == 1:
            verified[number] = matches[0]
    return verified


def store_label_context(store, cut_id, mode):
    with store.connect() as db:
        cut = store.cut_record(db, cut_id)
        record = store.matching_workspace(db, cut, mode)
        order = store.order_header(db, cut["order_id"])
        registry_parts = _registry_parts(store, db, cut, order, record["workspace"])
        return label_context({**record, "order": order, "name": cut["name"]}, registry_parts)


def _pixels(value):
    return round(value * PIXELS_PER_MM)


def _fit_prefix(value, font, width):
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        part = value[:middle]
        left, _, right, _ = font.getbbox(part, anchor="lt")
        if max(right, font.getlength(part)) - min(0, left) <= width:
            low = middle
        else:
            high = middle - 1
    return low


def _draw_text(layer, value, font):
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").expandtabs(2)
    width, height = layer.size
    ascender, descender = font.getmetrics()
    line_height = max(1, ascender + descender)
    lines, overflow = [], False
    for paragraph in text.split("\n"):
        remaining = paragraph.strip()
        while True:
            if len(lines) * line_height >= height:
                overflow = True
                break
            count = _fit_prefix(remaining, font, width)
            if remaining and not count:
                overflow = True
                break
            head = remaining[:count]
            if count < len(remaining) and not remaining[count].isspace() and " " in head:
                head = head.rsplit(" ", 1)[0]
            line = head.rstrip()
            if len(lines) * line_height + font.getbbox(line, anchor="lt")[3] > height:
                overflow = True
                break
            lines.append(line)
            remaining = remaining[len(head):].lstrip()
            if not remaining:
                break
        if overflow:
            break
    if overflow and lines:
        suffix = "..."
        if font.getlength(suffix) <= width:
            cutoff = _fit_prefix(lines[-1], font, width - font.getlength(suffix))
            lines[-1] = lines[-1][:cutoff].rstrip() + suffix
    draw = ImageDraw.Draw(layer)
    for index, line in enumerate(lines):
        if line:
            left = font.getbbox(line, anchor="lt")[0]
            draw.text((max(0, -left), index * line_height), line, font=font, fill="black", anchor="lt")
    return overflow


def _draw_contour(layer, value):
    paths = [path for path in (value or ()) if path.get("points")]
    if not paths:
        return True
    xs, ys = zip(*(point for path in paths for point in path["points"]))
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    width, height = layer.size
    inset = max(2, _pixels(0.35))
    if min(width, height) <= 2 * inset or not (x1 > x0 or y1 > y0):
        return False
    scale = min((width - 2 * inset) / (x1 - x0) if x1 > x0 else float("inf"),
                (height - 2 * inset) / (y1 - y0) if y1 > y0 else float("inf"))
    left, top = (width - (x1 - x0) * scale) / 2, (height - (y1 - y0) * scale) / 2
    draw = ImageDraw.Draw(layer)
    for path in paths:
        points = [(left + (x - x0) * scale, top + (y1 - y) * scale) for x, y in path["points"]]
        if path.get("closed"):
            points.append(points[0])
        draw.line(points, fill="black", width=max(1, _pixels(0.23)), joint="curve")
    return True


def _draw_nap(layer, value):
    if not value:
        return True
    angle = value.get("angle", 0)
    if type(angle) not in (int, float) or not isfinite(angle):
        return False
    width, height = layer.size
    if min(width, height) < _pixels(1.5):
        return False
    ux, uy = cos(radians(angle)), -sin(radians(angle))
    radius = min(width, height) * 0.38
    cx, cy = width / 2, height / 2
    draw = ImageDraw.Draw(layer)
    draw.line((cx - ux * radius, cy - uy * radius, cx + ux * radius, cy + uy * radius),
              fill="black", width=max(1, _pixels(0.23)))
    head = max(2, radius / 2)
    for direction in (1, -1) if value.get("kind") == "grain" else (1,):
        x, y = cx + direction * ux * radius, cy + direction * uy * radius
        bx, by = x - direction * ux * head, y - direction * uy * head
        draw.polygon([(x, y), (bx - uy * head / 2, by + ux * head / 2),
                      (bx + uy * head / 2, by - ux * head / 2)], fill="black")
    return True


def _draw_image(layer, value):
    if not value:
        return
    image = _read_png(value)
    scale = min(layer.width / image.width, layer.height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    inset = image.resize(size, Image.Resampling.LANCZOS)
    layer.alpha_composite(inset, ((layer.width - inset.width) // 2, (layer.height - inset.height) // 2))


def _master_date_layout(elements, value, font, box):
    left, top, right, bottom = box
    free_left = 0
    for other in elements:
        x, y = _pixels(other["x"]), _pixels(other["y"])
        if x < left and y < bottom and _pixels(other["y"] + other["h"]) > top:
            # Keep a pixel between the date and any neighbouring field on the same row.
            free_left = max(free_left, min(left, _pixels(other["x"] + other["w"]) + 1))
    minimum = min(font.size, _pixels(1.5))
    while True:
        x0, _, x1, height = font.getbbox(value, anchor="lt")
        width = ceil(max(font.getlength(value), x1) - min(0, x0))
        if width <= right - free_left and height <= bottom - top:
            return min(left, right - width), font
        if font.size <= minimum:
            return free_left, font
        ratio = min((right - free_left) / max(width, 1), (bottom - top) / max(height, 1))
        font = font.font_variant(size=max(minimum, min(font.size - 1, int(font.size * ratio))))


def render_label(template, fields):
    template = validate_template("part", template)
    label = Image.new("RGBA", (_pixels(template["widthMm"]), _pixels(template["heightMm"])), "white")
    warnings, fonts = [], {}
    for element in template["elements"]:
        field = element["field"]
        left, top = _pixels(element["x"]), _pixels(element["y"])
        right, bottom = _pixels(element["x"] + element["w"]), _pixels(element["y"] + element["h"])
        layer = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
        value = fields.get(field)
        fits = True
        if field == "contour":
            fits = _draw_contour(layer, value)
        elif field == "nap":
            fits = _draw_nap(layer, value)
        elif field == "image":
            _draw_image(layer, element.get("image"))
        else:
            if field == "text":
                value = element.get("text", "")
            if value is not None and value != "":
                size = max(1, _pixels(element.get("fontMm", 3)))
                if size not in fonts:
                    fonts[size] = ImageFont.truetype(str(FONT_PATH), size)
                font = fonts[size]
                if field == "date" and fields.get("_labelKind") == "master":
                    left, font = _master_date_layout(template["elements"], str(value), font, (left, top, right, bottom))
                    if right - left > layer.width:
                        layer = Image.new("RGBA", (right - left, layer.height))
                fits = not _draw_text(layer, value, font)
        if not fits:
            warnings.append(f"Содержимое не помещается в поле «{element['id']}».")
        label.alpha_composite(layer, (left, top))
    result = BytesIO()
    label.convert("RGB").save(result, format="PNG", dpi=(DPI, DPI))
    return result.getvalue(), warnings


def label_pdf(pages):
    result = BytesIO()
    pdf = canvas.Canvas(result, pageCompression=1)
    for template, fields in pages:
        png, _ = render_label(template, fields)
        width, height = template["widthMm"] * mm, template["heightMm"] * mm
        pdf.setPageSize((width, height))
        pdf.drawImage(ImageReader(BytesIO(png)), 0, 0, width, height)
        pdf.showPage()
    pdf.save()
    return result.getvalue()
