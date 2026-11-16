"""Editable model-space CAD entities and their derived cutting geometry."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from codecs import BOM_UTF8
from io import BytesIO, TextIOWrapper
from itertools import islice, pairwise
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import ezdxf
from ezdxf import recover, units
from ezdxf.lldxf.tagger import ascii_tags_loader, binary_tags_loader
from ezdxf.math import BSpline, Matrix44, Vec3, arc_angle_span_deg, bulge_to_arc, open_uniform_knot_vector
from shapely import line_merge
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import substring
from shapely.strtree import STRtree

from cad import (
    CadImportError, CURVE_TOLERANCE_MM, MANUAL_CLOSURE_TOLERANCE_MM,
    MAX_ABS_COORDINATE_MM, MAX_ENTITIES, MAX_FILE_BYTES, MAX_POINTS,
    PLANAR_EPSILON_MM, POINT_EPSILON_MM, VECTOR_TYPES, _convert_dwg,
    repair_closed_contours,
)


class CadDesignError(ValueError):
    """The drawing or requested edit cannot be represented in the designer."""


class _RepairConflict(CadDesignError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


class _CurveDetailLimit(CadDesignError):
    """The requested curve resolution exceeds a sampling limit."""


DESIGN_CURVE_TOLERANCE_MM = 0.01
MAX_NOTCH_SERIES = 100
MAX_GROUP_DEPTH = 32
MIN_NOTCH_SPACING_MM = 0.5
NOTCH_BIND_TOLERANCE_MM = CURVE_TOLERANCE_MM
BINARY_DXF_SIGNATURE = b"AutoCAD Binary DXF\r\n\x1a\x00"
IMPORT_DUPLICATE_POINTS = "Удалены повторяющиеся вершины или нулевые рёбра."
IMPORT_INVALID_VERTEX = "Пропущены рёбра у вершины с недопустимыми координатами."
IMPORT_INVALID_BULGE = "Пропущено ребро с недопустимой кривизной."
IMPORT_DOUBLED_LINE = "Замкнутый контур из двух совпадающих рёбер сохранён как открытый отрезок."
IMPORT_BAD_CURVE = "Пропущены дуги, которые не удалось восстановить после преобразования блока."
IMPORT_BAD_LAYER = "Исправлены нечитаемые символы в имени слоя."
IMPORT_AUDIT = "Проверка DXF обнаружила ошибку в этом объекте."
LEGACY_IMPORT_ISSUE = "Повреждение отмечено при импорте. Точная причина в этом чертеже не сохранена."


def _number(value, description, limit=MAX_ABS_COORDINATE_MM):
    if type(value) not in (int, float) or not -limit <= value <= limit:
        raise CadDesignError(f"Недопустимое значение: {description}.")
    return float(value)


def _positive(value, description):
    value = _number(value, description)
    if value <= 0:
        raise CadDesignError(f"{description} должно быть больше нуля.")
    return value


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CadDesignError("У точки должны быть две координаты.")
    return [_number(coordinate, "координата") for coordinate in value]


def _text(value, description, length):
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise CadDesignError(f"Недопустимое значение: {description}.")
    return value


def _flag(value, description):
    if type(value) is not bool:
        raise CadDesignError(f"Недопустимое значение: {description}.")
    return value


def _points(values, minimum):
    if not isinstance(values, list) or not minimum <= len(values) <= MAX_POINTS:
        raise CadDesignError("Недопустимое число точек объекта.")
    return [_point(point) for point in values]


def _spline(entity, clean):
    closed = _flag(entity.get("closed", False), "замыкание сплайна")
    clean["closed"] = closed
    if "fitPoints" in entity:
        clean["fitPoints"] = _points(entity["fitPoints"], 3 if closed else 2)
        if "fitMethod" in entity:
            if entity["fitMethod"] != "centripetal":
                raise CadDesignError("Неизвестный способ построения сплайна.")
            clean["fitMethod"] = "centripetal"
            points = clean["fitPoints"]
            pairs = pairwise([*points, points[0]] if closed else points)
            if any(math.dist(a, b) <= POINT_EPSILON_MM for a, b in pairs):
                raise CadDesignError("Соседние точки сплайна должны различаться.")
        return len(clean["fitPoints"])

    controls = _points(entity.get("controlPoints"), 2)
    degree = entity.get("degree")
    if type(degree) is not int or not 1 <= degree < min(len(controls), 33):
        raise CadDesignError("Недопустимая степень сплайна.")
    knots = entity.get("knots")
    if not isinstance(knots, list) or len(knots) != len(controls) + degree + 1:
        raise CadDesignError("Недопустимое число узлов сплайна.")
    knots = [_number(knot, "узел сплайна", 1e100) for knot in knots]
    first, last = knots[degree], knots[len(controls)]
    interior = Counter(knot for knot in knots if first < knot < last)
    if (first >= last or any(left > right for left, right in pairwise(knots))
            or any(count > degree for count in interior.values())):
        raise CadDesignError("Недопустимая последовательность узлов сплайна.")
    weights = entity.get("weights", [])
    if not isinstance(weights, list) or len(weights) not in (0, len(controls)):
        raise CadDesignError("Недопустимое число весов сплайна.")
    clean.update(controlPoints=controls, degree=degree, knots=knots,
                 weights=[_positive(weight, "Вес сплайна") for weight in weights])
    return len(controls)


def _group_path(value):
    if not isinstance(value, list) or len(value) > MAX_GROUP_DEPTH:
        raise CadDesignError("Недопустимый путь группы.")
    return [_text(group, "идентификатор группы", 128) for group in value]


def _entity(entity):
    if not isinstance(entity, dict):
        raise CadDesignError("Неверный объект чертежа.")
    kind = entity.get("type")
    clean = {"id": _text(entity.get("id"), "идентификатор объекта", 128),
             "type": kind, "layer": _text(entity.get("layer", "0"), "слой", 255)}
    for flag in ("reference", "notch", "importDamaged"):
        if flag in entity:
            clean[flag] = _flag(entity[flag], flag)
    if "importIssues" in entity:
        issues = entity["importIssues"]
        if not isinstance(issues, list) or len(issues) > 12:
            raise CadDesignError("Некорректный список замечаний к импортированному объекту.")
        if issues:
            clean["importIssues"] = list(dict.fromkeys(_text(issue, "замечание к импорту", 255) for issue in issues))
            clean["importDamaged"] = True
    if "parentId" in entity:
        if not clean.get("notch"):
            raise CadDesignError("Связь с контуром должна принадлежать насечке.")
        clean["parentId"] = _text(entity["parentId"], "контур насечки", 128)
    if "groupPath" in entity:
        path = _group_path(entity["groupPath"])
        if path and "parentId" not in clean:
            clean["groupPath"] = path

    count = 1
    if kind == "point":
        if clean.get("notch"):
            raise CadDesignError("Вспомогательная точка не может быть надсечкой.")
        clean.update(point=_point(entity.get("point")), reference=True)
    elif kind == "line":
        start, end = _point(entity.get("start")), _point(entity.get("end"))
        if math.dist(start, end) <= POINT_EPSILON_MM:
            raise CadDesignError("Длина линии слишком мала.")
        clean.update(start=start, end=end)
        count = 2
    elif kind == "polyline":
        closed = _flag(entity.get("closed", False), "замыкание полилинии")
        points = _points(entity.get("points"), 2)
        clean.update(points=points, closed=closed)
        if "joined" in entity:
            clean["joined"] = _flag(entity["joined"], "соединённый объект")
        if "bulges" in entity:
            bulges = entity["bulges"]
            if not isinstance(bulges, list) or len(bulges) != len(points):
                raise CadDesignError("Недопустимое число дуговых сегментов полилинии.")
            clean["bulges"] = [_number(value, "кривизна сегмента") for value in bulges]
        if closed and len(points) < 3 and not any(clean.get("bulges", [])):
            raise CadDesignError("Замкнутая полилиния должна содержать три точки или дуговые сегменты.")
        count = len(points)
    elif kind == "spline":
        count = _spline(entity, clean)
    elif kind in ("circle", "arc", "ellipse"):
        clean["center"] = _point(entity.get("center"))
        if kind == "ellipse":
            axis = _point(entity.get("axis"))
            ratio = _positive(entity.get("ratio"), "Отношение осей")
            if math.hypot(*axis) <= POINT_EPSILON_MM or ratio > 1:
                raise CadDesignError("Недопустимые оси эллипса.")
            clean.update(axis=axis, ratio=ratio)
        else:
            clean["radius"] = _positive(entity.get("radius"), "Радиус")
        if kind != "circle":
            sweep = _number(entity.get("sweep"), "угол дуги", 360)
            if abs(sweep) < 1e-9 or kind == "arc" and abs(sweep) >= 360:
                raise CadDesignError("Недопустимый угол дуги.")
            clean.update(startAngle=_number(entity.get("startAngle"), "начальный угол") % 360, sweep=sweep)
    else:
        raise CadDesignError("Неизвестный тип объекта чертежа.")
    return clean, count


def validate_design(entities):
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES:
        raise CadDesignError("Недопустимое число объектов чертежа.")
    clean = []
    by_id = {}
    point_count = 0
    for entity in entities:
        current, count = _entity(entity)
        if current["id"] in by_id:
            raise CadDesignError("В чертеже повторяются идентификаторы объектов.")
        point_count += count
        if point_count > MAX_POINTS:
            raise CadDesignError("В чертеже слишком много опорных точек.")
        clean.append(current)
        by_id[current["id"]] = current
    for entity in clean:
        if "parentId" in entity:
            parent = by_id.get(entity["parentId"])
            if parent is None or parent.get("notch"):
                raise CadDesignError("Не найден контур, к которому привязана насечка.")
    return clean


class _NonPlanar(ValueError):
    pass


def _planar(points):
    points = list(points)
    if any(not math.isfinite(value) or abs(value) > MAX_ABS_COORDINATE_MM for p in points for value in p):
        raise CadDesignError("Координаты чертежа выходят за допустимый диапазон.")
    if points and max(p.z for p in points) - min(p.z for p in points) > PLANAR_EPSILON_MM:
        raise _NonPlanar
    return [[p.x, p.y] for p in points]


def _similar_axes(u, v):
    scale = u.magnitude * v.magnitude
    return (scale > 0 and math.isclose(u.magnitude, v.magnitude, rel_tol=1e-9)
            and abs(u.dot(v)) <= 1e-9 * scale)


def _conic(center, u, v, start, sweep, *, circular):
    center, u, v = _planar((center, center + u, center + v))
    ux, uy = (u[axis] - center[axis] for axis in (0, 1))
    vx, vy = (v[axis] - center[axis] for axis in (0, 1))
    determinant = ux * vy - uy * vx
    if not determinant:
        raise CadDesignError("Вырожденная окружность или эллипс.")

    orientation = math.copysign(1, determinant)
    if circular and _similar_axes(Vec3(ux, uy), Vec3(vx, vy)):
        radius = math.hypot(ux, uy)
        if sweep == 360:
            return {"type": "circle", "center": center, "radius": radius}
        angle = math.degrees(math.atan2(uy, ux)) + orientation * start
        return {"type": "arc", "center": center, "radius": radius,
                "startAngle": angle, "sweep": orientation * sweep}

    # The principal axes of the transformed unit circle give an equivalent ellipse.
    xx, xy, yy = ux * ux + vx * vx, ux * uy + vx * vy, uy * uy + vy * vy
    angle = math.atan2(2 * xy, xx - yy) / 2
    major = math.sqrt((xx + yy + math.hypot(xx - yy, 2 * xy)) / 2)
    minor = abs(determinant) / major
    ax, ay = math.cos(angle), math.sin(angle)
    parameter = math.radians(start)
    dx = ux * math.cos(parameter) + vx * math.sin(parameter)
    dy = uy * math.cos(parameter) + vy * math.sin(parameter)
    phase = math.degrees(math.atan2((-ay * dx + ax * dy) / minor, (ax * dx + ay * dy) / major))
    return {"type": "ellipse", "center": center, "axis": [ax * major, ay * major],
            "ratio": min(1, minor / major), "startAngle": phase, "sweep": orientation * sweep}


def _curve(entity, transform):
    dxf = entity.dxf
    if entity.dxftype() == "ELLIPSE":
        ellipse = entity.construction_tool()
        u, v = transform.transform_directions((ellipse.major_axis, ellipse.minor_axis))
        start, end = map(math.degrees, (ellipse.start_param, ellipse.end_param))
        return _conic(transform.transform(ellipse.center), u, v, start, arc_angle_span_deg(start, end), circular=False)
    ocs = entity.ocs()
    radius = abs(dxf.radius)
    center = transform.transform(ocs.to_wcs(dxf.center))
    u, v = transform.transform_directions((ocs.ux * radius, ocs.uy * radius))
    if entity.dxftype() == "CIRCLE":
        return _conic(center, u, v, 0, 360, circular=True)
    start, end = dxf.start_angle, dxf.end_angle
    return _conic(center, u, v, start, arc_angle_span_deg(start, end), circular=True)


def _polyline(entity, transform, skipped):
    if entity.dxftype() == "LWPOLYLINE":
        vertices, closed = entity.vertices_in_wcs(), entity.closed
        bulges = [float(vertex[0]) for vertex in entity.get_points("b")]
    elif entity.is_2d_polyline or entity.is_3d_polyline:
        vertices, closed = entity.points_in_wcs(), entity.is_closed
        bulges = [vertex.dxf.bulge for vertex in entity.vertices] if entity.is_2d_polyline else []
    else:
        skipped["сетки POLYLINE"] += 1
        return

    vertices = list(vertices)
    transformed = list(transform.transform_vertices(vertices))
    usable = [all(math.isfinite(value) and abs(value) <= MAX_ABS_COORDINATE_MM for value in point)
              for point in transformed]
    heights = [point.z for point, valid in zip(transformed, usable) if valid]
    if heights and max(heights) - min(heights) > PLANAR_EPSILON_MM:
        raise _NonPlanar
    points = [[point.x, point.y] if valid else None for point, valid in zip(transformed, usable)]
    edge_count = len(points) if closed else len(points) - 1
    edges = []
    issues = set()
    first_gap = None
    for index in range(edge_count):
        following = (index + 1) % len(points)
        start, end = points[index], points[following]
        bulge = bulges[index] if bulges else 0.0
        if start is not None and end is not None and math.dist(start, end) <= POINT_EPSILON_MM:
            # Reuse the endpoint even when its zero-length source segment has an invalid bulge.
            points[following] = start
            issues.add(IMPORT_DUPLICATE_POINTS)
        elif start is None or end is None or not math.isfinite(bulge) or abs(bulge) > MAX_ABS_COORDINATE_MM:
            if first_gap is None:
                first_gap = len(edges)
            edges.append(None)
            issues.add(IMPORT_INVALID_VERTEX if start is None or end is None else IMPORT_INVALID_BULGE)
        else:
            edges.append((index, start, end, bulge))
    if not any(edge is not None for edge in edges):
        raise CadDesignError("У полилинии не осталось корректных сегментов.")

    is_closed = closed and first_gap is None
    if is_closed and len(edges) == 2 and not any(edge[3] for edge in edges):
        # A closed two-point line traverses the same edge in both directions.
        edges.pop()
        is_closed = False
        issues.add(IMPORT_DOUBLED_LINE)
    if first_gap is not None and closed:
        split = first_gap + 1
        edges = edges[split:] + edges[:split]

    nonuniform = False
    orientation = 1
    if any(edge is not None and edge[3] for edge in edges):
        ocs = entity.ocs()
        u, v = transform.transform_directions((ocs.ux, ocs.uy))
        nonuniform = not _similar_axes(u, v)
        orientation = -1 if u.cross(v).z < 0 else 1

    def recover_status(shape):
        if issues:
            shape.update(importDamaged=True, importIssues=sorted(issues))
        return shape

    if nonuniform:
        skipped["полилинии под неравномерным масштабом разложены на линии и эллипсы"] += 1
        for edge in edges:
            if edge is None:
                continue
            index, start, end, bulge = edge
            if not bulge:
                yield recover_status({"type": "line", "start": start, "end": end})
                continue
            try:
                a = ocs.from_wcs(vertices[index])
                b = ocs.from_wcs(vertices[(index + 1) % len(vertices)])
                center, first, last, radius = bulge_to_arc(a, b, bulge)
                curve = _conic(transform.transform(ocs.to_wcs(center.vec3.replace(z=a.z))), u * radius, v * radius,
                               math.degrees(first if bulge > 0 else last), math.degrees(4 * math.atan(bulge)),
                               circular=False)
            except (ezdxf.DXFError, ArithmeticError, ValueError, IndexError, TypeError):
                skipped["пропущены повреждённые дуги полилиний"] += 1
                continue
            yield recover_status(curve)
        return

    run = []
    for edge in [*edges, None]:
        if edge is not None:
            run.append(edge)
        elif run:
            path_points = [item[1] for item in run]
            bulges = [edge[3] * orientation for edge in run]
            if not is_closed:
                path_points.append(run[-1][2])
                bulges.append(0.0)
            shape = {"type": "polyline", "points": path_points, "closed": is_closed}
            if any(bulges):
                shape["bulges"] = bulges
            yield recover_status(shape)
            run = []


def _native_shapes(entity, transform, skipped):
    kind = entity.dxftype()
    if kind == "LINE":
        start, end = _planar(transform.transform_vertices((entity.dxf.start, entity.dxf.end)))
        if math.dist(start, end) > POINT_EPSILON_MM:
            yield {"type": "line", "start": start, "end": end}
        else:
            skipped["вырожденные объекты"] += 1
    elif kind in ("ARC", "CIRCLE", "ELLIPSE"):
        shape = _curve(entity, transform)
        if abs(shape.get("sweep", 360)) > 1e-9:
            yield shape
        else:
            skipped["вырожденные объекты"] += 1
    elif kind in ("LWPOLYLINE", "POLYLINE"):
        yield from _polyline(entity, transform, skipped)
    elif kind == "SPLINE":
        if entity.control_point_count():
            controls, degree = entity.control_points, entity.dxf.degree
            knots = list(entity.knots) or list(open_uniform_knot_vector(len(controls), degree + 1))
            weights = list(entity.weights)
        else:
            spline = entity.construction_tool()
            controls, degree = spline.control_points, spline.degree
            knots, weights = list(spline.knots()), list(spline.weights())
        yield {"type": "spline", "controlPoints": _planar(transform.transform_vertices(controls)),
               "degree": degree, "knots": list(map(float, knots)), "weights": list(map(float, weights)),
               "closed": entity.closed}


class _NativeDrawing:
    def __init__(self, document, audited):
        self.hidden_layers = {layer.dxf.name.casefold() for layer in document.layers
                              if layer.is_off() or layer.is_frozen()}
        self.skipped = Counter()
        self.recovered = Counter()
        self.audited = audited
        self.visited = 0
        self.points = 0
        self.exported = 0
        self.insert_instance = 0
        self.source_scopes = {}

    def _count(self, count=1):
        self.visited += count
        if self.visited > MAX_ENTITIES:
            raise CadDesignError("В чертеже более 50 000 объектов.")

    def shapes(self, layout, transform, inherited_layer="0", ancestry=(), inherited_issues=(), instance=0):
        for entity in layout:
            self._count()
            kind = entity.dxftype()
            if kind != "INSERT" and kind not in VECTOR_TYPES:
                self.skipped[kind] += 1
                continue
            layer = entity.dxf.get("layer", "0")
            issues = [*inherited_issues]
            if id(entity) in self.audited:
                issues.append(IMPORT_AUDIT)
            if any(0xD800 <= ord(character) <= 0xDFFF for character in layer):
                self.skipped["исправлены нечитаемые имена слоёв"] += 1
                layer = layer.encode("utf-8", errors="replace").decode("utf-8")
                issues.append(IMPORT_BAD_LAYER)
            if layer == "0":
                layer = inherited_layer
            if layer.casefold() in self.hidden_layers or entity.dxf.get("invisible", 0):
                self.skipped["скрытые объекты"] += 1
                continue
            if kind == "INSERT":
                block = entity.block()
                if block is None or block.block.is_xref or block.block.is_xref_overlay:
                    self.skipped["недоступные и внешние блоки"] += 1
                    continue
                name = block.name.casefold()
                if name in ancestry or len(ancestry) >= 32:
                    self.skipped["циклические или слишком глубоко вложенные блоки"] += 1
                    continue
                inserts = entity.multi_insert() if entity.mcount > 1 else (entity,)
                self._count(max(0, entity.mcount - 1))
                for insert in inserts:
                    try:
                        matrix = insert.matrix44() @ transform
                    except (ezdxf.DXFError, ArithmeticError, ValueError, IndexError, TypeError):
                        self.skipped["повреждённые вставки блоков"] += 1
                        continue
                    self.insert_instance += 1
                    yield from self.shapes(block, matrix, layer, (*ancestry, name), issues, instance=self.insert_instance)
                continue
            try:
                converted = []
                count = 0
                omitted_arcs = self.skipped["пропущены повреждённые дуги полилиний"]
                for index, shape in enumerate(_native_shapes(entity, transform, self.skipped), start=1):
                    if issues:
                        shape["importDamaged"] = True
                        shape["importIssues"] = list(dict.fromkeys([*shape.get("importIssues", []), *issues]))
                    canonical, size = _entity({**shape, "layer": layer, "id": f"dxf-{self.exported + index}"})
                    converted.append(canonical)
                    count += size
                    if self.exported + index > MAX_ENTITIES or self.points + count > MAX_POINTS:
                        break
            except _NonPlanar:
                self.skipped["объекты вне плоскости XY"] += 1
                continue
            except (ezdxf.DXFError, ArithmeticError, ValueError, IndexError, TypeError):
                self.skipped[f"повреждённые объекты {kind}"] += 1
                continue
            self.exported += len(converted)
            self.points += count
            if self.exported > MAX_ENTITIES or self.points > MAX_POINTS:
                raise CadDesignError("В чертеже слишком много объектов или опорных точек.")
            if self.skipped["пропущены повреждённые дуги полилиний"] != omitted_arcs:
                for shape in converted:
                    shape["importDamaged"] = True
                    shape.setdefault("importIssues", []).append(IMPORT_BAD_CURVE)
            if any(shape.get("importDamaged") for shape in converted):
                self.recovered[kind] += 1
            for shape in converted:
                self.source_scopes[shape["id"]] = instance, id(entity)
            yield from converted


def _dxf_units(data):
    # ezdxf synthesizes a metre-based R12 header for DXF streams without one.
    if data.startswith(BINARY_DXF_SIGNATURE):
        return _header_units(iter(binary_tags_loader(data)))
    with TextIOWrapper(BytesIO(data), encoding="latin-1") as stream:
        return _header_units(ascii_tags_loader(stream))


def _header_units(tags):
    drawing_units = None
    inside_header = False
    for tag in tags:
        if tag == (0, "SECTION"):
            inside_header = next(tags, None) == (2, "HEADER")
            if not inside_header:
                break
        elif tag == (0, "ENDSEC"):
            break
        elif tag == (9, "$INSUNITS") and inside_header:
            value = next(tags, None)
            if value is None or value.code != 70:
                raise ValueError("Invalid INSUNITS tag.")
            units_value = units.InsertUnits(int(value.value))
            if drawing_units is not None and drawing_units != units_value:
                raise ValueError("Conflicting INSUNITS tags.")
            drawing_units = units_value
        elif tag == (9, "$INSUNITS"):
            raise ValueError("INSUNITS outside HEADER.")
    return drawing_units or 0


def _read_editor_document(data, extension, warnings):
    if extension == ".dwg" and not data.startswith(b"AC10"):
        raise CadImportError("У файла отсутствует заголовок DWG.")
    audited = set()
    with TemporaryDirectory(prefix="plotter-cad-") as directory:
        source = Path(directory) / f"drawing{extension}"
        if extension == ".dwg":
            source.write_bytes(data)
            source = _convert_dwg(source)
            data = source.read_bytes()
        had_utf8_bom = data.startswith(BOM_UTF8)
        if had_utf8_bom:
            data = data.removeprefix(BOM_UTF8)
            source.write_bytes(data)
        elif extension == ".dxf":
            source.write_bytes(data)
        try:
            document = ezdxf.readfile(source, encoding="utf-8" if had_utf8_bom else None)
        except (ezdxf.DXFError, OSError, ValueError, IndexError, OverflowError, EOFError) as error:
            if data.startswith(BINARY_DXF_SIGNATURE):
                raise CadImportError("Не удалось прочитать двоичный DXF. Сохраните чертёж как текстовый DXF.") from error
            try:
                document, audit = recover.read(BytesIO(data))
            except (ezdxf.DXFError, OSError, ValueError, IndexError, OverflowError, EOFError) as recovery_error:
                raise CadImportError("Не удалось прочитать или восстановить структуру чертежа. "
                                     "Откройте его в исходном редакторе и сохраните снова как DXF.") from recovery_error
            warnings.append("Структура DXF восстановлена. Проверьте геометрию перед использованием "
                            f"(исправлений: {len(audit.fixes)}, оставшихся ошибок: {len(audit.errors)}).")
            audited = {id(entry.entity) for entry in (*audit.fixes, *audit.errors) if entry.entity is not None}
    return document, data, audited


def import_design(data, filename):
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(filename).suffix.lower()
    if extension not in {".dxf", ".dwg"}:
        raise CadImportError("Выберите файл DXF или DWG.")
    if not data:
        raise CadImportError("Выбранный файл пуст.")
    if len(data) > MAX_FILE_BYTES:
        raise CadImportError("Размер файла превышает 25 МБ.")
    warnings = []
    document, data, audited = _read_editor_document(data, extension, warnings)
    factor = 1
    try:
        drawing_units = _dxf_units(data)
        if drawing_units:
            factor = units.conversion_factor(drawing_units, units.MM)
    except (ezdxf.DXFError, TypeError, ValueError, IndexError, ZeroDivisionError, StopIteration) as error:
        raise CadImportError("Неизвестные единицы чертежа. Задайте единицы в CAD-редакторе.") from error
    if not drawing_units and warnings and document.units not in (0, units.M):
        raise CadImportError("После восстановления не удалось подтвердить единицы чертежа. "
                             "Задайте единицы в исходном CAD-редакторе.")
    drawing = _NativeDrawing(document, audited)
    entities = list(drawing.shapes(document.modelspace(), Matrix44.scale(factor)))
    if drawing.recovered:
        summary = ", ".join(f"{name} ({count})" for name, count in sorted(drawing.recovered.items()))
        warnings.append(f"Восстановлены повреждённые объекты: {summary}. Проверьте выделенные контуры.")
    if drawing.skipped:
        summary = ", ".join(f"{name} ({count})" for name, count in sorted(drawing.skipped.items()))
        warnings.append(f"Особенности импорта: {summary}.")
    if not entities:
        detail = f" {' '.join(warnings)}" if warnings else ""
        raise CadDesignError(f"В пространстве модели нет доступной двумерной геометрии.{detail}")
    result = {"filename": filename, "units": "mm", "entities": entities, "warnings": warnings}
    if extension == ".dxf":
        result.update(_import_conflict_cleanup(entities, document, drawing.source_scopes))
    return result


def _curve_points(center, u, v, start, sweep, tolerance=CURVE_TOLERANCE_MM):
    # The larger ellipse axis also bounds the second derivative in parameter space.
    radius = max(math.hypot(*u), math.hypot(*v))
    angle_step = min(math.pi / 4, math.sqrt(8 * tolerance / radius))
    count = math.ceil(abs(math.radians(sweep)) / angle_step)
    if count > MAX_POINTS:
        raise _CurveDetailLimit("Кривая требует слишком много точек для резки.")
    for index in range(count + 1):
        angle = math.radians(start + sweep * index / count)
        yield [value + first * math.cos(angle) + second * math.sin(angle)
               for value, first, second in zip(center, u, v)]


def _bulge_points(start, end, bulge, tolerance=CURVE_TOLERANCE_MM):
    if abs(bulge) < 1e-12:
        yield end
        return
    center, first_angle, last_angle, radius = bulge_to_arc(start, end, bulge)
    sweep = math.degrees(4 * math.atan(bulge))
    angle = math.degrees(first_angle if bulge > 0 else last_angle)
    points = iter(_curve_points(center, (radius, 0), (0, radius), angle, sweep, tolerance))
    next(points)
    previous = next(points)
    for point in points:
        yield previous
        previous = point
    yield end


def _span_points(evaluate, left, right, a, b, tolerance, depth=0):
    chord = Vec3(b) - Vec3(a)
    squared = chord.magnitude_square
    samples = [Vec3(evaluate(left + (right - left) * ratio)) for ratio in (0.25, 0.5, 0.75)]
    for point in samples:
        delta = point - Vec3(a)
        projection = max(0, min(1, delta.dot(chord) / squared)) if squared else 0
        if (delta - chord * projection).magnitude > tolerance:
            if depth == 24:
                raise _CurveDetailLimit("Не удалось построить сплайн с точностью для резки.")
            middle = (left + right) / 2
            yield from _span_points(evaluate, left, middle, a, samples[1], tolerance, depth + 1)
            yield from _span_points(evaluate, middle, right, samples[1], b, tolerance, depth + 1)
            return
    yield b


def _fit_spline_spans(entity):
    points, closed = entity["fitPoints"], entity["closed"]
    size = len(points)
    for index in range(size if closed else size - 1):
        neighbors = [point % size if closed else max(0, min(size - 1, point))
                     for point in range(index - 1, index + 3)]
        a, b, c, d = [Vec3(points[neighbor]) for neighbor in neighbors]
        if entity.get("fitMethod") == "centripetal":
            previous, middle, following = (math.sqrt((q - p).magnitude) for p, q in ((a, b), (b, c), (c, d)))
            start_tangent = (c - b if previous == 0 else
                             ((b - a) / previous - (c - a) / (previous + middle) + (c - b) / middle) * middle)
            end_tangent = (c - b if following == 0 else
                           ((c - b) / middle - (d - b) / (middle + following) + (d - c) / following) * middle)
        else:
            start_tangent, end_tangent = (c - a) / 2, (d - b) / 2
        yield b, start_tangent, end_tangent, c


def _native_spline(entity):
    control_points = entity["controlPoints"]
    old_knots = entity["knots"]
    # Normalize the whole vector consistently before passing it to the evaluator.
    span = old_knots[-1] - old_knots[0]
    knots = [(knot - old_knots[0]) / span for knot in old_knots]
    return BSpline(control_points, order=entity["degree"] + 1, knots=knots, weights=entity["weights"] or None)


def _spline_points(entity, tolerance=CURVE_TOLERANCE_MM):
    if "fitPoints" in entity:
        yield entity["fitPoints"][0]
        for b, start_tangent, end_tangent, c in _fit_spline_spans(entity):
            quadratic = (c - b) * 3 - start_tangent * 2 - end_tangent
            cubic = (b - c) * 2 + start_tangent + end_tangent

            def evaluate(t):
                return b + (start_tangent + (quadratic + cubic * t) * t) * t

            yield from _span_points(evaluate, 0, 1, b, c, tolerance)
        return

    spline = _native_spline(entity)
    evaluate = spline.evaluator.point
    bounds = sorted(set(spline.knots()[spline.degree:spline.count + 1]))
    start = evaluate(bounds[0])
    yield start
    for left, right in pairwise(bounds):
        end = evaluate(right)
        yield from _span_points(evaluate, left, right, start, end, tolerance)
        start = end


def _path_points(entity, tolerance):
    kind = entity["type"]
    if kind == "point":
        yield entity["point"]
    elif kind == "line":
        yield entity["start"]
        yield entity["end"]
    elif kind == "polyline":
        points = entity["points"]
        bulges = entity.get("bulges")
        yield points[0]
        for index in range(len(points) if entity["closed"] else len(points) - 1):
            start, end = points[index], points[(index + 1) % len(points)]
            if math.dist(start, end) > POINT_EPSILON_MM:
                yield from _bulge_points(start, end, bulges[index] if bulges else 0, tolerance)
    elif kind == "spline":
        yield from _spline_points(entity, tolerance)
    else:
        if kind == "ellipse":
            x, y = entity["axis"]
            u, v = (x, y), (-y * entity["ratio"], x * entity["ratio"])
        else:
            radius = entity["radius"]
            u, v = (radius, 0), (0, radius)
        yield from _curve_points(entity["center"], u, v, entity.get("startAngle", 0), entity.get("sweep", 360), tolerance)


def _closed(entity):
    return (entity["type"] == "circle" or entity.get("closed", False)
            or entity["type"] == "ellipse" and abs(entity["sweep"]) == 360)


def _path(entity, budget=MAX_POINTS, *, tolerance=CURVE_TOLERANCE_MM):
    points = []
    try:
        for index, raw in enumerate(_path_points(entity, tolerance), start=1):
            if index > budget:
                raise _CurveDetailLimit("После построения кривых в чертеже слишком много точек.")
            point = [round(float(raw[0]), 6), round(float(raw[1]), 6)]
            if not points or math.dist(points[-1], point) > POINT_EPSILON_MM:
                points.append(_point(point))
    except (ezdxf.DXFError, ArithmeticError, IndexError) as error:
        raise CadDesignError("Не удалось построить контур объекта.") from error
    closed = _closed(entity)
    if closed and len(points) > 1 and math.dist(points[0], points[-1]) <= POINT_EPSILON_MM:
        points.pop()
    if len(points) < (3 if closed else 2):
        raise CadDesignError("Объект вырождается при построении контура резки.")
    result = {"points": points, "layer": entity["layer"], "closed": closed}
    if entity.get("notch"):
        if closed:
            raise CadDesignError("Насечка должна быть открытой.")
        result["notch"] = True
    return result


def _smooth_path(entity, budget=MAX_POINTS):
    try:
        return _path(entity, budget, tolerance=DESIGN_CURVE_TOLERANCE_MM)
    except _CurveDetailLimit:
        return _path(entity, budget)


def _cut_paths(entities, tolerance):
    paths = []
    remaining = MAX_POINTS
    for entity in entities:
        path = _path(entity, remaining, tolerance=tolerance)
        remaining -= len(path["points"])
        paths.append(path)
    return paths


def prepare_design(entities):
    from nesting import annotate_notches, identify_parts

    entities = validate_design(entities)
    references = {entity["id"] for entity in entities if entity.get("reference")}
    cut_entities = [entity for entity in entities if entity["id"] not in references and entity.get("parentId") not in references]
    try:
        paths = _cut_paths(cut_entities, DESIGN_CURVE_TOLERANCE_MM)
    except _CurveDetailLimit:
        paths = _cut_paths(cut_entities, CURVE_TOLERANCE_MM)
    marked = annotate_notches(paths)
    notches = [index for index, path in enumerate(marked) if path["notch"]]
    for index in notches:
        paths[index] = marked[index]
    repaired = repair_closed_contours(paths, notches)
    source_ids = [[cut_entities[index]["id"] for index in indices] for indices in repaired.sources]
    parents = {entity["id"]: entity["parentId"] for entity in cut_entities if entity.get("parentId")}
    if parents:
        contours = defaultdict(list)
        bindings = {}
        for index, sources in enumerate(source_ids):
            if repaired.paths[index].get("notch") and sources[0] in parents:
                bindings[index] = parents[sources[0]]
            elif not repaired.paths[index].get("notch"):
                for source in sources:
                    contours[source].append(index)
        owners = {}
        for index, parent in bindings.items():
            if matches := contours[parent]:
                # Repairs can leave an open remainder beside a recovered perimeter.
                owners[index] = next((match for match in matches if repaired.paths[match]["closed"]), matches[0])
        if owners:
            for part in identify_parts(repaired.paths, notch_owners=owners):
                if any(index in owners for index in part["indices"]):
                    for index in part["indices"]:
                        repaired.paths[index] = {**repaired.paths[index], "partNumber": part["number"]}
    return {"paths": repaired.paths, "warnings": [], "closedCount": repaired.closed_count, "sourceIds": source_ids}


def _line_geometry(path):
    points = path["points"]
    return LineString([*points, points[0]] if path["closed"] else points)


def _new_shape(source, kind, **geometry):
    shape = {"id": uuid4().hex, "layer": source["layer"], "type": kind, **geometry}
    if source.get("groupPath") and not geometry.get("parentId"):
        shape["groupPath"] = source["groupPath"]
    if source.get("reference"):
        shape["reference"] = True
    if source.get("importDamaged") and not geometry.get("notch"):
        shape["importDamaged"] = True
        if source.get("importIssues"):
            shape["importIssues"] = source["importIssues"]
    return shape


def _shape_from_points(source, points, closed=False):
    points = [list(point) for point in points]
    if closed and points[0] == points[-1]:
        points.pop()
    if len(points) == 2 and not closed:
        return _new_shape(source, "line", start=points[0], end=points[1])
    return _new_shape(source, "polyline", points=points, closed=closed)


class _NotchContour:
    """Measure notch positions on the same contour used to generate the cut."""

    def __init__(self, parent, budget=MAX_POINTS, *, require_simple=True):
        self.parent = parent
        self.closed = _closed(parent)
        self.circle = parent["type"] == "circle"
        if self.circle:
            self.length = math.tau * parent["radius"]
        else:
            self.line = _line_geometry(_path(parent, budget))
            self.length = self.line.length
        if require_simple:
            self.require_simple()

    def require_simple(self):
        if not self.circle:
            self.polygon = Polygon(self.line) if self.closed else None
            if not self.line.is_simple or self.closed and not self.polygon.is_valid:
                raise CadDesignError("Для насечек нужен контур без самопересечений.")
            self.side = -1 if self.closed and not self.polygon.exterior.is_ccw else 1

    def project(self, point):
        if self.circle:
            x, y = self.parent["center"]
            angle = math.atan2(point[1] - y, point[0] - x) % math.tau
            return angle * self.parent["radius"]
        return self.line.project(Point(point))

    def distance(self, point):
        if self.circle:
            return abs(math.dist(point.coords[0], self.parent["center"]) - self.parent["radius"])
        return self.line.distance(point)

    def build(self, at, depth):
        if self.circle:
            x, y = self.parent["center"]
            radius = self.parent["radius"]
            if depth >= 2 * radius - POINT_EPSILON_MM:
                raise CadDesignError("Насечка пересекает противоположную сторону. Уменьшите глубину.")
            angle = at / radius
            dx, dy = math.cos(angle), math.sin(angle)
            start = [x + radius * dx, y + radius * dy]
            end = [x + (radius - depth) * dx, y + (radius - depth) * dy]
        else:
            attach = self.line.interpolate(at)
            width = min(self.length / 4, 0.01)
            if self.closed:
                before = self.line.interpolate((at - width) % self.length)
                after = self.line.interpolate((at + width) % self.length)
            else:
                before = self.line.interpolate(max(0, at - width))
                after = self.line.interpolate(min(self.length, at + width))
            dx, dy = after.x - before.x, after.y - before.y
            magnitude = math.hypot(dx, dy)
            if magnitude <= POINT_EPSILON_MM:
                raise CadDesignError("В этой точке не удалось определить направление насечки.")
            normal_x, normal_y = -self.side * dy / magnitude, self.side * dx / magnitude
            start = [attach.x, attach.y]
            end = [attach.x + depth * normal_x, attach.y + depth * normal_y]
            step = min(POINT_EPSILON_MM, depth / 4)
            inside = Point(attach.x + step * normal_x, attach.y + step * normal_y)
            cut = LineString([inside, end])
            if self.line.intersects(cut) or self.closed and not self.polygon.covers(cut):
                raise CadDesignError("Насечка пересекает границу детали. Выберите другую точку или уменьшите глубину.")
        return _new_shape(self.parent, "line", start=start, end=end, notch=True, parentId=self.parent["id"])

    def edge(self, point):
        if self.parent["type"] != "polyline":
            if self.closed:
                raise CadDesignError("Для этого замкнутого объекта выберите расстановку по всему контуру.")
            return self.line

        click = Point(point)
        best_distance = math.inf
        edge = None
        ambiguous = False
        for segment in self.polyline_edges():
            distance = segment.distance(click)
            if distance < best_distance - POINT_EPSILON_MM:
                best_distance, edge = distance, segment
                ambiguous = False
            elif abs(distance - best_distance) <= POINT_EPSILON_MM:
                ambiguous = True
        if ambiguous:
            raise CadDesignError("Укажите точку внутри одной стороны контура, вдали от угла.")
        return edge

    def polyline_edges(self):
        vertices = self.parent["points"]
        bulges = self.parent.get("bulges", [])
        for index in range(len(vertices) if self.closed else len(vertices) - 1):
            start, end = vertices[index], vertices[(index + 1) % len(vertices)]
            if math.dist(start, end) > POINT_EPSILON_MM:
                yield LineString([start, *_bulge_points(start, end, bulges[index] if bulges else 0)])


def _notch(parent, point, depth):
    contour = _NotchContour(parent)
    return contour.build(contour.project(point), depth)


def _notch_series(contour, options, depth):
    """Leave both endpoint intervals clear or divide a closed perimeter evenly."""

    count = options.get("count")
    if type(count) is not int or not 1 <= count <= MAX_NOTCH_SERIES:
        raise CadDesignError(f"Укажите от 1 до {MAX_NOTCH_SERIES} насечек.")
    scope = options.get("scope")
    if scope == "edge":
        edge = contour.edge(_point(options.get("point")))
        if edge.length / (count + 1) < MIN_NOTCH_SPACING_MM:
            raise CadDesignError("Сторона слишком короткая для указанного числа насечек.")
        positions = [contour.project(edge.interpolate(index / (count + 1), normalized=True).coords[0])
                     for index in range(1, count + 1)]
    elif scope == "contour":
        anchor = contour.project(_point(options["point"])) if "point" in options else 0
        interval = contour.length / (count if contour.closed else count + 1)
        if interval < MIN_NOTCH_SPACING_MM:
            raise CadDesignError("Контур слишком короткий для указанного числа насечек.")
        positions = ([(anchor + (index + 0.5) * interval) % contour.length for index in range(count)]
                     if contour.closed else [index * interval for index in range(1, count + 1)])
    else:
        raise CadDesignError("Укажите расстановку насечек по стороне или по всему контуру.")
    return [contour.build(position, depth) for position in positions]


def _check_notch_spacing(shapes, siblings):
    anchors = []
    lines = []
    budget = MAX_POINTS
    for entity in siblings:
        path = _path(entity, budget)
        budget -= len(path["points"])
        anchors.append(path["points"][0])
        lines.append(_line_geometry(path))
    tree = STRtree(lines)
    added = []
    for shape in shapes:
        start = shape["start"]
        line = LineString([start, shape["end"]])
        x1, y1, x2, y2 = line.bounds
        gap = MIN_NOTCH_SPACING_MM
        nearby = ((anchors[index], lines[index]) for index in tree.query(box(x1 - gap, y1 - gap, x2 + gap, y2 + gap)))
        for anchor, existing in (*nearby, *added):
            if math.dist(start, anchor) < gap or line.intersects(existing):
                raise CadDesignError("Насечки пересекаются или расположены ближе 0,5 мм друг к другу.")
        added.append((start, line))


def _find_notch_attachment(existing, by_id, options):
    requested = options.get("parentId")
    path = _path(existing)
    if "parentId" in existing:
        if requested is not None and requested != existing["parentId"]:
            raise CadDesignError("Насечку можно переместить только по её исходному контуру.")
        parent = _selected(by_id, existing["parentId"])
        return _NotchContour(parent), _point(options.get("point", path["points"][0]))

    points = [_point(options["point"])] if "point" in options else [path["points"][0], path["points"][-1]]
    queries = [Point(point) for point in points]
    if requested is not None:
        candidates = [_selected(by_id, requested)]
    else:
        candidates = (entity for entity in by_id.values() if not entity.get("notch") and entity["type"] != "point")

    match = None
    error = "Насечка примыкает к нескольким сторонам. Укажите контур и точку явно."
    tolerance = NOTCH_BIND_TOLERANCE_MM + POINT_EPSILON_MM
    budget = MAX_POINTS
    for entity in candidates:
        contour = _NotchContour(entity, budget, require_simple=False)
        if not contour.circle:
            budget -= len(contour.line.coords)
        for point, query in zip(points, queries):
            if contour.distance(query) <= tolerance:
                if match is not None:
                    raise CadDesignError(error)
                if "point" not in options and entity["type"] == "polyline":
                    nearby = (edge for edge in contour.polyline_edges() if edge.distance(query) <= tolerance)
                    next(nearby, None)
                    if next(nearby, None) is not None:
                        raise CadDesignError(error)
                match = contour, point
    if match is None:
        raise CadDesignError("Не найден соседний край насечки. Укажите контур и точку на нём.")
    contour, point = match
    contour.require_simple()
    return contour, point


def _edit_notches(entities, by_id, operation, options):
    depth = _positive(options.get("depth"), "Глубина")
    if operation == "notch-edit":
        existing = by_id.get(_text(options.get("id"), "идентификатор насечки", 128))
        if (not existing or not existing.get("notch") or existing["type"] not in ("line", "polyline")
                or _closed(existing)):
            raise CadDesignError("Выберите открытую насечку.")
        contour, point = _find_notch_attachment(existing, by_id, options)
        parent = contour.parent
        shape = contour.build(contour.project(point), depth)
        shape.update(id=existing["id"], layer=existing["layer"])
        if "reference" in existing:
            shape["reference"] = existing["reference"]
        shapes = [shape]
        result = [shapes[0] if entity["id"] == existing["id"] else entity for entity in entities]
        ignored = existing["id"]
    else:
        parent = _selected(by_id, options.get("id"))
        if operation == "notch-series":
            shapes = _notch_series(_NotchContour(parent), options, depth)
        else:
            shapes = [_notch(parent, _point(options.get("point")), depth)]
        result = [*entities, *shapes]
        ignored = None
    _check_notch_spacing(shapes, [entity for entity in entities
                                 if entity.get("parentId") == parent["id"] and entity["id"] != ignored])
    return result, shapes


def _move_notches(entities, targets):
    if not targets or not entities:
        return []
    lines = [(target, _line_geometry(_path(target))) for target in targets]
    result = []
    for entity in entities:
        points = _path(entity)["points"]
        point = Point(points[0])
        target, _ = min(lines, key=lambda record: record[1].distance(point))
        result.append(_notch(target, points[0], math.dist(points[0], points[-1])))
    return result


def _offset_contour(source, curve):
    if source["type"] == "line":
        return _shape_from_points(source, curve.coords)
    closed = curve.is_closed
    points = [list(point) for point in curve.coords]
    if closed:
        points.pop()
    return _new_shape(source, "polyline", points=points, closed=closed, joined=True)


def _offset_shapes(entity, distance):
    kind = entity["type"]
    if kind in ("circle", "arc"):
        direction = 1 if kind == "circle" else -math.copysign(1, entity["sweep"])
        radius = entity["radius"] + direction * distance
        if radius <= POINT_EPSILON_MM:
            raise CadDesignError("Смещение полностью убирает контур.")
        return [{**entity, "radius": radius, "id": uuid4().hex}]

    path = _path(entity) if kind == "line" else _smooth_path(entity)
    line = _line_geometry(path)
    result = []
    if path["closed"]:
        polygon = Polygon(line)
        if not polygon.is_valid:
            raise CadDesignError("Смещение требует замкнутого контура без самопересечений.")
        winding = 1 if polygon.exterior.is_ccw else -1
        offset = polygon.buffer(distance, join_style="mitre")
        polygons = offset.geoms if offset.geom_type == "MultiPolygon" else (offset,)
        for polygon in polygons:
            if not polygon.is_empty:
                boundary = orient(polygon, sign=winding)
                result.append(_offset_contour(entity, boundary.exterior))
                result.extend(_offset_contour(entity, ring) for ring in boundary.interiors)
    else:
        offset = line.offset_curve(distance, join_style="mitre")
        if offset.geom_type == "MultiLineString":
            offset = line_merge(offset)
        lines = offset.geoms if offset.geom_type == "MultiLineString" else (offset,)
        result.extend(_offset_contour(entity, curve) for curve in lines if not curve.is_empty)
    if not result:
        raise CadDesignError("Смещение полностью убирает контур.")
    return result


def _intersections(target, cutter):
    intersection = target.intersection(cutter)
    pending = [intersection]
    while pending:
        geometry = pending.pop()
        if geometry.is_empty:
            continue
        if geometry.geom_type == "Point":
            yield target.project(geometry)
        elif hasattr(geometry, "geoms"):
            pending.extend(geometry.geoms)
        else:
            raise CadDesignError("Контур и граница обрезки совпадают на участке.")


def _trim_shapes(entity, cutters, click):
    path = _path(entity)
    target = _line_geometry(path)
    if not target.is_simple:
        raise CadDesignError("Для обрезки нужен контур без самопересечений.")
    length = target.length
    at = target.project(Point(click))
    budget = MAX_POINTS - len(path["points"])
    positions = set()
    for cutter in cutters:
        cutter_path = _path(cutter, budget)
        budget -= len(cutter_path["points"])
        positions.update(round(position, 8) for position in _intersections(target, _line_geometry(cutter_path)))
        if len(positions) > MAX_POINTS:
            raise CadDesignError("Слишком много пересечений для обрезки.")
    intersections = sorted(positions)
    cuts = [position for position in intersections if POINT_EPSILON_MM < position < length - POINT_EPSILON_MM]
    if path["closed"]:
        if len(cuts) < len(intersections):
            cuts = [0.0, *cuts]
        if len(cuts) < 2:
            raise CadDesignError("Для обрезки замкнутого контура нужны два пересечения.")
        if at < cuts[0] or at > cuts[-1]:
            ranges = [(cuts[0], cuts[-1])]
        else:
            left, right = next((left, right) for left, right in pairwise(cuts) if left <= at <= right)
            ranges = [(right, length), (0, left)]
    else:
        if not cuts:
            raise CadDesignError("Границы обрезки не пересекают внутреннюю часть контура.")
        left, right = next((left, right) for left, right in pairwise([0, *cuts, length]) if left <= at <= right)
        ranges = [(0, left), (right, length)]

    pieces = [list(substring(target, first, last).coords)
              for first, last in ranges if last - first > POINT_EPSILON_MM]
    if path["closed"] and len(pieces) == 2:
        pieces = [pieces[0] + pieces[1][1:]]
    return [_shape_from_points(entity, points) for points in pieces]


class _QuickTrimEdge:
    def __init__(self, start, end, center=None, radius=None, angle=None, sweep=None):
        self.start = start
        self.end = end
        self.center = center
        self.radius = radius
        self.angle = angle
        self.sweep = sweep
        self.length = abs(radius * sweep) if center is not None else math.dist(start, end)

    @classmethod
    def circular(cls, center, radius, angle, sweep):
        def point(at):
            return [center[0] + radius * math.cos(at), center[1] + radius * math.sin(at)]

        return cls(point(angle), point(angle + sweep), center, radius, angle, sweep)

    @property
    def geometry(self):
        if self.center is None:
            return LineString([self.start, self.end]) if self.length else Point(self.start)
        points = [self.start, self.end]
        for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
            point = [self.center[0] + self.radius * math.cos(angle), self.center[1] + self.radius * math.sin(angle)]
            if self.position(point) is not None:
                points.append(point)
        xmin, ymin = map(min, zip(*points))
        xmax, ymax = map(max, zip(*points))
        slack = POINT_EPSILON_MM
        return box(xmin - slack, ymin - slack, xmax + slack, ymax + slack)

    def angle_position(self, point):
        dx, dy = point[0] - self.center[0], point[1] - self.center[1]
        delta = math.atan2(dy, dx) - self.angle
        return (math.copysign(1, self.sweep) * delta % math.tau) * self.radius

    def position(self, point):
        if self.center is None:
            along = sum((value - start) * (end - start) / self.length
                        for value, start, end in zip(point, self.start, self.end))
            return max(0.0, min(along, self.length))
        position = self.angle_position(point)
        if math.tau * self.radius - position <= POINT_EPSILON_MM:
            return 0.0
        return min(position, self.length) if position <= self.length + POINT_EPSILON_MM else None

    def project(self, point):
        if self.length == 0:
            return 0.0, math.dist(point, self.start)
        position = self.position(point)
        if position is None:
            position = 0.0 if math.dist(point, self.start) <= math.dist(point, self.end) else self.length
        return position, math.dist(point, self.at(position))

    def at(self, position):
        ratio = position / self.length
        if self.center is None:
            return [first + (last - first) * ratio for first, last in zip(self.start, self.end)]
        angle = self.angle + ratio * self.sweep
        return [self.center[0] + self.radius * math.cos(angle), self.center[1] + self.radius * math.sin(angle)]

    def nearest_to(self, stroke):
        if self.length == 0:
            return 0.0, stroke.project(self.start)[1]
        nearest = min((self.project(stroke.start), self.project(stroke.end)), key=lambda item: item[1])
        for position, point in ((0, self.start), (self.length, self.end)):
            _, gap = stroke.project(point)
            if gap < nearest[1]:
                nearest = position, gap
        if self.center is not None:
            position, distance = stroke.project(self.center)
            if distance > POINT_EPSILON_MM and 0 < position < stroke.length:
                foot = stroke.at(position)
                opposite = [2 * center - value for center, value in zip(self.center, foot)]
                for point in (foot, opposite):
                    at = self.position(point)
                    if at is not None:
                        _, gap = stroke.project(self.at(at))
                        if gap < nearest[1]:
                            nearest = at, gap
        return nearest


def _quick_trim_edges(entity, budget):
    kind = entity["type"]
    if kind == "point":
        yield _QuickTrimEdge(entity["point"], entity["point"])
        return
    if kind in ("circle", "arc"):
        yield _QuickTrimEdge.circular(entity["center"], entity["radius"], math.radians(entity.get("startAngle", 0)),
                                      math.radians(entity.get("sweep", 360)))
        return
    path = entity["points"] if kind == "polyline" else _path(entity, budget)["points"]
    bulges = entity.get("bulges", [])
    for index, (start, end) in enumerate(pairwise([*path, path[0]] if _closed(entity) else path)):
        if math.dist(start, end) <= POINT_EPSILON_MM:
            continue
        bulge = bulges[index] if bulges else 0
        yield _curve_edge(start, end, bulge)


def _curve_edge(start, end, bulge):
    if abs(bulge) < 1e-12:
        return _QuickTrimEdge(start, end)
    center, first, last, radius = bulge_to_arc(start, end, bulge)
    return _QuickTrimEdge(start, end, center, radius, first if bulge > 0 else last, 4 * math.atan(bulge))


def _circle_line_crossings(circle, line):
    ax, ay = line.start
    dx, dy = ((b - a) / line.length for a, b in zip(line.start, line.end))
    px, py = circle.center[0] - ax, circle.center[1] - ay
    nearest = px * dx + py * dy
    height = abs(px * dy - py * dx)
    if height > circle.radius + POINT_EPSILON_MM:
        return
    extent = math.sqrt(max(0, (circle.radius + height) * (circle.radius - height)))
    for longitudinal in (nearest - extent, nearest + extent):
        if -POINT_EPSILON_MM <= longitudinal <= line.length + POINT_EPSILON_MM:
            yield [ax + longitudinal * dx, ay + longitudinal * dy]


def _straight_crossings(first, second):
    ax, ay = first.start
    bx, by = second.start
    ux, uy = ((b - a) / first.length for a, b in zip(first.start, first.end))
    vx, vy = ((b - a) / second.length for a, b in zip(second.start, second.end))
    determinant = ux * vy - uy * vx
    if not determinant:
        return
    dx, dy = bx - ax, by - ay
    t = (dx * vy - dy * vx) / determinant
    s = (dx * uy - dy * ux) / determinant
    if -POINT_EPSILON_MM <= t <= first.length + POINT_EPSILON_MM and (
            -POINT_EPSILON_MM <= s <= second.length + POINT_EPSILON_MM):
        yield [ax + t * ux, ay + t * uy]


def _circular_crossings(first, second):
    x, y = first.center
    dx, dy = second.center[0] - x, second.center[1] - y
    gap = math.hypot(dx, dy)
    larger, smaller = max(first.radius, second.radius), min(first.radius, second.radius)
    if not gap or gap > larger + smaller + POINT_EPSILON_MM or gap < larger - smaller - POINT_EPSILON_MM:
        return
    distance = (gap + (first.radius - second.radius) * (first.radius + second.radius) / gap) / 2
    height = math.sqrt(max(0, (first.radius + distance) * (first.radius - distance)))
    u, v = dx / gap, dy / gap
    for offset in (-height, height):
        yield [x + distance * u - offset * v, y + distance * v + offset * u]


def _quick_trim_crossings(first, second):
    if not first.length or not second.length:
        return
    if first.center is not None and second.center is not None:
        candidates = _circular_crossings(first, second)
    elif first.center is not None:
        candidates = _circle_line_crossings(first, second)
    elif second.center is not None:
        candidates = _circle_line_crossings(second, first)
    else:
        candidates = _straight_crossings(first, second)
    for point in candidates:
        at = first.position(point)
        if at is not None and second.position(point) is not None:
            yield at


class _QuickTrimContour:
    def __init__(self, entity, edges):
        self.entity = entity
        self.edges = edges
        self.offsets = []
        self.length = 0.0
        self.closed = _closed(entity)
        self.can_partition = entity["type"] != "point" and not entity.get("notch")
        self.native_edges = self.can_partition and entity["type"] == "polyline" and not entity.get("joined", False)
        for edge in edges:
            self.offsets.append(self.length)
            self.length += edge.length

    def partitions(self, positions):
        if self.native_edges:
            positions = [*positions, *self.offsets]
        stops = []
        for cut in sorted(positions):
            if min(cut, self.length - cut) <= POINT_EPSILON_MM:
                if not self.closed:
                    continue
                cut = 0.0 if cut < self.length / 2 else self.length
            if not stops or cut - stops[-1] > POINT_EPSILON_MM:
                stops.append(cut)
        if self.closed:
            if len(stops) > 1 and self.length - stops[-1] + stops[0] <= POINT_EPSILON_MM:
                stops.pop()
            if len(stops) < 2:
                return []
            return [*pairwise(stops), (stops[-1], self.length + stops[0])]
        return list(pairwise([0.0, *stops, self.length])) if stops else []

    def authored_position(self, edge, position):
        if not self.native_edges:
            return position
        inset = min(2 * POINT_EPSILON_MM, edge.length / 2)
        return max(inset, min(position, edge.length - inset))

    def authored_overlap(self, edge, stroke):
        if not self.native_edges or edge.center is not None:
            return None
        dx, dy = ((end - start) / edge.length for start, end in zip(edge.start, edge.end))
        if any(abs((point[0] - edge.start[0]) * dy - (point[1] - edge.start[1]) * dx) > POINT_EPSILON_MM
               for point in (stroke.start, stroke.end)):
            return None
        start, end = sorted((edge.position(stroke.start), edge.position(stroke.end)))
        return (start, end) if end - start > POINT_EPSILON_MM else None

    def kept_ranges(self, parts, removed):
        kept = []
        for index, (start, end) in enumerate(parts):
            if index in removed:
                continue
            if kept and kept[-1][1] == start:
                kept[-1] = (kept[-1][0], end)
            else:
                kept.append((start, end))
        if self.closed and len(kept) > 1 and kept[-1][1] == kept[0][0] + self.length:
            first, last = kept.pop(0), kept.pop()
            kept.append((last[0], first[1] + self.length))
        return kept

    def position(self, point):
        _, position = min((gap, offset + at)
                          for edge, offset in zip(self.edges, self.offsets)
                          for at, gap in (edge.project(point),))
        return position

    def fragment(self, start, end):
        kind = self.entity["type"]
        if kind in ("circle", "arc"):
            edge, = self.edges
            return _new_shape(self.entity, "arc", center=edge.center, radius=edge.radius,
                              startAngle=math.degrees(edge.angle + start / edge.length * edge.sweep),
                              sweep=math.degrees((end - start) / edge.length * edge.sweep))
        points, bulges = [], []
        spans = [(start, min(end, self.length))]
        if end > self.length:
            spans.append((0, end - self.length))
        for left, right in spans:
            first = max(0, bisect_right(self.offsets, left) - 1)
            last = bisect_left(self.offsets, right)
            for index in range(first, last):
                edge, offset = self.edges[index], self.offsets[index]
                a = max(0, left - offset)
                b = min(edge.length, right - offset)
                if b - a <= POINT_EPSILON_MM:
                    continue
                if not points:
                    points.append(edge.at(a))
                points.append(edge.at(b))
                bulges.append(math.tan(edge.sweep * (b - a) / edge.length / 4) if edge.center is not None else 0)
        if any(bulges):
            shape = _new_shape(self.entity, "polyline", points=points, bulges=[*bulges, 0], closed=False)
        else:
            shape = _shape_from_points(self.entity, points)
        if shape["type"] == "polyline":
            if kind in ("ellipse", "spline"):
                shape["joined"] = True
            elif "joined" in self.entity:
                shape["joined"] = self.entity["joined"]
        return shape


MAX_TRIM_STROKE = 256
MAX_TRIM_EDGES = 50_000
MAX_TRIM_CHECKS = 250_000


def _quick_trim(entities, options):
    stroke = options.get("stroke")
    if not isinstance(stroke, list) or not 1 <= len(stroke) <= MAX_TRIM_STROKE:
        raise CadDesignError(f"Укажите до {MAX_TRIM_STROKE} точек для обрезки.")
    stroke = [_point(point) for point in stroke]
    tolerance = _positive(options.get("tolerance"), "Допуск выбора")
    if tolerance > 800_001:
        raise CadDesignError("Недопустимый допуск выбора для обрезки.")

    scope = _group_path(options["groupPath"]) if "groupPath" in options else None
    by_id = {entity["id"]: entity for entity in entities} if scope is not None else {}
    edges, owners, bounds = [], [], []
    for entity in entities:
        owner = by_id.get(entity.get("parentId"), entity)
        if scope is not None and owner.get("groupPath", []) != scope:
            continue
        available = MAX_TRIM_EDGES - len(edges)
        contour_edges = list(islice(_quick_trim_edges(entity, available + 1), available + 1))
        if len(contour_edges) > available:
            raise CadDesignError("Слишком много сегментов для быстрой обрезки.")
        if not contour_edges:
            continue
        contour = _QuickTrimContour(entity, contour_edges)
        edges.extend(contour_edges)
        owners.extend((contour, offset) for offset in contour.offsets)
        bounds.extend(edge.geometry for edge in contour_edges)
    tree = STRtree(bounds)
    checks = 0

    def track_checks(count):
        nonlocal checks
        checks += count
        if checks > MAX_TRIM_CHECKS:
            raise CadDesignError("Слишком много пересечений для быстрой обрезки. Уменьшите область выбора.")

    def nearby(geometry):
        candidates = tree.query(geometry)
        track_checks(len(candidates))
        return candidates

    targets = defaultdict(list)
    swept = defaultdict(list)
    corners = defaultdict(list)
    strokes = [_QuickTrimEdge(start, end) for start, end in pairwise(stroke)
               if math.dist(start, end) > POINT_EPSILON_MM]
    if strokes:
        for segment in strokes:
            hits = defaultdict(list)
            interior = defaultdict(list)
            xmin, ymin, xmax, ymax = segment.geometry.bounds
            area = box(xmin - tolerance, ymin - tolerance, xmax + tolerance, ymax + tolerance)
            for index in nearby(area):
                contour, offset = owners[index]
                edge = edges[index]
                overlap = contour.authored_overlap(edge, segment)
                if overlap:
                    start, end = overlap
                    swept[contour].append((offset + start, offset + end))
                    interior[contour].append((offset + (start + end) / 2, 0))
                    continue
                crossings = list(_quick_trim_crossings(edge, segment))
                if crossings:
                    for position in crossings:
                        contact = (offset + contour.authored_position(edge, position), 0)
                        if contour.native_edges:
                            candidates = interior if min(position, edge.length - position) > POINT_EPSILON_MM else corners
                            candidates[contour].append(contact)
                        else:
                            hits[contour].append(contact)
                    continue
                position, distance = edge.nearest_to(segment)
                if distance <= tolerance:
                    candidates = corners if contour.native_edges and min(position, edge.length - position) <= POINT_EPSILON_MM else hits
                    candidates[contour].append((offset + contour.authored_position(edge, position), distance))
            for contour, contacts in interior.items():
                targets[contour].extend(contacts)
            for contour, contacts in hits.items():
                # Keep distinct crossings, but a wide eraser may graze several
                # adjacent intervals while following one edge of a contour.
                if contour in interior:
                    continue
                exact = [] if contour.native_edges else [contact for contact in contacts if contact[1] <= POINT_EPSILON_MM]
                targets[contour].extend(exact or [min(contacts, key=lambda contact: contact[1])])
        for contour, contacts in corners.items():
            # A stroke leaving an erased side at its vertex does not also erase the adjacent side.
            if not targets[contour]:
                targets[contour].append(min(contacts, key=lambda contact: contact[1]))
    else:
        x, y = stroke[0]
        click_hit = None
        for index in nearby(box(x - tolerance, y - tolerance, x + tolerance, y + tolerance)):
            contour, offset = owners[index]
            edge = edges[index]
            position, distance = edge.project(stroke[0])
            if distance <= tolerance:
                targets[contour].append((offset + contour.authored_position(edge, position), distance))
                priority = distance, contour.can_partition
                if click_hit is None or priority < click_hit[0]:
                    click_hit = priority, contour, edge, position
        if click_hit is not None:
            _, contour, edge, position = click_hit
            if contour.can_partition and min(position, edge.length - position) <= MAX_CORNER_OVERRUN_MM:
                correction = _erase_corner_overlap(entities, contour.entity, stroke[0], tolerance)
                if correction is not None:
                    return correction

    partitions = {}
    edge_indexes = defaultdict(list)
    for index, (contour, _) in enumerate(owners):
        if targets.get(contour):
            edge_indexes[contour].append(index)
    for contour in edge_indexes:
        cuts = []
        if contour.can_partition:
            for index in edge_indexes[contour]:
                _, offset = owners[index]
                for cutter in nearby(bounds[index]):
                    other = owners[cutter][0]
                    if other is contour or not other.can_partition:
                        continue
                    cuts.extend(offset + position for position in _quick_trim_crossings(edges[index], edges[cutter]))
        partitions[contour] = contour.partitions(cuts)

    selected = []
    for contour, parts in partitions.items():
        if not parts:
            selected.append((min(distance for _, distance in targets[contour]), None, contour))
            continue
        ends = [end for _, end in parts]
        contacted = []
        exact_partitions = set()
        for position, distance in targets[contour]:
            at = position + contour.length if contour.closed and position < parts[0][0] else position
            index = bisect_left(ends, at)
            if index < len(parts):
                start, end = parts[index]
                contacted.append((distance, index, min(at - start, end - at)))
                if distance <= POINT_EPSILON_MM:
                    exact_partitions.add(index)
        for start, end in swept.get(contour, ()):
            first = bisect_right(ends, start + POINT_EPSILON_MM)
            for index in range(first, len(parts)):
                left, right = parts[index]
                if left >= end:
                    break
                if index not in exact_partitions and min(right, end) - max(left, start) > POINT_EPSILON_MM:
                    exact_partitions.add(index)
                    selected.append((0, index, contour))
        for distance, index, boundary_gap in contacted:
            adjacent = {index - 1, index + 1}
            if contour.closed:
                adjacent = {neighbor % len(parts) for neighbor in adjacent}
            if strokes and distance > POINT_EPSILON_MM and boundary_gap < tolerance and adjacent & exact_partitions:
                continue
            selected.append((distance, index, contour))
    if selected and not strokes:
        # At a shared anchor prefer the explicit point or notch over its contour.
        selected = [min(selected, key=lambda match: (match[0], match[2].can_partition))]
    removed = defaultdict(set)
    for _, index, contour in selected:
        removed[contour].add(index)

    replacements = {}
    shapes = []
    warnings = []
    contours_by_id = {}
    for contour, discarded in removed.items():
        ranges = contour.kept_ranges(partitions[contour], discarded)
        pieces = [(start, end, contour.fragment(start, end)) for start, end in ranges]
        entity = contour.entity
        contours_by_id[entity["id"]] = contour
        replacements[entity["id"]] = pieces
        shapes.extend(shape for _, _, shape in pieces)
        if entity["type"] in ("ellipse", "spline") and pieces:
            warnings = ["Обрезанная кривая представлена полилинией."]
    result = []
    remaining_notch_points = MAX_POINTS
    for entity in entities:
        if entity["id"] in replacements:
            result.extend(shape for _, _, shape in replacements[entity["id"]])
            continue
        parent = entity.get("parentId")
        if parent not in replacements:
            result.append(entity)
            continue
        if not replacements[parent]:
            continue
        contour = contours_by_id[parent]
        path = _path(entity, remaining_notch_points)
        remaining_notch_points -= len(path["points"])
        track_checks(len(contour.edges) + len(replacements[parent]))
        at = contour.position(path["points"][0])
        for start, end, shape in replacements[parent]:
            if start - POINT_EPSILON_MM <= at <= end + POINT_EPSILON_MM or (
                    contour.closed and start - POINT_EPSILON_MM <= at + contour.length <= end + POINT_EPSILON_MM):
                result.append({**entity, "parentId": shape["id"]})
                break
    return result, shapes, warnings, bool(removed)


def _join_shapes(entities, tolerance):
    if len(entities) < 2:
        raise CadDesignError("Выберите не менее двух открытых объектов.")
    if len({entity.get("reference", False) for entity in entities}) > 1:
        raise CadDesignError("Для объединения выберите только контуры резки или только вспомогательные.")

    runs = []
    budget = MAX_POINTS
    for entity in entities:
        path = _path(entity, budget)
        budget -= len(path["points"])
        if path["closed"]:
            raise CadDesignError("Объединяются только открытые объекты.")
        if entity["type"] == "arc":
            bulges = [math.tan(math.radians(entity["sweep"]) / 4), 0]
            points = [path["points"][0], path["points"][-1]]
        elif entity["type"] == "polyline" and "bulges" in entity:
            points = entity["points"]
            bulges = [*entity["bulges"][:-1], 0]
        else:
            points = path["points"]
            bulges = [0] * len(points)
        runs.append((points, bulges))

    buckets = defaultdict(list)
    joined = {}
    for index, (points, _) in enumerate(runs):
        for side, point in enumerate((points[0], points[-1])):
            end = (index, side)
            x, y = (math.floor(value / tolerance) for value in point)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other, position in buckets[x + dx, y + dy]:
                        if index == other[0] or math.dist(point, position) > tolerance:
                            continue
                        if end in joined or other in joined:
                            junction = point if end in joined else position
                            raise _RepairConflict("В выбранных линиях есть развилка. Выберите ветви одного контура.",
                                                  [{"point": junction, "kind": "branch"}])
                        joined[end] = other
                        joined[other] = end
            buckets[x, y].append((end, point))

    ends = [(index, side) for index in range(len(runs)) for side in (0, 1) if (index, side) not in joined]
    first = ends[0] if ends else (0, 0)
    current = first
    seen = set()
    points, bulges = [], []
    while current is not None and current[0] not in seen:
        index, side = current
        source_points, source_bulges = runs[index]
        if side == 1:
            source_points = source_points[::-1]
            source_bulges = [-value for value in reversed(source_bulges[:-1])] + [0]
        if points:
            points.pop()
            bulges.pop()
        points.extend(source_points)
        bulges.extend(source_bulges)
        seen.add(index)
        current = joined.get((index, 1 - side))
    if len(seen) != len(runs):
        raise CadDesignError("Между выбранными объектами есть разрыв. Сведите их или выберите соседние фрагменты.")
    closed = current == first
    if closed:
        points.pop()
        bulges.pop()
    result = _new_shape(entities[0], "polyline", points=points, closed=closed, joined=True)
    if any(entity.get("importDamaged") for entity in entities):
        result["importDamaged"] = True
        issues = list(dict.fromkeys(issue for entity in entities for issue in entity.get("importIssues", [])))
        if issues:
            result["importIssues"] = issues[:12]
    if any(bulges):
        result["bulges"] = bulges
    return result


def _selected(by_id, value):
    entity = by_id.get(_text(value, "идентификатор объекта", 128))
    if entity is None or entity.get("notch") or entity["type"] == "point":
        raise CadDesignError("Выберите существующий контур.")
    return entity


def _insert_polyline_points(entity, click, edge_index, count):
    line = entity["type"] == "line"
    points = [entity["start"], entity["end"]] if line else entity["points"]
    closed = entity.get("closed", False)
    bulges = entity.get("bulges")
    edge_count = len(points) - int(not closed)
    if edge_index is not None and not 0 <= edge_index < edge_count:
        raise CadDesignError("Указано несуществующее ребро.")
    indices = range(edge_count) if edge_index is None else (edge_index,)
    nearest = None
    for index in indices:
        start, end = points[index], points[(index + 1) % len(points)]
        if math.dist(start, end) <= POINT_EPSILON_MM:
            continue
        edge = _curve_edge(start, end, bulges[index] if bulges else 0)
        position, distance = edge.project(click)
        if nearest is None or distance < nearest[0]:
            nearest = distance, index, edge, position
    if nearest is None:
        raise CadDesignError("На выбранном ребре недостаточно места для новых точек.")
    _, index, edge, position = nearest
    stops = [position] if count == 1 else [edge.length * step / (count + 1) for step in range(1, count + 1)]
    additions = [edge.at(stop) for stop in stops]
    if any(math.dist(first, last) <= POINT_EPSILON_MM for first, last in pairwise([edge.start, *additions, edge.end])):
        raise CadDesignError("Новые точки должны находиться внутри ребра и отличаться от существующих.")

    result = {**entity, "type": "polyline", "closed": closed,
              "points": [*points[:index + 1], *additions, *points[index + 1:]]}
    if line:
        result.pop("start")
        result.pop("end")
        result["joined"] = True
    if bulges is not None:
        split_bulges = [math.tan((end - start) / edge.length * edge.sweep / 4) if edge.sweep else 0.0
                        for start, end in pairwise([0, *stops, edge.length])]
        result["bulges"] = [*bulges[:index], *split_bulges, *bulges[index + 1:]]
    return result


def _spline_for_insertion(entity):
    if "fitPoints" not in entity:
        return _native_spline(entity)
    size = len(entity["fitPoints"]) - int(not entity["closed"])
    if 1 + 3 * size > MAX_POINTS:
        raise CadDesignError("Для добавления точек в сплайн требуется слишком много опорных точек.")
    controls = [entity["fitPoints"][0]]
    for start, out_tangent, in_tangent, end in _fit_spline_spans(entity):
        controls.extend((start + out_tangent / 3, end - in_tangent / 3, end))
    knots = [0.0] * 4 + [index / size for index in range(1, size) for _ in range(3)] + [1.0] * 4
    return BSpline(controls, order=4, knots=knots)


def _project_spline_span(evaluate, distance, start, end, steps):
    best = min(range(steps + 1), key=lambda i: distance(evaluate(start + (end - start) * i / steps)))
    left = start + (end - start) * max(0, best - 1) / steps
    right = start + (end - start) * min(steps, best + 1) / steps
    bounds = [left, right]
    for _ in range(40):
        a = (2 * left + right) / 3
        b = (left + 2 * right) / 3
        if distance(evaluate(a)) <= distance(evaluate(b)):
            right = b
        else:
            left = a
    return min((distance(evaluate(at)), at) for at in [*bounds, (left + right) / 2])


def _pick_spline_span(spline, point, edge_index):
    knots, degree = spline.knots(), spline.degree
    spans = [index for index in range(degree, spline.count) if knots[index] < knots[index + 1]]
    if edge_index is not None and not 0 <= edge_index < len(spans):
        raise CadDesignError("Указан несуществующий участок сплайна.")
    indices = spans if edge_index is None else [spans[edge_index]]
    best = None
    evaluate = spline.evaluator.point
    for index in indices:
        controls = spline.control_points[index - degree:index + 1]
        lower = [min(control[axis] for control in controls) for axis in (0, 1)]
        upper = [max(control[axis] for control in controls) for axis in (0, 1)]
        bound = sum(max(a - value, value - b, 0) ** 2 for a, b, value in zip(lower, upper, point))
        if best is not None and bound > best[0]:
            continue
        start, end = knots[index:index + 2]
        gap, at = _project_spline_span(evaluate, lambda p: (p - point).magnitude_square, start, end, max(12, degree * 4))
        if best is None or gap < best[0]:
            best = gap, at, start, end
    return best[1:]


def _even_spline_parameters(evaluate, start, end, count):
    steps = max(64, count * 8)
    parameters = [start + (end - start) * step / steps for step in range(steps + 1)]
    points = [evaluate(parameter) for parameter in parameters]
    length = [0.0]
    for first, last in pairwise(points):
        length.append(length[-1] + (last - first).magnitude)
    if length[-1] <= POINT_EPSILON_MM:
        raise CadDesignError("Участок сплайна слишком короткий для добавления точки.")
    result = []
    for step in range(1, count + 1):
        target = length[-1] * step / (count + 1)
        index = bisect_right(length, target) - 1
        ratio = (target - length[index]) / (length[index + 1] - length[index])
        result.append(parameters[index] + (parameters[index + 1] - parameters[index]) * ratio)
    return result


def _insert_spline_points(entity, click, edge_index, count):
    spline = _spline_for_insertion(entity)
    at, start, end = _pick_spline_span(spline, Vec3(click), edge_index)
    evaluate = spline.evaluator.point
    parameters = [at] if count == 1 else _even_spline_parameters(evaluate, start, end, count)
    positions = [evaluate(parameter) for parameter in [start, *parameters, end]]
    if any((last - first).magnitude <= POINT_EPSILON_MM for first, last in pairwise(positions)):
        raise CadDesignError("Новые точки должны находиться внутри участка сплайна и отличаться от существующих.")
    degree = spline.degree
    if spline.count + count * degree > MAX_POINTS:
        raise CadDesignError("Для добавления точек в сплайн требуется слишком много опорных точек.")
    weights = spline.weights()
    points = ([Vec3(point.x * weight, point.y * weight, weight) for point, weight in zip(spline.control_points, weights)]
              if weights else list(spline.control_points))
    knots = list(spline.knots())
    # Repeating to the degree leaves a control point on the curve at each inserted knot.
    # Refine the affected control range in place, including weights in homogeneous coordinates.
    for parameter in parameters:
        for _ in range(degree):
            last = bisect_right(knots, parameter) - 1
            first = last - degree + 1
            refined = []
            for index in range(first, last + 1):
                weight = (parameter - knots[index]) / (knots[index + degree] - knots[index])
                refined.append(points[index - 1].lerp(points[index], weight))
            points[first:last] = refined
            knots.insert(last + 1, parameter)
    result = {key: value for key, value in entity.items() if key not in ("fitPoints", "fitMethod")}
    result.update(controlPoints=[[point.x / point.z, point.y / point.z] if weights else [point.x, point.y] for point in points],
                  degree=degree, knots=knots, weights=[point.z for point in points] if weights else [])
    return result


def _insert_points(entity, options):
    count = options.get("count", 1)
    edge = options.get("edge")
    if type(count) is not int or not 1 <= count <= 100:
        raise CadDesignError("Введите целое количество новых точек от 1 до 100.")
    if edge is not None and type(edge) is not int:
        raise CadDesignError("Недопустимый номер ребра.")
    point = _point(options.get("point"))
    if entity["type"] in ("line", "polyline"):
        return _insert_polyline_points(entity, point, edge, count)
    if entity["type"] == "spline":
        return _insert_spline_points(entity, point, edge, count)
    raise CadDesignError("Точки можно добавлять на отрезках, полилиниях и сплайнах.")


def _replace(entities, ids, new_shapes, *, keep_original=False):
    attached = [entity for entity in entities if entity.get("parentId") in ids]
    replacements = [*new_shapes, *_move_notches(attached, new_shapes)]
    result = []
    for entity in entities:
        if entity["id"] not in ids and entity.get("parentId") not in ids:
            result.append(entity)
        elif keep_original:
            result.append({**entity, "reference": True})
    return [*result, *replacements]


OUTLINE_SIMPLIFY_TOLERANCE_MM = 0.02
OUTLINE_CORNER_COSINE = math.cos(math.radians(30))


def _simplify_outline(entity, tolerance, *, preserve_last=False, junctions=None):
    if entity["type"] != "polyline":
        return entity
    points = entity["points"]
    closed = entity["closed"]
    path = [*points, points[0]] if closed else points
    bulges = entity.get("bulges")
    curved_edges = {index for index, bulge in enumerate(bulges or ()) if bulge and index < len(path) - 1}
    if curved_edges:
        tolerance = 0
    minimum = 3 if closed and not curved_edges else 2
    if len(points) <= minimum:
        return entity

    # Preserve the authored start, end, and sharp turns while simplifying each intervening run.
    anchors = [0]
    for index, (before, point, after) in enumerate(zip(path, path[1:], path[2:]), 1):
        if index in curved_edges or index - 1 in curved_edges:
            anchors.append(index)
            continue
        incoming = [point[axis] - before[axis] for axis in (0, 1)]
        outgoing = [after[axis] - point[axis] for axis in (0, 1)]
        if math.dist(before, point) <= POINT_EPSILON_MM or math.dist(point, after) <= POINT_EPSILON_MM:
            anchors.append(index)
        elif sum(a * b for a, b in zip(incoming, outgoing)) <= OUTLINE_CORNER_COSINE * math.hypot(*incoming) * math.hypot(*outgoing):
            anchors.append(index)
    if closed and preserve_last and anchors[-1] != len(path) - 2:
        anchors.append(len(path) - 2)
    anchors.append(len(path) - 1)

    reduced = [points[0]]
    reduced_bulges = []
    for first, last in pairwise(anchors):
        if last == first + 1:
            reduced.append(path[last])
            if bulges is not None:
                reduced_bulges.append(bulges[first])
            continue
        span = LineString(path[first:last + 1]).simplify(tolerance, preserve_topology=True)
        remaining = [list(point) for point in span.coords[1:]]
        reduced.extend(remaining)
        if bulges is not None:
            reduced_bulges.extend([0.0] * len(remaining))
    if closed:
        reduced.pop()
    elif bulges is not None:
        reduced_bulges.append(bulges[-1])
    if not minimum <= len(reduced) < len(points):
        return entity
    result = {**entity, "points": reduced}
    if bulges is not None:
        result["bulges"] = reduced_bulges
    if curved_edges:
        try:
            line, candidate = (_line_geometry(_path(outline)) for outline in (entity, result))
        except CadDesignError:
            return entity
    else:
        line = LineString(path)
        candidate = LineString([*reduced, reduced[0]] if closed else reduced)
    if not line.is_simple or not candidate.is_simple:
        return entity
    if closed:
        original = Polygon(line)
        polygon = Polygon(candidate)
        if (not original.is_valid or not polygon.is_valid or polygon.area <= POINT_EPSILON_MM ** 2
                or polygon.exterior.is_ccw != original.exterior.is_ccw):
            return entity
    if tolerance and junctions is not None:
        nearby = set(junctions.query(line, predicate="dwithin", distance=tolerance))
        nearby.update(junctions.query(candidate, predicate="dwithin", distance=tolerance))
        if any(abs(line.distance(point) - candidate.distance(point)) > POINT_EPSILON_MM
               for point in junctions.geometries.take(sorted(nearby))):
            return _simplify_outline(entity, 0, preserve_last=preserve_last)

    return result


def _outline_junctions(entities, *, include_arcs=False):
    points = set()
    for entity in entities:
        kind = entity["type"]
        if kind == "point":
            points.add(tuple(entity["point"]))
        elif kind == "line":
            points.update((tuple(entity["start"]), tuple(entity["end"])))
        elif kind == "polyline" and not entity["closed"]:
            points.update((tuple(entity["points"][0]), tuple(entity["points"][-1])))
        elif include_arcs and kind == "arc":
            edge, = _quick_trim_edges(entity, 1)
            points.update((tuple(edge.start), tuple(edge.end)))
    return STRtree([Point(point) for point in points])


def _simplify_selected(entities, by_id, options):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контуры для упрощения.")
    selected = [_selected(by_id, identifier) for identifier in ids]
    if len(set(ids)) != len(ids):
        raise CadDesignError("Выбранные объекты повторяются.")
    with_notches = {entity.get("parentId") for entity in entities if entity.get("notch")}
    junctions = _outline_junctions(entities)
    shapes = [_simplify_outline(entity, 0 if entity["id"] in with_notches else OUTLINE_SIMPLIFY_TOLERANCE_MM,
                                junctions=junctions)
              for entity in selected]
    removed = sum(len(original.get("points", ())) - len(shape.get("points", ()))
                  for original, shape in zip(selected, shapes))
    if not removed:
        return entities, selected, ["Лишних вершин для безопасного упрощения не найдено."], False
    replacements = {shape["id"]: shape for shape in shapes}
    return ([replacements.get(entity["id"], entity) for entity in entities], shapes,
            [f"Удалено лишних вершин — {removed}."], True)


MAX_CORNER_OVERRUN_MM = 0.1


def _closed_corner_trial(entity):
    if entity["type"] != "polyline" or entity["closed"] or entity["points"][0] != entity["points"][-1]:
        return entity
    trial = {**entity, "closed": True, "points": entity["points"][:-1]}
    if "bulges" in entity:
        trial["bulges"] = entity["bulges"][:-1]
    return trial


def _corner_has_external_contact(loop, crossing, neighbors, work=None):
    boundary = loop.boundary
    tolerance = POINT_EPSILON_MM
    segments = [_QuickTrimEdge(start, end) for start, end in pairwise(boundary.coords)]
    geometries = [segment.geometry for segment in segments]
    remaining = MAX_TRIM_EDGES
    for neighbor in neighbors:
        edges = _checked_repair_edges(neighbor, remaining)
        remaining -= len(edges)
        if work is not None and not work(len(edges)):
            return True
        sampled = neighbor["type"] in ("ellipse", "spline")
        for edge in edges:
            geometry = edge.geometry
            if sampled and boundary.distance(geometry) <= 2 * CURVE_TOLERANCE_MM:
                return True
            if boundary.distance(geometry) > tolerance:
                continue
            if any(math.dist(point, crossing) > tolerance and boundary.distance(Point(point)) <= tolerance
                   for point in (edge.start, edge.end)):
                return True
            for segment, span in zip(segments, geometries):
                if any(kind == "overlap" or math.dist(point, crossing) > tolerance
                       for kind, point in _diagnostic_contacts(segment, edge, span, geometry)):
                    return True
    return False


def _corner_overlap(entity, neighbors, click=None, *, work=None):
    if entity["type"] != "polyline" or not entity["closed"] or len(entity["points"]) < 4:
        return None
    points = entity["points"]
    bulges = entity.get("bulges")
    count = len(points)
    overlap = None
    overlaps = 0
    click_distance = math.inf
    for index, before in enumerate(points):
        first, second, after = ((index + offset) % count for offset in (1, 2, 3))
        if bulges and any(bulges[edge] for edge in (index, first, second)):
            continue
        tip, next_tip = points[first], points[second]
        if not POINT_EPSILON_MM < math.dist(tip, next_tip) <= MAX_CORNER_OVERRUN_MM:
            continue
        incoming = LineString([before, tip])
        outgoing = LineString([next_tip, points[after]])
        if not incoming.crosses(outgoing):
            continue
        crossing = list(incoming.intersection(outgoing).coords[0])
        tails = math.dist(tip, crossing), math.dist(next_tip, crossing)
        if not all(POINT_EPSILON_MM < length <= MAX_CORNER_OVERRUN_MM for length in tails):
            continue
        if math.dist(before, crossing) < 10 * tails[0] or math.dist(points[after], crossing) < 10 * tails[1]:
            continue
        overlaps += 1
        if click is None and overlaps > 1:
            return None
        loop = Polygon([crossing, tip, next_tip])
        if click is not None:
            gap = loop.boundary.distance(click[0])
            if gap > click[1] or gap >= click_distance:
                continue
            click_distance = gap
        overlap = first, second, crossing, loop
    if overlap is None:
        return None

    first, second, crossing, loop = overlap
    # Remove the connector and both tails. Keep the original traversal and its seam whenever possible.
    retained = [index for index in range(count) if index != second]
    if second == 0:
        retained = [retained[-1], *retained[:-1]]
    outline = {**entity, "points": [crossing if index == first else points[index] for index in retained]}
    if bulges is not None:
        outline["bulges"] = [bulges[index] for index in retained]
    path = _line_geometry(_path(outline))
    polygon = Polygon(path)
    if not POINT_EPSILON_MM ** 2 < loop.area <= 0.001 * polygon.area:
        return None
    if click is not None and path.distance(click[0]) <= click_distance + POINT_EPSILON_MM:
        return None
    blocked = None, first, second, crossing
    if overlaps > 1 or not path.is_simple or not polygon.is_valid:
        return blocked
    if _corner_has_external_contact(loop, crossing, neighbors, work):
        return blocked
    return outline, first, second, crossing


def _repair_corner_overlap(entity, neighbors):
    overlap = _corner_overlap(entity, neighbors)
    return overlap[0] if overlap is not None else None


def _repair_open_corner(entity, neighbors):
    if entity["type"] != "polyline" or entity["closed"] or len(entity["points"]) < 4:
        return None, False
    points = entity["points"]
    bulges = entity.get("bulges")
    if bulges and (bulges[0] or bulges[-2]):
        return None, False
    start, end = points[0], points[-1]
    gap = math.dist(start, end)
    if not POINT_EPSILON_MM < gap <= 2 * MAX_CORNER_OVERRUN_MM:
        return None, False

    lengths = math.dist(start, points[1]), math.dist(end, points[-2])
    if min(lengths) <= POINT_EPSILON_MM:
        return None, False
    ux, uy = ((b - a) / lengths[0] for a, b in zip(start, points[1]))
    vx, vy = ((b - a) / lengths[1] for a, b in zip(end, points[-2]))
    determinant = ux * vy - uy * vx
    if not determinant:
        return None, False
    dx, dy = end[0] - start[0], end[1] - start[1]
    first = (dx * vy - dy * vx) / determinant
    last = (dx * uy - dy * ux) / determinant
    if first > POINT_EPSILON_MM and last > POINT_EPSILON_MM:
        trial = {**entity, "closed": True}
        if bulges is not None:
            trial["bulges"] = [*bulges[:-1], 0.0]
        overlap = _corner_overlap(trial, neighbors)
        if (overlap is not None and overlap[0] is not None and overlap[1:3] == (len(points) - 1, 0)
                and _conflict_boundary(overlap[0]) is not None):
            return overlap[0], True
        return None, False

    if abs(determinant) < 0.1 or any(abs(change) > min(MANUAL_CLOSURE_TOLERANCE_MM, length / 10)
                                     for change, length in zip((first, last), lengths)):
        return None, False
    corner = [start[0] + first * ux, start[1] + first * uy]
    outline = {**entity, "closed": True, "points": [corner, *points[1:-1]]}
    if bulges is not None:
        outline["bulges"] = bulges[:-1]
    boundary = _conflict_boundary(outline)
    if boundary is None:
        return None, False
    path = _line_geometry(boundary[0])
    junctions = _outline_junctions(neighbors, include_arcs=True)
    for endpoint in (start, end):
        if math.dist(endpoint, corner) <= POINT_EPSILON_MM:
            continue
        nearby = junctions.query(LineString([endpoint, corner]), predicate="dwithin", distance=POINT_EPSILON_MM)
        if any(path.distance(point) > POINT_EPSILON_MM for point in junctions.geometries.take(nearby)):
            return None, False
    return outline, False


def _closed_line_chain(entities, seed):
    joints = defaultdict(list)
    for entity in entities:
        if (entity["type"] == "line" and not entity.get("notch") and entity["layer"] == seed["layer"]
                and entity.get("reference", False) == seed.get("reference", False)
                and entity.get("groupPath", []) == seed.get("groupPath", [])):
            for side in ("start", "end"):
                joints[tuple(entity[side])].append((entity, side))
    points, chain, visited = [], [], set()
    entity, side = seed, "start"
    while entity["id"] not in visited:
        start = tuple(entity[side])
        if len(joints[start]) != 2:
            return None
        points.append(entity[side])
        chain.append((entity, side))
        visited.add(entity["id"])
        end = tuple(entity["end" if side == "start" else "start"])
        connected = joints[end]
        if len(connected) != 2:
            return None
        entity, side = next(record for record in connected if record[0] is not entity)
    return (points, chain) if len(chain) >= 4 and entity is seed and side == "start" else None


def _line_corner_changes(chain, first, second, crossing):
    connector = chain[first][0]["id"]
    incoming, incoming_side = chain[first - 1]
    outgoing, outgoing_side = chain[second]
    incoming_end = "end" if incoming_side == "start" else "start"
    return connector, [{**incoming, incoming_end: crossing}, {**outgoing, outgoing_side: crossing}]


def _erase_corner_overlap(entities, target, point, tolerance):
    click = Point(point), min(tolerance, MAX_CORNER_OVERRUN_MM)
    if target["type"] == "polyline":
        trial = _closed_corner_trial(target)
        sources = {target["id"]}
        chain = None
    elif target["type"] == "line":
        joined = _closed_line_chain(entities, target)
        if joined is None:
            return None
        points, chain = joined
        trial = {"type": "polyline", "layer": target["layer"], "points": points, "closed": True}
        sources = {entity["id"] for entity, _ in chain}
    else:
        return None
    overlap = _corner_overlap(trial, (entity for entity in entities if entity["id"] not in sources), click)
    if overlap is None:
        return None
    outline, first, second, crossing = overlap
    blocked = entities, [target], ["Петлю нельзя удалить автоматически. Контур сохранён."], False
    if outline is None:
        return blocked
    if chain is None:
        return [outline if entity is target else entity for entity in entities], [outline], [], True

    connector, shapes = _line_corner_changes(chain, first, second, crossing)
    if any(entity.get("parentId") == connector for entity in entities):
        return blocked
    replacements = {entity["id"]: entity for entity in shapes}
    return ([replacements.get(entity["id"], entity) for entity in entities if entity["id"] != connector], shapes, [], True)


MAX_REPAIR_DIAGNOSTICS = 16


def _diagnostic_contacts(first, second, first_geometry, second_geometry):
    if first.center is None and second.center is None:
        intersection = first_geometry.intersection(second_geometry)
        if intersection.is_empty:
            return
        if intersection.geom_type == "Point":
            yield "intersection", list(intersection.coords[0])
        elif intersection.geom_type == "LineString" and intersection.length > POINT_EPSILON_MM:
            yield "overlap", list(intersection.interpolate(0.5, normalized=True).coords[0])
        return

    coincident = (first.center is not None and second.center is not None
                  and math.dist(first.center, second.center) <= POINT_EPSILON_MM
                  and abs(first.radius - second.radius) <= POINT_EPSILON_MM)
    if coincident:
        stops = [0.0, first.length]
        for point in (second.start, second.end):
            if (position := first.position(point)) is not None:
                stops.append(position)
        overlapping = False
        for left, right in pairwise(sorted(stops)):
            if right - left <= POINT_EPSILON_MM:
                continue
            midpoint = first.at((left + right) / 2)
            if second.project(midpoint)[1] <= POINT_EPSILON_MM:
                overlapping = True
                yield "overlap", midpoint
        if overlapping:
            return
        for point in (first.start, first.end, second.start, second.end):
            if first.project(point)[1] <= POINT_EPSILON_MM and second.project(point)[1] <= POINT_EPSILON_MM:
                yield "intersection", point
        return

    for position in _quick_trim_crossings(first, second):
        point = first.at(position)
        if second.project(point)[1] <= POINT_EPSILON_MM:
            yield "intersection", point


def _checked_repair_edges(entity, limit=MAX_TRIM_EDGES):
    edges = list(islice(_quick_trim_edges(entity, limit), limit + 1))
    if len(edges) > limit:
        raise _CurveDetailLimit("Слишком много рёбер для безопасной проверки контура.")
    return edges


def _outline_conflicts(entity):
    edges = _checked_repair_edges(entity)
    bounds = [edge.geometry for edge in edges]
    tree = STRtree(bounds)
    loop = entity["closed"] or entity["points"][0] == entity["points"][-1]
    checks = 0
    for index, first in enumerate(edges):
        nearby = tree.query(bounds[index])
        checks += len(nearby)
        if checks > MAX_TRIM_CHECKS:
            raise _CurveDetailLimit("Слишком много пересечений для безопасной проверки контура.")
        for other in sorted(nearby):
            if other <= index:
                continue
            expected = []
            if other == index + 1:
                expected.append(first.end)
            if loop and index == 0 and other == len(edges) - 1:
                expected.append(first.start)
            for kind, point in _diagnostic_contacts(first, edges[other], bounds[index], bounds[other]):
                if kind != "intersection" or not any(math.dist(point, joint) <= POINT_EPSILON_MM for joint in expected):
                    yield kind, point


def _self_conflict_diagnostics(entity):
    if entity["type"] != "polyline":
        return []
    diagnostics = []
    try:
        for kind, point in _outline_conflicts(entity):
            if not all(abs(value) <= MAX_ABS_COORDINATE_MM for value in point):
                continue
            same = next((item for item in diagnostics if math.dist(item["point"], point) <= POINT_EPSILON_MM), None)
            if same is not None:
                if kind == "overlap":
                    same.update(point=point, kind=kind)
                continue
            diagnostics.append({"point": point, "kind": kind})
            if len(diagnostics) >= MAX_REPAIR_DIAGNOSTICS:
                break
    except (CadDesignError, GEOSException, ArithmeticError):
        pass
    return diagnostics


def _repair_outline(entity, neighbors=()):
    shape = dict(entity)
    changed = False
    removed_overlap = False
    if shape["type"] == "polyline":
        points = shape["points"]
        bulges = shape.get("bulges", [0.0] * len(points))
        clean_points, clean_bulges = [points[0]], [bulges[0]]
        for point, bulge in zip(points[1:], bulges[1:]):
            if math.dist(clean_points[-1], point) <= POINT_EPSILON_MM:
                clean_bulges[-1] = bulge
                changed = True
            else:
                clean_points.append(point)
                clean_bulges.append(bulge)
        if shape["closed"] and len(clean_points) > 1 and math.dist(clean_points[-1], clean_points[0]) <= POINT_EPSILON_MM:
            clean_points.pop()
            clean_bulges.pop()
            changed = True
        if len(clean_points) < 2 or shape["closed"] and len(clean_points) < 3 and not any(clean_bulges):
            raise CadDesignError("После удаления совпадающих вершин не останется пригодного контура.")
        if changed:
            shape["points"] = clean_points
            if "bulges" in shape:
                shape["bulges"] = clean_bulges

    if not _closed(shape):
        corner, removed_overlap = _repair_open_corner(shape, neighbors)
        if corner is not None:
            shape = corner
            changed = True
    path = _path(shape)
    line = _line_geometry(path)
    if not line.is_simple:
        corrected = _repair_corner_overlap(_closed_corner_trial(shape), neighbors)
        if corrected is None:
            raise _RepairConflict("Контур самопересекается или имеет наложенные рёбра. Исправьте пересечения вручную.",
                                  _self_conflict_diagnostics(shape))
        shape = corrected
        changed = removed_overlap = True
        path = _path(shape)
        line = _line_geometry(path)
    if not path["closed"]:
        gap = math.dist(path["points"][0], path["points"][-1])
        if shape["type"] != "polyline" or len(path["points"]) < 4:
            raise CadDesignError("Открытый фрагмент. Выберите все рёбра внешнего контура для соединения.")
        repaired = repair_closed_contours([path], bridge_small_gaps=True)
        if repaired.closed_count != 1 or len(repaired.paths) != 1 or not repaired.paths[0]["closed"]:
            raise CadDesignError(f"Контур разомкнут. Между концами {gap:.3f} мм. "
                                 f"Автозамыкание допускается до {MANUAL_CLOSURE_TOLERANCE_MM:g} мм без пересечения рёбер.")
        if gap <= POINT_EPSILON_MM:
            shape["points"] = shape["points"][:-1]
            if "bulges" in shape:
                shape["bulges"] = shape["bulges"][:-1]
        elif "bulges" in shape:
            shape["bulges"] = [*shape["bulges"][:-1], 0.0]
        shape["closed"] = True
        changed = True
        line = _line_geometry(_path(shape))

    polygon = Polygon(line)
    if not polygon.is_valid or polygon.area <= POINT_EPSILON_MM ** 2:
        raise CadDesignError("Контур не образует замкнутую область без пересечений.")
    reduced = _simplify_outline(shape, 0, preserve_last=not _closed(entity))
    return reduced, changed or reduced is not shape, removed_overlap


def _retain_import_issues(shape, sources, resolved):
    issues = list(dict.fromkeys(issue for entity in sources for issue in
                               entity.get("importIssues", [LEGACY_IMPORT_ISSUE] if entity.get("importDamaged") else [])
                               if issue not in resolved))
    if issues:
        shape.update(importDamaged=True, importIssues=issues[:12])
    else:
        shape.pop("importDamaged", None)
        shape.pop("importIssues", None)
    return issues


def _accept_imported_segment(entity):
    if IMPORT_DOUBLED_LINE not in entity.get("importIssues", ()):
        return None
    shape = dict(entity)
    if entity["type"] != "line":
        if (entity["type"] != "polyline" or entity["closed"]
                or len(entity["points"]) != 2 or any(entity.get("bulges", ()))
                or math.dist(*entity["points"]) <= POINT_EPSILON_MM):
            return None
        start, end = shape.pop("points")
        for field in ("closed", "bulges", "joined"):
            shape.pop(field, None)
        shape.update(type="line", start=start, end=end)
    issues = _retain_import_issues(shape, [entity], {IMPORT_DOUBLED_LINE, IMPORT_DUPLICATE_POINTS})
    message = "Оставлен один отрезок. Остальные замечания требуют проверки." if issues else "Оставлен один отрезок."
    return shape, message


def _same_conflict_edge(first, second, reverse=False):
    start, end = (second.end, second.start) if reverse else (second.start, second.end)
    if first.center is None or second.center is None:
        return first.center is None and second.center is None and first.start == start and first.end == end
    sweep = -second.sweep if reverse else second.sweep
    return (math.dist(first.start, start) <= POINT_EPSILON_MM and math.dist(first.end, end) <= POINT_EPSILON_MM
            and math.dist(first.center, second.center) <= POINT_EPSILON_MM
            and abs(first.radius - second.radius) <= POINT_EPSILON_MM
            and abs(first.sweep - sweep) * max(first.radius, second.radius) <= POINT_EPSILON_MM)


def _same_conflict_path(first, second):
    return len(first) == len(second) and (
        all(_same_conflict_edge(a, b) for a, b in zip(first, second))
        or all(_same_conflict_edge(a, b, reverse=True) for a, b in zip(first, reversed(second)))
    )


def _conflict_boundary(outline):
    if not outline["closed"]:
        return None
    path = _path(outline)
    line = _line_geometry(path)
    polygon = Polygon(line)
    if (not line.is_simple or not polygon.is_valid or polygon.area <= POINT_EPSILON_MM ** 2
            or next(_outline_conflicts(outline), None) is not None):
        return None
    return path, polygon


def _edges_have_only_joint_contact(branch_edges, boundary, joint):
    bounds = [edge.geometry for edge in boundary]
    tree = STRtree(bounds)
    checks = 0
    for edge in branch_edges:
        geometry = edge.geometry
        nearby = tree.query(geometry)
        checks += len(nearby)
        if checks > MAX_TRIM_CHECKS:
            return False
        for index in nearby:
            if any(kind != "intersection" or math.dist(point, joint) > POINT_EPSILON_MM
                   for kind, point in _diagnostic_contacts(edge, boundary[index], geometry, bounds[index])):
                return False
    return True


def _clear_conflict_branch(branch, outline, boundary, joint, neighbors, branch_edges, work=None):
    from nesting import annotate_notches

    boundary_path, polygon = boundary
    branch_path = _path(branch)
    geometry = _line_geometry(branch_path)
    if (not geometry.is_simple or geometry.distance(Point(joint)) > POINT_EPSILON_MM
            or not Point(joint).buffer(2 * POINT_EPSILON_MM).covers(geometry.intersection(polygon))
            or not _edges_have_only_joint_contact(branch_edges, _checked_repair_edges(outline), joint)
            or annotate_notches([boundary_path, branch_path])[1]["notch"]):
        return False

    remaining = MAX_POINTS
    curved_branch = any(edge.center is not None for edge in branch_edges)
    for neighbor in neighbors:
        if neighbor["type"] == "point":
            other = Point(neighbor["point"])
        else:
            path = _path(neighbor, remaining)
            remaining -= len(path["points"])
            if work is not None and not work(len(path["points"])):
                return False
            other = _line_geometry(path)
        if curved_branch or neighbor["type"] in ("arc", "circle", "ellipse", "spline") or any(neighbor.get("bulges", ())):
            clearance = 2 * CURVE_TOLERANCE_MM
        else:
            clearance = POINT_EPSILON_MM
        if geometry.distance(other) <= clearance:
            return False
    return True


def _conflict_scope(entity):
    return entity["layer"], tuple(entity.get("groupPath", ())), entity.get("reference", False)


def _protected_conflict_source(entity, attached):
    return entity["id"] in attached or IMPORT_DOUBLED_LINE in entity.get("importIssues", ())


def _duplicate_keeps_review(source, survivor):
    return IMPORT_DOUBLED_LINE not in survivor.get("importIssues", ()) and (
        not source.get("importDamaged") or survivor.get("importDamaged")
        and set(source.get("importIssues", ())).issubset(survivor.get("importIssues", ()))
    )


def _fix_conflict_branch(entities, selected, point):
    if not 3 <= len(selected) <= MAX_TRIM_EDGES or any(_closed(entity) or entity["type"] not in ("line", "arc", "polyline")
                                                     for entity in selected):
        return None
    scope = {_conflict_scope(entity) for entity in selected}
    if len(scope) != 1:
        return None
    selected_ids = {entity["id"] for entity in selected}
    attached = {entity["parentId"] for entity in entities if "parentId" in entity}
    neighbors = [entity for entity in entities if entity["id"] not in selected_ids]
    edges = {}
    remaining = MAX_TRIM_EDGES
    touching = []
    for entity in selected:
        path = _checked_repair_edges(entity, remaining)
        if not path:
            return None
        remaining -= len(path)
        edges[entity["id"]] = path
        if min(math.dist(point, path[0].start), math.dist(point, path[-1].end)) <= POINT_EPSILON_MM:
            touching.append(entity)
    if not 3 <= len(touching) <= 4:
        return None

    accepted = None
    for candidate in reversed(touching):
        identifier = candidate["id"]
        if _protected_conflict_source(candidate, attached):
            continue
        tail = edges[identifier]
        survivors = [entity for entity in selected if entity is not candidate]
        duplicates = [entity for entity in touching if entity is not candidate
                      and _same_conflict_path(tail, edges[entity["id"]])]
        duplicate = bool(duplicates)
        if duplicate and not any(_duplicate_keeps_review(candidate, entity) for entity in duplicates):
            continue
        try:
            outline = _join_shapes(survivors, POINT_EPSILON_MM)
        except CadDesignError:
            continue
        boundary = _conflict_boundary(outline)
        if boundary is None:
            continue
        if not duplicate:
            start_at_joint = math.dist(point, tail[0].start) <= POINT_EPSILON_MM
            end_at_joint = math.dist(point, tail[-1].end) <= POINT_EPSILON_MM
            if start_at_joint == end_at_joint or not _clear_conflict_branch(candidate, outline, boundary, point, neighbors, tail):
                continue
        if accepted is not None:
            prior_id, _, prior_duplicate = accepted
            if duplicate and prior_duplicate and _same_conflict_path(tail, edges[prior_id]):
                continue
            return None
        accepted = identifier, survivors, duplicate
    if accepted is None:
        return None
    identifier, survivors, duplicate = accepted
    message = "Совпадающее ребро удалено. Наружный контур замкнут." if duplicate else "Лишняя ветвь удалена. Наружный контур замкнут."
    return [entity for entity in entities if entity["id"] != identifier], survivors, message


def _fix_conflict_retrace(entities, selected, point):
    if len(selected) != 1 or selected[0]["type"] != "polyline":
        return None
    original = selected[0]
    outline = _closed_corner_trial(original)
    points = outline["points"]
    count = len(points)
    if not outline["closed"] or not 5 <= count <= MAX_TRIM_EDGES or IMPORT_DOUBLED_LINE in original.get("importIssues", ()):
        return None
    bulges = outline.get("bulges", [0.0] * count)
    reduction = None
    for index, start in enumerate(points):
        back, repeat, end = ((index + offset) % count for offset in (1, 2, 3))
        if start != points[repeat] or points[back] != points[end] or math.dist(start, points[back]) <= POINT_EPSILON_MM:
            continue
        if not bulges[index] == bulges[repeat] == -bulges[back]:
            continue
        edge = _curve_edge(start, points[back], bulges[index])
        if math.dist(point, edge.at(edge.length / 2)) > POINT_EPSILON_MM:
            continue
        retained = [vertex for vertex in range(count) if vertex not in (back, repeat)]
        if 0 in (back, repeat):
            seam = retained.index(index)
            retained = retained[seam:] + retained[:seam]
        candidate = {**outline, "points": [points[vertex] for vertex in retained]}
        if "bulges" in outline:
            candidate["bulges"] = [bulges[vertex] for vertex in retained]
        if _conflict_boundary(candidate) is None:
            continue
        if reduction is not None and candidate != reduction:
            return None
        reduction = candidate
    if reduction is None:
        return None
    return ([reduction if entity is original else entity for entity in entities], [reduction],
            "Повторный проход по ребру удалён. Наружный контур замкнут.")


def _conflict_fix(entities, selected, point, kind):
    try:
        if kind == "branch":
            return _fix_conflict_branch(entities, selected, point)
        if kind == "overlap":
            return _fix_conflict_retrace(entities, selected, point)
    except (CadDesignError, GEOSException, ArithmeticError):
        pass
    return None


def _annotate_repair_conflicts(entities, selected, diagnostics):
    for issue in diagnostics:
        if _conflict_fix(entities, selected, issue["point"], issue["kind"]) is not None:
            issue["fixable"] = True


def _repair_conflict(entities, by_id, options):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контур с отмеченным конфликтом.")
    selected = [_selected(by_id, value) for value in ids]
    if len(set(ids)) != len(ids):
        raise CadDesignError("Выбранные объекты повторяются.")
    kind = options.get("kind")
    if kind not in ("branch", "intersection", "overlap"):
        raise CadDesignError("Укажите известный тип конфликта контура.")
    fix = _conflict_fix(entities, selected, _point(options.get("point")), kind)
    if fix is None:
        return entities, selected, ["Этот конфликт нельзя исправить автоматически. Контур сохранён."], False
    result, shapes, message = fix
    return result, shapes, [message], True


def _bulk_conflict_components(selected, source_scopes=None):
    components = []
    records = {}
    joints = defaultdict(list)
    remaining = MAX_TRIM_EDGES
    for entity in selected:
        if entity.get("notch") or entity["type"] not in ("line", "arc", "polyline"):
            continue
        outline = _closed_corner_trial(entity)
        if _closed(outline):
            components.append([entity["id"]])
            continue
        edges = _checked_repair_edges(entity, remaining)
        if not edges:
            continue
        remaining -= len(edges)
        scope = (_conflict_scope(entity), source_scopes.get(entity["id"]) if source_scopes is not None else None)
        endpoints = tuple(tuple(round(value, 6) for value in point) for point in (edges[0].start, edges[-1].end))
        identifier = entity["id"]
        nodes = tuple((scope, point) for point in endpoints)
        records[identifier] = (entity, edges, nodes)
        for node in nodes:
            joints[node].append(identifier)

    positions = {identifier: index for index, identifier in enumerate(records)}
    visited = set()
    for identifier in records:
        if identifier in visited:
            continue
        component = []
        pending = [identifier]
        visited.add(identifier)
        while pending:
            current = pending.pop()
            component.append(current)
            for node in records[current][2]:
                for neighbor in joints.pop(node, ()):
                    if neighbor not in visited:
                        pending.append(neighbor)
                        visited.add(neighbor)
        components.append(sorted(component, key=positions.__getitem__))
    return components, records


def _bulk_duplicate_classes(component, records):
    classes = []
    by_endpoints = defaultdict(list)
    checks = 0
    for identifier in component:
        _, edges, nodes = records[identifier]
        key = tuple(sorted(nodes))
        for index in by_endpoints[key]:
            other = records[classes[index][0]][1]
            checks += 1 + min(len(edges), len(other))
            if checks > MAX_TRIM_CHECKS:
                raise _CurveDetailLimit("Слишком много совпадающих рёбер для безопасной проверки контура.")
            if _same_conflict_path(edges, other):
                classes[index].append(identifier)
                break
        else:
            by_endpoints[key].append(len(classes))
            classes.append([identifier])
    return classes


def _bulk_keep_duplicate_ids(identifiers, records, attached):
    # Avoid quadratic work for drawings with very large stacks of repeated paths.
    if len(identifiers) > 64:
        return set(identifiers)
    kept = list(identifiers)
    for identifier in reversed(identifiers):
        source = records[identifier][0]
        if not _protected_conflict_source(source, attached) and any(
            other != identifier and _duplicate_keeps_review(source, records[other][0]) for other in kept
        ):
            kept.remove(identifier)
    return set(kept)


def _bulk_open_conflicts(component, records, entities, attached, work):
    classes = _bulk_duplicate_classes(component, records)
    joints = defaultdict(set)
    ends = [records[identifiers[0]][2] for identifiers in classes]
    if any(first == last for first, last in ends):
        return set()
    for index, (first, last) in enumerate(ends):
        joints[first].add(index)
        joints[last].add(index)
    degrees = {node: indices.copy() for node, indices in joints.items()}
    leaves = deque(node for node, indices in degrees.items() if len(indices) == 1)
    core = set(range(len(classes)))
    while leaves:
        node = leaves.popleft()
        if len(degrees[node]) != 1:
            continue
        index = next(iter(degrees[node]))
        core.remove(index)
        for endpoint in ends[index]:
            degrees[endpoint].remove(index)
            if len(degrees[endpoint]) == 1:
                leaves.append(endpoint)
    if len(core) < 2 or any(len(indices) not in (0, 2) for indices in degrees.values()):
        return set()
    peeled = set(range(len(classes))) - core
    if not peeled and all(len(identifiers) == 1 for identifiers in classes):
        return set()

    # Prove one closed native boundary using only the distinct cycle edges.
    outlines = []
    for index in sorted(core):
        representative = next((records[identifier][0] for identifier in classes[index]
                               if IMPORT_DOUBLED_LINE not in records[identifier][0].get("importIssues", ())), None)
        if representative is None:
            return set()
        outlines.append(representative)
    try:
        outline = _join_shapes(outlines, POINT_EPSILON_MM)
    except CadDesignError:
        return set()
    boundary = _conflict_boundary(outline)
    if boundary is None:
        return set()

    removed = set()
    for identifiers in classes:
        removed.update(set(identifiers) - _bulk_keep_duplicate_ids(identifiers, records, attached))
    if not peeled or not work(len(entities)):
        return removed
    component_ids = set(component)
    neighbors = [entity for entity in entities if entity["id"] not in component_ids]
    peeled_edge_count = sum(len(records[classes[index][0]][1]) for index in peeled)
    for index in sorted(peeled):
        first, last = ends[index]
        joint, tip = (first, last) if len(degrees[first]) == 2 else (last, first)
        if len(degrees[joint]) != 2 or len(joints[tip]) != 1:
            continue
        identifiers = classes[index]
        if any(_protected_conflict_source(records[identifier][0], attached) for identifier in identifiers):
            continue
        candidate, edges, _ = records[identifiers[0]]
        edge_checks = len(edges) * (len(outline["points"]) + peeled_edge_count - len(edges))
        if not work(edge_checks + len(neighbors)):
            break
        other_edges = [edge for other in peeled if other != index for edge in records[classes[other][0]][1]]
        point = joint[1]
        try:
            if (_clear_conflict_branch(candidate, outline, boundary, point, neighbors, edges, work)
                    and _edges_have_only_joint_contact(edges, other_edges, point)):
                removed.update(identifiers)
        except (CadDesignError, GEOSException, ArithmeticError):
            continue
    return removed


def _bulk_closed_retraces(entity):
    outline = _closed_corner_trial(entity)
    if (outline["type"] != "polyline" or not outline["closed"]
            or IMPORT_DOUBLED_LINE in outline.get("importIssues", ())):
        return None
    points = outline["points"]
    count = len(points)
    if not 5 <= count <= MAX_TRIM_EDGES:
        return None
    seam = next((index for index in range(count) if points[index - 1] != points[(index + 1) % count]), None)
    if seam is None:
        return None
    bulges = outline.get("bulges", [0.0] * count)
    kept = []
    for offset in range(count + 1):
        kept.append((seam + offset) % count)
        if len(kept) < 4:
            continue
        first, back, forward, last = kept[-4:]
        if (points[first] == points[forward] and points[back] == points[last]
                and points[first] != points[back] and bulges[first] == bulges[forward] == -bulges[back]):
            del kept[-3:-1]
    kept.pop()
    if len(kept) == count or len(kept) < 2:
        return None
    if 0 in kept:
        start = kept.index(0)
        kept = kept[start:] + kept[:start]
    result = {**outline, "points": [points[index] for index in kept]}
    if "bulges" in outline:
        result["bulges"] = [bulges[index] for index in kept]
    return result if _conflict_boundary(result) is not None else None


def _bulk_corner_overlap(component, by_id, entities, attached, work):
    chain = None
    if len(component) == 1:
        trial = _closed_corner_trial(by_id[component[0]])
        if not _closed(trial) or IMPORT_DOUBLED_LINE in trial.get("importIssues", ()):
            return None
    else:
        sources = [by_id[identifier] for identifier in component]
        if any(entity["type"] != "line" for entity in sources) or not work(len(sources)):
            return None
        joined = _closed_line_chain(sources, sources[0])
        if joined is None or len(joined[1]) != len(component):
            return None
        points, chain = joined
        trial = {"type": "polyline", "layer": sources[0]["layer"], "closed": True, "points": points}
    if not work(len(trial["points"])):
        return None

    source_ids = set(component)

    def neighbors():
        if not work(len(entities)):
            raise _CurveDetailLimit("Слишком много соседних объектов для безопасного восстановления петли.")
        yield from (entity for entity in entities if entity["id"] not in source_ids)

    overlap = _corner_overlap(trial, neighbors(), work=work)
    if overlap is None:
        return None
    outline, first, second, crossing = overlap
    if outline is None or _conflict_boundary(outline) is None:
        return None
    if chain is None:
        return None, [outline]
    connector, shapes = _line_corner_changes(chain, first, second, crossing)
    if _protected_conflict_source(by_id[connector], attached):
        return None
    return connector, shapes


def _bulk_manual_components(components, records, removed):
    # Use the manual tolerance for locators only. The proof and all changes use exact connectivity.
    surviving = [[identifier for identifier in component if identifier not in removed] for component in components]
    parent = list(range(len(surviving)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners = {}
    for index, component in enumerate(surviving):
        for identifier in component:
            if identifier in records:
                for node in records[identifier][2]:
                    owners[node] = index
    buckets = defaultdict(list)
    tolerance = MANUAL_CLOSURE_TOLERANCE_MM
    checks = 0
    for (scope, point), index in owners.items():
        x, y = (math.floor(value / tolerance) for value in point)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nearby = buckets[scope, x + dx, y + dy]
                checks += len(nearby)
                if checks > MAX_TRIM_CHECKS:
                    raise _CurveDetailLimit("Слишком много соседних концов для проверки развилок.")
                for coordinate, other in nearby:
                    if math.dist(point, coordinate) <= tolerance:
                        parent[root(index)] = root(other)
        buckets[scope, x, y].append((point, index))
    linked = defaultdict(list)
    for index, component in enumerate(surviving):
        linked[root(index)].extend(component)
    return list(linked.values())


def _bulk_remaining_issues(component, records):
    if len(component) == 1:
        return _self_conflict_diagnostics(component[0])
    degree = Counter(node for entity in component for node in records[entity["id"]][2])
    issues = [{"point": list(node[1]), "kind": "branch"} for node, count in degree.items() if count > 2]
    if issues:
        return issues
    for tolerance in (POINT_EPSILON_MM, MANUAL_CLOSURE_TOLERANCE_MM):
        try:
            outline = _join_shapes(component, tolerance)
        except CadDesignError as error:
            if (issues := getattr(error, "diagnostics", [])):
                return issues
        else:
            if _closed(outline) or tolerance == MANUAL_CLOSURE_TOLERANCE_MM:
                return _self_conflict_diagnostics(outline)
    return []


NO_SAFE_REPAIR_CONFLICTS = "Конфликтов для безопасного исправления не найдено."


def _repair_conflicts(entities, by_id, options, diagnostics, diagnostic_ids, source_scopes=None):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контуры для исправления конфликтов.")
    ids = [_text(identifier, "идентификатор объекта", 128) for identifier in ids]
    if len(set(ids)) != len(ids) or any(identifier not in by_id for identifier in ids):
        raise CadDesignError("Выбраны повторяющиеся или отсутствующие объекты.")
    selected = [by_id[identifier] for identifier in ids]
    removed = set()
    replacements = {}
    fixes = 0
    corner_fixes = 0
    attached = {entity["parentId"] for entity in entities if "parentId" in entity}
    try:
        components, records = _bulk_conflict_components(selected, source_scopes)
    except (CadDesignError, GEOSException, ArithmeticError):
        return entities, selected, ["Выделение слишком сложное для безопасной проверки конфликтов."], False
    limited = False
    checks = 0

    def work(amount):
        nonlocal checks, limited
        checks += amount
        if checks > MAX_TRIM_CHECKS:
            limited = True
            return False
        return True

    for component in components:
        try:
            corner = _bulk_corner_overlap(component, by_id, entities, attached, work)
            if corner is not None:
                connector, shapes = corner
                if connector is not None:
                    removed.add(connector)
                for shape in shapes:
                    identifier = shape["id"]
                    replacements[identifier] = shape
                    if identifier in records:
                        scope = records[identifier][2][0][0]
                        edges = _checked_repair_edges(shape)
                        nodes = tuple((scope, tuple(round(value, 6) for value in shape[side]))
                                      for side in ("start", "end"))
                        records[identifier] = shape, edges, nodes
                fixes += 1
                corner_fixes += 1
                continue
            if len(component) > 1:
                changes = _bulk_open_conflicts(component, records, entities, attached, work)
                removed.update(changes)
                fixes += len(changes)
            elif (shape := _bulk_closed_retraces(by_id[component[0]])) is not None:
                original = _closed_corner_trial(by_id[shape["id"]])
                replacements[shape["id"]] = shape
                fixes += (len(original["points"]) - len(shape["points"])) // 2
        except (CadDesignError, GEOSException, ArithmeticError):
            limited = True

    result = [replacements.get(entity["id"], entity) for entity in entities if entity["id"] not in removed]
    shapes = [replacements.get(entity["id"], entity) for entity in selected if entity["id"] not in removed]
    residual_count = 0
    try:
        residual_components = _bulk_manual_components(components, records, removed)
    except _CurveDetailLimit:
        residual_components = [[identifier for identifier in component if identifier not in removed] for component in components]
        limited = True
    for component in residual_components:
        survivors = [replacements.get(identifier, by_id[identifier]) for identifier in component]
        if not survivors:
            continue
        try:
            issues = _bulk_remaining_issues(survivors, records)
        except (CadDesignError, GEOSException, ArithmeticError):
            limited = True
            continue
        residual_count += len(issues)
        issue_ids = [entity["id"] for entity in survivors]
        if issues:
            diagnostic_ids.extend(issue_ids)
        available = MAX_REPAIR_DIAGNOSTICS - len(diagnostics)
        if available > 0:
            issues = issues[:available]
            _annotate_repair_conflicts(result, survivors, issues)
            diagnostics.extend({**issue, "ids": issue_ids} for issue in issues)

    messages = []
    if fixes:
        messages.append(f"Исправлено конфликтов — {fixes}.")
    if corner_fixes:
        messages.append(f"Удалено малых петель в углах — {corner_fixes}. Контуры остались замкнутыми.")
    if residual_count:
        messages.append(f"Осталось отмеченных конфликтов — {residual_count}.")
    if residual_count > len(diagnostics):
        messages.append(f"Показано {len(diagnostics)} из {residual_count}.")
    if limited:
        messages.append("Часть контуров слишком сложна для безопасной проверки.")
    return result, shapes, messages or [NO_SAFE_REPAIR_CONFLICTS], bool(fixes)


def _import_conflict_cleanup(entities, document, source_scopes):
    edge_count = sum(len(entity["points"]) - int(not entity["closed"]) if entity["type"] == "polyline" else 1
                     for entity in entities if entity["type"] in ("line", "arc", "polyline"))
    skipped = {"repairSummary": "Автоисправление пропущено из-за сложности чертежа. Геометрия сохранена."}
    if edge_count > MAX_TRIM_EDGES:
        return skipped

    diagnostics, diagnostic_ids = [], []
    try:
        # Imported groups and block instances are distinct sources even when their geometry coincides.
        groups = defaultdict(set)
        memberships = 0
        for name, group in document.groups:
            for member in group:
                memberships += 1
                if memberships > MAX_ENTITIES:
                    return skipped
                groups[id(member)].add(name.casefold())
        scopes = {identifier: (instance, tuple(sorted(groups.get(source, ()))))
                  for identifier, (instance, source) in source_scopes.items()}
        by_id = {entity["id"]: entity for entity in entities}
        cleaned, _, messages, changed = _repair_conflicts(
            entities, by_id, {"ids": list(by_id)}, diagnostics, diagnostic_ids, scopes,
        )
        result = {"entities": validate_design(cleaned)} if changed else {}
    except (ezdxf.DXFError, GEOSException, ArithmeticError, ValueError, TypeError, IndexError):
        return {"repairSummary": "Автоисправление не удалось. Исходная геометрия сохранена."}
    if messages != [NO_SAFE_REPAIR_CONFLICTS]:
        result["repairSummary"] = " ".join(messages)
    if diagnostics:
        result["diagnostics"] = diagnostics
        result["diagnosticIds"] = diagnostic_ids
    return result


def _repair_selected(entities, by_id, options, diagnostics):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контур или несколько фрагментов одной детали.")
    selected = [_selected(by_id, value) for value in ids]
    if len(set(ids)) != len(ids):
        raise CadDesignError("Выбранные объекты повторяются.")

    if len(selected) == 1 and (accepted := _accept_imported_segment(selected[0])) is not None:
        shape, message = accepted
        return [shape if entity is selected[0] else entity for entity in entities], [shape], [message], True

    sources = [selected[0]]
    if len(selected) > 1:
        if any(_closed(entity) for entity in selected) or len({entity["layer"] for entity in selected}) > 1:
            raise CadDesignError("Для соединения выберите открытые фрагменты одной детали на одном слое.")
        if any(entity["type"] in ("spline", "ellipse") for entity in selected):
            raise CadDesignError("Сплайн или эллипс нельзя соединить этим методом без изменения формы. Соедините вручную.")
        sources = selected
        exact_diagnostics = []
        try:
            exact = _join_shapes(selected, POINT_EPSILON_MM)
        except CadDesignError as error:
            exact = None
            exact_diagnostics = getattr(error, "diagnostics", [])
        if exact is not None and _closed(exact):
            contour = exact
        else:
            try:
                contour = _join_shapes(selected, MANUAL_CLOSURE_TOLERANCE_MM)
            except CadDesignError as error:
                diagnostics.extend(exact_diagnostics or getattr(error, "diagnostics", []))
                _annotate_repair_conflicts(entities, selected, diagnostics)
                return entities, selected, [str(error)], False
    else:
        contour = selected[0]

    source_ids = {entity["id"] for entity in sources}
    try:
        repaired, changed, removed_overlap = _repair_outline(
            contour, (entity for entity in entities if entity["id"] not in source_ids),
        )
    except CadDesignError as error:
        diagnostics.extend(getattr(error, "diagnostics", []))
        _annotate_repair_conflicts(entities, selected, diagnostics)
        return entities, selected, [str(error)], False
    repaired["id"] = sources[0]["id"]
    unresolved = _retain_import_issues(repaired, sources, {IMPORT_DUPLICATE_POINTS})
    changed = changed or len(sources) > 1 or repaired != contour
    if not changed:
        message = "Контур замкнут. Проверьте отмеченные ошибки по исходному чертежу." if unresolved else "Контур уже замкнут и не самопересекается."
        return entities, selected, [message], False

    remove = set(ids[1:])
    result = []
    for entity in entities:
        if entity["id"] == repaired["id"]:
            result.append(repaired)
        elif entity["id"] not in remove:
            result.append({**entity, "parentId": repaired["id"]} if entity.get("parentId") in remove else entity)
    message = "Контур замкнут. Отмеченные потери при импорте требуют ручной проверки." if unresolved else "Контур детали восстановлен."
    messages = [message]
    if removed_overlap:
        messages.append("Малая петля в углу удалена. Контур остался замкнутым.")
    removed = len(contour.get("points", ())) - len(repaired.get("points", ()))
    if removed > 0:
        messages.append(f"Удалено лишних вершин — {removed}.")
    return result, [repaired], messages, True


def _scale_design(entities, by_id, options):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контуры для масштабирования.")
    ids = {_text(value, "идентификатор объекта", 128) for value in ids}
    if not ids <= by_id.keys():
        raise CadDesignError("Часть выбранных объектов отсутствует в чертеже.")
    origin = _point(options.get("origin"))
    sx, sy = (_positive(options.get(axis), "Коэффициент масштаба") for axis in ("sx", "sy"))
    uniform = sx == sy
    tolerance = CURVE_TOLERANCE_MM / max(sx, sy)

    def point(p):
        return [origin[0] + sx * (p[0] - origin[0]), origin[1] + sy * (p[1] - origin[1])]

    def direction(p):
        return Vec3(sx * p[0], sy * p[1])

    changed = []
    flattened = False
    remaining = MAX_POINTS
    for entity in entities:
        if entity["id"] not in ids and entity.get("parentId") not in ids:
            changed.append(entity)
            continue
        scaled = dict(entity)
        kind = entity["type"]
        if kind in ("arc", "circle") and uniform:
            scaled.update(center=point(entity["center"]), radius=entity["radius"] * sx)
        elif kind in ("arc", "circle", "ellipse"):
            if kind == "ellipse":
                x, y = entity["axis"]
                ratio = entity["ratio"]
                axes = (x, y), (-y * ratio, x * ratio)
            else:
                radius = entity["radius"]
                axes = (radius, 0), (0, radius)
                scaled.pop("radius")
            scaled.update(_conic(Vec3(point(entity["center"])), *map(direction, axes),
                                 entity.get("startAngle", 0), entity.get("sweep", 360), circular=False))
        elif kind == "polyline" and not uniform and any(entity.get("bulges", [])):
            points = entity["points"]
            scaled_points = [point(points[0])]
            for index in range(len(points) - int(not entity["closed"])):
                start, end = points[index], points[(index + 1) % len(points)]
                if math.dist(start, end) <= POINT_EPSILON_MM:
                    continue
                for vertex in _bulge_points(start, end, entity["bulges"][index], tolerance):
                    scaled_points.append(point(vertex))
                    if len(scaled_points) > remaining + 1:
                        raise CadDesignError("Масштабированный чертёж содержит слишком много точек.")
            if entity["closed"]:
                scaled_points.pop()
            remaining -= len(scaled_points)
            scaled.update(points=scaled_points)
            scaled.pop("bulges")
            flattened = True
        else:
            for field in ("start", "end", "point"):
                if field in entity:
                    scaled[field] = point(entity[field])
            for field in ("points", "fitPoints", "controlPoints"):
                if field in entity:
                    scaled[field] = list(map(point, entity[field]))
        if scaled.get("notch") and scaled.get("parentId") not in ids:
            scaled.pop("parentId", None)
        changed.append(scaled)
    warnings = ["Дуговые сегменты полилиний при масштабировании по одной оси представлены полилиниями."] if flattened else []
    return changed, [entity for entity in changed if entity["id"] in ids], warnings


def edit_design(entities, operation, options):
    entities = validate_design(entities)
    if not isinstance(options, dict):
        raise CadDesignError("Неверные параметры операции с чертежом.")
    by_id = {entity["id"]: entity for entity in entities}
    warnings = []
    diagnostics = []
    diagnostic_ids = []
    try:
        if operation == "join":
            ids = options.get("ids")
            if not isinstance(ids, list) or len(ids) > MAX_ENTITIES:
                raise CadDesignError("Выберите объекты для объединения.")
            selected = [_selected(by_id, value) for value in ids]
            if len(set(ids)) != len(ids):
                raise CadDesignError("Выбранные объекты повторяются.")
            tolerance = _positive(options.get("tolerance", MANUAL_CLOSURE_TOLERANCE_MM), "Допуск")
            if tolerance > 1:
                raise CadDesignError("Допуск объединения должен быть не больше 1 мм.")
            shapes = [_join_shapes(selected, tolerance)]
            result = _replace(entities, set(ids), shapes)
            if any(entity["type"] in ("spline", "ellipse") for entity in selected):
                warnings.append("Сплайны и эллипсы в соединённом контуре представлены полилинией.")
        elif operation == "trim-quick":
            result, shapes, warnings, changed = _quick_trim(entities, options)
        elif operation == "repair-part":
            result, shapes, warnings, changed = _repair_selected(entities, by_id, options, diagnostics)
        elif operation == "repair-conflict":
            result, shapes, warnings, changed = _repair_conflict(entities, by_id, options)
        elif operation == "repair-conflicts":
            result, shapes, warnings, changed = _repair_conflicts(entities, by_id, options, diagnostics, diagnostic_ids)
        elif operation == "simplify":
            result, shapes, warnings, changed = _simplify_selected(entities, by_id, options)
        elif operation == "scale":
            result, shapes, warnings = _scale_design(entities, by_id, options)
        elif operation in ("notch", "notch-series", "notch-edit"):
            result, shapes = _edit_notches(entities, by_id, operation, options)
        else:
            entity = _selected(by_id, options.get("id"))
            if operation == "insert-points":
                inserted = _insert_points(entity, options)
                shapes = [inserted]
                result = [inserted if item is entity else item for item in entities]
            elif operation == "offset":
                distance = _number(options.get("distance"), "величина смещения")
                if abs(distance) <= POINT_EPSILON_MM:
                    raise CadDesignError("Величина смещения должна отличаться от нуля.")
                keep = _flag(options.get("keepOriginal", True), "сохранение исходного контура")
                shapes = _offset_shapes(entity, distance)
                for shape in shapes:
                    shape.pop("reference", None)
                result = _replace(entities, {entity["id"]}, shapes, keep_original=keep)
                if entity["type"] in ("spline", "ellipse") or entity.get("bulges"):
                    warnings.append("Смещённая кривая представлена полилинией.")
                if len(shapes) > 1:
                    warnings.append("Смещение дало несколько отдельных контуров.")
            elif operation == "trim":
                ids = options.get("cutterIds")
                if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
                    raise CadDesignError("Выберите границы обрезки.")
                cutters = [_selected(by_id, value) for value in ids if value != entity["id"]]
                shapes = _trim_shapes(entity, cutters, _point(options.get("point")))
                result = _replace(entities, {entity["id"]}, shapes)
                if entity["type"] not in ("line", "polyline") or entity.get("bulges"):
                    warnings.append("Обрезанная кривая представлена полилинией.")
            else:
                raise CadDesignError("Неизвестная операция с чертежом.")
    except (GEOSException, ArithmeticError) as error:
        raise CadDesignError("Не удалось выполнить операцию с этой геометрией.") from error
    response = {"entities": validate_design(result), "warnings": warnings,
                "selectedIds": [shape["id"] for shape in shapes]}
    if operation in ("trim-quick", "repair-part", "repair-conflict", "repair-conflicts", "simplify"):
        response["changed"] = changed
    if diagnostics:
        response["diagnostics"] = diagnostics
    if diagnostic_ids:
        response["diagnosticIds"] = diagnostic_ids
    return response
