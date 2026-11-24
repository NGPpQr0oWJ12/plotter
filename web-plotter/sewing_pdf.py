"""Vector work sheets for a sewing set or an individual fabric cut."""

from base64 import b64encode
from collections import defaultdict
from functools import cache, partial
from io import BytesIO
from itertools import chain
from math import ceil, isfinite, log
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree.ElementTree import Element, SubElement, tostring

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle


FONT = "PlotterGostTypeB"
FONT_PATH = Path(__file__).with_name("static") / "OpenGostTypeB-Regular.ttf"
FRAME_LEFT, FRAME_RIGHT = 20 * mm, A4[0] - 5 * mm
FRAME_BOTTOM, FRAME_TOP = 5 * mm, A4[1] - 5 * mm
LEFT, RIGHT = FRAME_LEFT + 6 * mm, FRAME_RIGHT - 6 * mm
TOP, BOTTOM = FRAME_TOP - 23 * mm, FRAME_BOTTOM + 24 * mm
WIDTH = RIGHT - LEFT
INK = colors.black
MUTED = RULE = INK
EMPTY = "–"
DRAWING_FOOT = 49 * mm
SHEET_GAP = 4 * mm


@cache
def _register_font():
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_PATH)))


@cache
def _preview_font_style():
    encoded = b64encode(FONT_PATH.read_bytes()).decode("ascii")
    return f'@font-face{{font-family:{FONT};src:url(data:font/ttf;base64,{encoded}) format("truetype")}}'


def _numbers(*values):
    return " ".join(f"{value:.5f}".rstrip("0").rstrip(".") for value in values)


class _PreviewPath:
    def __init__(self):
        self.commands = []

    def moveTo(self, x, y):
        self.commands.append("M " + _numbers(x, y))

    def lineTo(self, x, y):
        self.commands.append("L " + _numbers(x, y))

    def close(self):
        self.commands.append("Z")

    def rect(self, x, y, width, height):
        self.moveTo(x, y)
        self.lineTo(x + width, y)
        self.lineTo(x + width, y + height)
        self.lineTo(x, y + height)
        self.close()

    def svg(self):
        return " ".join(self.commands)


class _PreviewCanvas:
    """The line, path and text operations used by the shared drawing sheet."""

    def __init__(self, title, width, height):
        self.root = Element("svg", xmlns="http://www.w3.org/2000/svg", viewBox=_numbers(0, 0, width, height),
                            width=_numbers(width), height=_numbers(height), role="img")
        SubElement(self.root, "title").text = title
        SubElement(self.root, "style").text = _preview_font_style()
        SubElement(self.root, "rect", x="0", y="0", width="100%", height="100%", fill="white")
        self.defs = SubElement(self.root, "defs")
        self.group = SubElement(self.root, "g", transform=f"translate(0 {height}) scale(1 -1)")
        self.stroke = self.fill = "#000000"
        self.line_width = 1
        self.cap = "butt"
        self.join = "miter"
        self.dash = ""
        self.font = FONT
        self.font_size = 12
        self.stack = []

    def saveState(self):
        self.stack.append((self.group, self.stroke, self.fill, self.line_width, self.cap,
                           self.join, self.dash, self.font, self.font_size))
        self.group = SubElement(self.group, "g")

    def restoreState(self):
        (self.group, self.stroke, self.fill, self.line_width, self.cap,
         self.join, self.dash, self.font, self.font_size) = self.stack.pop()

    def translate(self, x, y):
        self.group = SubElement(self.group, "g", transform=f"translate({_numbers(x, y)})")

    def rotate(self, angle):
        self.group = SubElement(self.group, "g", transform=f"rotate({angle})")

    def setLineWidth(self, width):
        self.line_width = width

    def setLineCap(self, cap):
        self.cap = ("butt", "round", "square")[cap]

    def setLineJoin(self, join):
        self.join = ("miter", "round", "bevel")[join]

    def setDash(self, dash=()):
        self.dash = _numbers(*dash)

    def setStrokeColor(self, color):
        self.stroke = colors.toColor(color).hexval().replace("0x", "#")

    def setFillColor(self, color):
        self.fill = colors.toColor(color).hexval().replace("0x", "#")

    def setFont(self, font, size, leading=None):
        self.font, self.font_size = font, size

    def beginPath(self):
        return _PreviewPath()

    def drawPath(self, path, stroke=1, fill=0):
        attrs = {"stroke": self.stroke if stroke else "none", "fill": self.fill if fill else "none",
                 "stroke-width": _numbers(self.line_width), "stroke-linecap": self.cap, "stroke-linejoin": self.join}
        if self.dash:
            attrs["stroke-dasharray"] = self.dash
        SubElement(self.group, "path", d=path.svg(), **attrs)

    def clipPath(self, path, stroke=0, fill=0):
        ident = f"clip-{len(self.defs)}"
        SubElement(SubElement(self.defs, "clipPath", id=ident), "path", d=path.svg())
        self.group = SubElement(self.group, "g", {"clip-path": f"url(#{ident})"})
        if stroke or fill:
            self.drawPath(path, stroke=stroke, fill=fill)

    def rect(self, x, y, width, height, stroke=1, fill=0):
        path = self.beginPath()
        path.rect(x, y, width, height)
        self.drawPath(path, stroke=stroke, fill=fill)

    def line(self, x0, y0, x1, y1):
        self.lines(((x0, y0, x1, y1),))

    def lines(self, segments):
        path = self.beginPath()
        for x0, y0, x1, y1 in segments:
            path.moveTo(x0, y0)
            path.lineTo(x1, y1)
        self.drawPath(path)

    def drawString(self, x, y, value):
        if not value:
            return
        length = pdfmetrics.stringWidth(value, self.font, self.font_size)
        attributes = {"font-family": self.font + ",sans-serif", "font-size": _numbers(self.font_size),
                      "lengthAdjust": "spacingAndGlyphs", "textLength": _numbers(length), "fill": self.fill}
        SubElement(self.group, "text", transform=f"translate({_numbers(x, y)}) scale(1 -1)", **attributes).text = value

    def drawCentredString(self, x, y, value):
        width = pdfmetrics.stringWidth(value, self.font, self.font_size)
        self.drawString(x - width / 2, y, value)

    def draw_paragraph(self, paragraph):
        style = paragraph.style
        self.setFont(style.fontName, style.fontSize)
        self.setFillColor(style.textColor)
        y = paragraph.height - style.fontSize
        for line in paragraph.blPara.lines:
            text = ("".join(fragment.text for fragment in line.words) if paragraph.blPara.kind
                    else " ".join(line[1]))
            self.drawString(0, y, text)
            y -= style.leading

    def complete(self):
        return tostring(self.root, encoding="utf-8", xml_declaration=True)


class _SheetParagraph(Paragraph):
    def draw(self):
        if isinstance(self.canv, _PreviewCanvas):
            self.canv.draw_paragraph(self)
        else:
            super().draw()


@cache
def _style(size, tone=INK):
    return ParagraphStyle("Sewing", fontName=FONT, fontSize=size, leading=size * 1.23, textColor=tone)


def _para(value, size=11, tone=INK):
    text = escape(str(value if value not in (None, "") else EMPTY))
    return _SheetParagraph(text.replace("\r\n", "\n").replace("\n", "<br/>"), _style(size, tone))


def _text(pdf, value, x, top, width, size=11, tone=INK):
    paragraph = _para(value, size, tone)
    _, height = paragraph.wrap(width, A4[1])
    paragraph.drawOn(pdf, x, top - height)
    return top - height


def _line_text(pdf, value, x, y, size=10, tone=INK):
    pdf.setFillColor(tone)
    pdf.setFont(FONT, size)
    pdf.drawString(x, y, str(value))


def _mm(value):
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _bounds(paths):
    points = iter(chain.from_iterable(path.get("points", ()) for path in paths))
    first = next(points, None)
    if first is None:
        return None
    x0, y0 = x1, y1 = first
    for x, y in points:
        x0, y0 = min(x0, x), min(y0, y)
        x1, y1 = max(x1, x), max(y1, y)
    return x0, y0, x1, y1


def _size(bounds):
    if bounds is None:
        return EMPTY
    x0, y0, x1, y1 = bounds
    return f"{_mm(x1 - x0)} × {_mm(y1 - y0)}"


def _piece_bounds(part):
    paths = part["paths"]
    return (_bounds(path for path in paths if path.get("closed") and not path.get("notch"))
            or _bounds(path for path in paths if not path.get("notch")) or _bounds(paths))


def _frame(pdf, section, index, count):
    pdf.setStrokeColor(INK)
    pdf.setLineWidth(.9)
    pdf.rect(FRAME_LEFT, FRAME_BOTTOM, FRAME_RIGHT - FRAME_LEFT, FRAME_TOP - FRAME_BOTTOM)
    top = FRAME_BOTTOM + 17 * mm
    pdf.line(FRAME_LEFT, top, FRAME_RIGHT, top)
    cells = (FRAME_RIGHT - 44 * mm, FRAME_RIGHT - 29 * mm, FRAME_RIGHT - 14 * mm)
    for x in cells:
        pdf.line(x, FRAME_BOTTOM, x, top)
    _line_text(pdf, "Наименование листа", FRAME_LEFT + 4 * mm, top - 5 * mm, 10)
    _line_text(pdf, section, FRAME_LEFT + 4 * mm, FRAME_BOTTOM + 4 * mm, 13)
    for x, label, value in zip(cells, ("Лист", "Листов", "Формат"), (index, count, "А4")):
        _line_text(pdf, label, x + 2 * mm, top - 5 * mm, 10)
        _line_text(pdf, value, x + 2 * mm, FRAME_BOTTOM + 4 * mm, 13)
    if index > 1:
        _text(pdf, section, LEFT, FRAME_TOP - 8 * mm, WIDTH, 22)


def _cover(title, parts, context, workspace, pdf):
    y = FRAME_TOP - 43 * mm
    y = _text(pdf, "Карта пошива", LEFT, y, WIDTH, 24)
    y = _text(pdf, title, LEFT, y - 6 * mm, WIDTH, 29)
    pdf.setLineWidth(.65)
    pdf.line(LEFT, y - 9 * mm, RIGHT, y - 9 * mm)
    y -= 20 * mm
    labels = (("Номер заказа", "number"), ("Что кроим", "designation"), ("Изделие", "modelName"), ("Ткань", "fabric"))
    fields = [(label, context[key]) for label, key in labels if context.get(key)]
    width = _material_width(context, workspace)
    if width:
        fields.append(("Ширина полотна", f"{_mm(width)} мм"))
    fields.append(("Деталей", f"{len(parts)} шт."))
    for label, value in fields:
        _text(pdf, label, LEFT, y, 40 * mm, 10, MUTED)
        y = _text(pdf, value, LEFT + 45 * mm, y, WIDTH - 45 * mm, 13) - 5 * mm
    _text(pdf, "Размеры указаны в миллиметрах. Контуры и схемы приведены не в натуральную величину.",
          LEFT, BOTTOM + 26 * mm, WIDTH, 11, MUTED)


def _table_pages(section, headers, widths, rows, note):
    note_height = _para(note, 10, MUTED).wrap(WIDTH, A4[1])[1] + 4 * mm if note else 0
    available = TOP - BOTTOM - note_height
    table = Table([[_para(header, 10) for header in headers],
                   *[[_para(value) for value in row] for row in rows]],
                  colWidths=[WIDTH * width for width in widths], repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), .75, INK),
        ("LINEBELOW", (0, 0), (-1, 0), .75, INK),
        ("LINEBELOW", (0, 1), (-1, -1), .35, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    while table is not None:
        _, height = table.wrap(WIDTH, available)
        if height > available:
            pieces = table.split(WIDTH, available)
            if len(pieces) != 2:
                raise ValueError("Строка документа не помещается на листе.")
            page, table = pieces
        else:
            page, table = table, None
        yield section, partial(_draw_table, page, note, note_height)


def _draw_table(table, note, note_height, pdf):
    if note:
        _text(pdf, note, LEFT, TOP, WIDTH, 10, MUTED)
    _, height = table.wrap(WIDTH, TOP - BOTTOM - note_height)
    table.drawOn(pdf, LEFT, TOP - note_height - height)


def _paths(pdf, paths, project, *, weight=.8):
    for item in paths:
        points = item.get("points", ())
        if len(points) < 2:
            continue
        path = pdf.beginPath()
        path.moveTo(*project(points[0]))
        for point in points[1:]:
            path.lineTo(*project(point))
        if item.get("closed"):
            path.close()
        pdf.setLineWidth(weight * .75 if item.get("notch") else weight)
        pdf.setStrokeColor(INK)
        pdf.drawPath(path)


def _clip(pdf, box):
    path = pdf.beginPath()
    path.rect(*box)
    pdf.clipPath(path, stroke=0, fill=0)


DIMENSION_OFFSET = 10 * mm
DIAGRAM_MARGINS = (4 * mm, DIMENSION_OFFSET + 2 * mm, DIMENSION_OFFSET + 2 * mm, 3 * mm)


def _diagram_scale(bounds, width, height):
    if bounds is None:
        return 1.0
    left, bottom, right, top = DIAGRAM_MARGINS
    return min(max(1, width - left - right) / max(bounds[2] - bounds[0], 1),
               max(1, height - bottom - top) / max(bounds[3] - bounds[1], 1))


def _point_marks(pdf, marks, project):
    pdf.setStrokeColor(INK)
    pdf.setLineWidth(.7)
    for mark in marks:
        x, y = project(mark["point"])
        pdf.lines([(x - 2, y + 3, x, y), (x, y, x + 2, y + 3)])


def _gabarit_label(pdf, value, x, y, *, vertical=False):
    pdf.saveState()
    pdf.translate(x, y)
    if vertical:
        pdf.rotate(90)
    pdf.setFillColor(INK)
    pdf.setFont(FONT, 12)
    pdf.drawCentredString(0, 0, _mm(value))
    pdf.restoreState()


def _dimension_line(pdf, start, end):
    x0, y0 = start
    x1, y1 = end
    size = max(x1 - x0, y1 - y0)
    if size <= 0:
        return
    dx, dy = (x1 - x0) / size, (y1 - y0) / size
    head = 4.5
    outside = size < 4 * head
    extension = head + 2 if outside else 0
    pdf.line(x0 - dx * extension, y0 - dy * extension, x1 + dx * extension, y1 + dy * extension)
    pdf.setFillColor(INK)
    for x, y, sign in ((x0, y0, 1), (x1, y1, -1)):
        tail = -sign * head if outside else sign * head
        bx, by = x + dx * tail, y + dy * tail
        arrow = pdf.beginPath()
        arrow.moveTo(x, y)
        arrow.lineTo(bx - dy * 1.3, by + dx * 1.3)
        arrow.lineTo(bx + dy * 1.3, by - dx * 1.3)
        arrow.close()
        pdf.drawPath(arrow, fill=1, stroke=0)


def _gabarit(pdf, plot, dx, dy):
    left, bottom, right, top = plot
    dim_x, dim_y = right + DIMENSION_OFFSET, bottom - DIMENSION_OFFSET
    pdf.setStrokeColor(INK)
    pdf.setLineWidth(.4)
    pdf.lines([
        (left, bottom - mm, left, dim_y - mm),
        (right, bottom - mm, right, dim_y - mm),
        (right + mm, bottom, dim_x + mm, bottom),
        (right + mm, top, dim_x + mm, top),
    ])
    _dimension_line(pdf, (left, dim_y), (right, dim_y))
    _dimension_line(pdf, (dim_x, bottom), (dim_x, top))
    _gabarit_label(pdf, dx, (left + right) / 2, dim_y + 2)
    _gabarit_label(pdf, dy, dim_x - 2, (bottom + top) / 2, vertical=True)


def _part_diagram(pdf, part, bounds, slot):
    x, y, width, height = slot
    if not bounds:
        _text(pdf, "Контур отсутствует", x, y + height, width, 10, MUTED)
        return
    x0, y0, x1, y1 = bounds
    dx, dy = x1 - x0, y1 - y0
    ml, mb, mr, mt = DIAGRAM_MARGINS
    scale = _diagram_scale(bounds, width, height)
    left = x + ml + max(0, width - ml - mr - dx * scale) / 2
    bottom = y + mb + max(0, height - mb - mt - dy * scale) / 2

    def project(point):
        return left + (point[0] - x0) * scale, bottom + (point[1] - y0) * scale

    pdf.saveState()
    _clip(pdf, slot)
    _paths(pdf, part["paths"], project)
    right, top = project((x1, y1))
    plot = left, bottom, right, top
    _point_marks(pdf, part.get("marks") or (), project)
    _gabarit(pdf, plot, dx, dy)
    pdf.restoreState()


def _legend_fields(part):
    legend = part.get("partLegend") or {}
    return [("Разработчик", legend.get("developer") or ""),
            ("Дата составления", legend.get("date") or EMPTY),
            ("Особые комментарии", legend.get("comments") or EMPTY)]


def _legend_cell(value, width):
    size = 11.5
    paragraph = _para(value, size)
    limit = _style(size).leading * 2 + .1
    if paragraph.wrap(width, limit)[1] <= limit:
        return paragraph, False
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if _para(value[:middle].rstrip() + "…*", size).wrap(width, limit)[1] <= limit:
            low = middle
        else:
            high = middle - 1
    return _para(value[:low].rstrip() + "…*", size), True


def _piece_legend(entries, *, full=False, width=WIDTH * .76, size=11.5, compact=False):
    numbered = not full or any(part["number"] for part, _ in entries)
    if numbered:
        columns = [.09, .25, .13, .53]
        fields = ("№", "Разработчик", "Дата", "Комментарии")
    else:
        columns = [.22, .13, .65]
        fields = ("Разработчик", "Дата", "Комментарии")
    columns = [fraction * width for fraction in columns]
    data = [[_para(field, size) for field in fields]]
    notes = []
    for part, _ in entries:
        fields = _legend_fields(part)
        values = [value for _, value in fields]
        if numbered:
            values.insert(0, part["number"])
        if full:
            data.append([_para(" ".join(str(value).split()) if compact else value, size) for value in values])
        else:
            cells = [_legend_cell(str(value), column - 12) for value, column in zip(values, columns)]
            data.append([paragraph for paragraph, _ in cells])
            if any(overflow for _, overflow in cells):
                notes.append((f"№ {part['number']}  {part['name']}",
                              "\n".join(f"{field}  {value}" for field, value in fields)))
    table = Table(data, colWidths=columns)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), .45, RULE),
        ("BOX", (0, 0), (-1, -1), .75, INK),
        ("LINEBELOW", (0, 0), (-1, 0), .75, INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table, notes


def _piece_plan(entries):
    plans = []
    count = len(entries)
    height = TOP - BOTTOM - DRAWING_FOOT
    for rows in (True, False):
        card_width = WIDTH if rows else (WIDTH - (count - 1) * SHEET_GAP) / count
        card_height = (height - (count - 1) * SHEET_GAP) / count if rows else height
        score, drawings = 0, []
        for index, (part, bounds) in enumerate(entries):
            x = LEFT if rows else LEFT + index * (card_width + SHEET_GAP)
            y = TOP - index * (card_height + SHEET_GAP) if rows else TOP
            title = f"№ {part['number']}  {part['name']}" if part["number"] else part["name"]
            heading = _para(title, 14)
            _, caption_height = heading.wrap(card_width, card_height)
            diagram_height = card_height - caption_height - 4 * mm
            score += log(_diagram_scale(bounds, card_width, diagram_height))
            drawings.append((part, bounds, heading, (x, y - card_height, card_width, diagram_height)))
        plans.append((score, drawings))
    return max(plans, key=lambda plan: plan[0])[1]


def _piece_sheet(drawings, table, has_notes, pdf):
    for part, bounds, heading, slot in drawings:
        x, y, width, height = slot
        heading.drawOn(pdf, x, y + height + 2 * mm)
        _part_diagram(pdf, part, bounds, slot)
    width, _ = table.wrap(WIDTH, DRAWING_FOOT)
    table.drawOn(pdf, RIGHT - width, BOTTOM)
    lines = ["Размеры в мм. Масштаб условный."]
    if has_notes:
        lines.append("* Полный текст на листе «Примечания к деталям».")
    _text(pdf, "\n".join(lines), LEFT, BOTTOM + DRAWING_FOOT - 2 * mm, WIDTH - width - 4 * mm, 10, MUTED)


def render_part_preview_svg(part):
    """Render the part drawing and its full properties without the paper frame."""
    _register_font()
    bounds = _piece_bounds(part)
    entries = [(part, bounds)]
    diagram_width = diagram_height = 0
    if bounds:
        x0, y0, x1, y1 = bounds
        dx, dy = x1 - x0, y1 - y0
        scale = min(560 / max(dx, 1), 400 / max(dy, 1))
        ml, mb, mr, mt = DIAGRAM_MARGINS
        diagram_width = dx * scale + ml + mr
        diagram_height = dy * scale + mb + mt
    preferred = max(350, diagram_width)
    for width, size, compact in ((preferred, 11.5, False), (620, 11.5, False), (620, 10, False),
                                 (620, 10, True), (620, 8.5, True)):
        legend, _ = _piece_legend(entries, full=True, width=width, size=size, compact=compact)
        _, legend_height = legend.wrap(width, 420)
        if legend_height <= 420:
            break
    padding = 12
    gap = 14 if bounds else 0
    sheet = _PreviewCanvas(part["name"], width + 2 * padding, diagram_height + legend_height + gap + 2 * padding)
    if bounds:
        left = padding + (width - diagram_width) / 2
        _part_diagram(sheet, part, bounds, (left, padding + legend_height + gap, diagram_width, diagram_height))
    legend.drawOn(sheet, padding, padding)
    return sheet.complete()


def _seam_rows(parts):
    numbers = defaultdict(list)
    for part in parts:
        pattern = part.get("pattern") or {}
        if pattern.get("number"):
            numbers[pattern["number"]].append(part["number"])
    rows = []
    for part in parts:
        for seam in (part.get("pattern") or {}).get("sewing", ()):
            others = numbers.get(seam["other"], ())
            label = f"{seam['name']} / {seam['other']}"
            if others:
                label += " (№ " + ", ".join(map(str, others)) + ")"
            else:
                label += " (вне комплекта)"
            if seam.get("method") == "closure":
                label += " · застёжка"
            rows.append((part["number"], part["name"], label))
    return rows or [(EMPTY, EMPTY, "Сохранённых парных швов нет.")]


def _saved_paths(workspace):
    excluded = set(workspace.get("excluded") or ())
    included = [path for index, path in enumerate(workspace.get("paths", ())) if index not in excluded]
    return included, excluded


def _cut_rows(parts, workspace):
    if workspace is None:
        directions = {"grain": "Долевая", "nap": "Ворс", "free": "Свободное"}
        return [(part["number"], part["name"], _size(_piece_bounds(part)),
                 directions.get((part.get("pattern") or {}).get("direction"), EMPTY)) for part in parts]
    paths, excluded = _saved_paths(workspace)
    included = {path.get("partNumber") for path in paths}
    omitted = {path.get("partNumber") for index, path in enumerate(workspace.get("paths", ())) if index in excluded}
    rows = []
    for part in parts:
        number = part["number"]
        status = "Включена" if number in included else "Исключена" if number in omitted else "Не размещена"
        rows.append((number, part["name"], _size(_piece_bounds(part)), status))
    return rows


def _material_width(context, workspace):
    nesting = (workspace or {}).get("optimization", {}).get("nesting", {})
    width = nesting.get("widthMm") or context.get("widthMm")
    return width if isinstance(width, (int, float)) and isfinite(width) and width > 0 else None


def _layout_pages(workspace, context):
    paths, excluded = _saved_paths(workspace)
    extent = _bounds(paths)
    if extent is None:
        yield "Раскладка полотна", partial(_empty_layout, len(excluded))
        return
    x0, y0, x1, y1 = extent
    x0, y0 = min(0, x0), min(0, y0)
    material_width = _material_width(context, workspace)
    y1 = max(y1, material_width or 0, y0 + 1)
    length = max(1, x1 - x0)
    count = min(24, max(1, ceil(length / (1.35 * (y1 - y0)))))
    step = length / count
    source = [(path, _bounds((path,))) for path in paths if path.get("points")]
    for page in range(count):
        start, end = x0 + page * step, x0 + (page + 1) * step
        visible = [path for path, box in source if box[2] >= start and box[0] <= end]
        yield "Раскладка полотна", partial(
            _layout_sheet, visible, (start, y0, end, y1), len(excluded), page + 1, count, material_width)


def _empty_layout(excluded, pdf):
    _text(pdf, f"На сохранённой раскладке нет включённых контуров. Исключено контуров: {excluded}.",
          LEFT, TOP, WIDTH, 11)


def _layout_sheet(paths, bounds, excluded, page, count, material_width, pdf):
    start, y0, end, y1 = bounds
    caption = f"Участок {page} из {count}"
    if material_width:
        caption += f" · полотно {_mm(material_width)} мм"
    if excluded:
        caption += f" · исключено контуров: {excluded}"
    _text(pdf, caption, LEFT, TOP, WIDTH, 11)
    _text(pdf, "Размеры в мм. Масштаб условный.", LEFT, TOP - 7 * mm, WIDTH, 10)
    available_height = TOP - BOTTOM - 22 * mm
    scale = min(WIDTH / (y1 - y0), available_height / (end - start))
    width, height = (y1 - y0) * scale, (end - start) * scale
    left, bottom = LEFT + (WIDTH - width) / 2, TOP - 19 * mm - height

    def project(point):
        return left + (point[1] - y0) * scale, bottom + (point[0] - start) * scale

    pdf.saveState()
    _clip(pdf, (left, bottom, width, height))
    _paths(pdf, paths, project, weight=.7)
    groups = defaultdict(list)
    for path in paths:
        if path.get("partNumber"):
            groups[path["partNumber"]].append(path)
    for number, contours in groups.items():
        x0, low, x1, high = _bounds(contours)
        px, py = project(((max(x0, start) + min(x1, end)) / 2, (low + high) / 2))
        label = f"№ {number}"
        size = pdfmetrics.stringWidth(label, FONT, 9)
        pdf.setFillColor(colors.white)
        pdf.rect(px - size / 2 - 2, py - 3, size + 4, 10, fill=1, stroke=0)
        _line_text(pdf, label, px - size / 2, py, 9)
    pdf.restoreState()
    pdf.setStrokeColor(RULE)
    pdf.setLineWidth(.65)
    pdf.rect(left, bottom, width, height)


def render_sewing_pdf(title, parts, *, context=None, workspace=None):
    """Render numbered vector sheets and optional saved roll geometry."""
    _register_font()
    context = context or {}
    parts = list(parts)
    bounds = [_piece_bounds(part) for part in parts]
    pages = [("Титульный лист", partial(_cover, title, parts, context, workspace))]
    cards = list(zip(parts, bounds))
    notes = []
    for index in range(0, len(cards), 3):
        group = cards[index:index + 3]
        drawings = _piece_plan(group)
        legend, continued = _piece_legend(group)
        notes.extend(continued)
        pages.append(("Чертежи деталей", partial(_piece_sheet, drawings, legend, bool(continued))))
    pages.extend(_table_pages("Перечень деталей", ("№", "Наименование", "Габарит, мм"), (.10, .65, .25),
                              [(part["number"], part["name"], _size(box)) for part, box in zip(parts, bounds)],
                              "Каждый номер соответствует одной физической детали."))
    if notes:
        pages.extend(_table_pages("Примечания к деталям", ("Деталь", "Основная надпись"), (.33, .67), notes, ""))
    pages.extend(_table_pages("Карта пошива", ("№", "Деталь", "Шов и сопряжение"), (.10, .42, .48),
                              _seam_rows(parts), ""))
    if workspace is None:
        pages.extend(_table_pages("Карта раскроя", ("№", "Деталь", "Габарит, мм", "Направление"), (.08, .48, .24, .20),
                                  _cut_rows(parts, workspace), "Положение деталей задаётся при раскладке полотна."))
    else:
        pages.extend(_table_pages("Карта раскроя", ("№", "Деталь", "Габарит, мм", "В раскладке"),
                                  (.08, .48, .24, .20), _cut_rows(parts, workspace), ""))
        pages.extend(_layout_pages(workspace, context))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(title)
    for index, (section, draw) in enumerate(pages, 1):
        _frame(pdf, section, index, len(pages))
        draw(pdf)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
