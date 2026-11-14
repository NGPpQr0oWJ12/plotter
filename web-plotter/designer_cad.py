"""Editable model-space CAD entities and their derived cutting geometry."""

from collections import Counter, defaultdict
from io import BytesIO, TextIOWrapper
from itertools import pairwise
import math
from pathlib import Path
from uuid import uuid4

import ezdxf
from ezdxf import units
from ezdxf.lldxf.tagger import binary_tags_loader
from ezdxf.lldxf.validator import dxf_info
from ezdxf.math import BSpline, Matrix44, Vec3, arc_angle_span_deg, bulge_to_arc, open_uniform_knot_vector
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring

from cad import (
    CadImportError, CURVE_TOLERANCE_MM, MANUAL_CLOSURE_TOLERANCE_MM,
    MAX_ABS_COORDINATE_MM, MAX_ENTITIES, MAX_FILE_BYTES, MAX_POINTS,
    PLANAR_EPSILON_MM, POINT_EPSILON_MM, VECTOR_TYPES, _read_document,
    repair_closed_contours,
)


class CadDesignError(ValueError):
    """The drawing or requested edit cannot be represented in the designer."""


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


def _entity(entity):
    if not isinstance(entity, dict):
        raise CadDesignError("Неверный объект чертежа.")
    kind = entity.get("type")
    clean = {"id": _text(entity.get("id"), "идентификатор объекта", 128),
             "type": kind, "layer": _text(entity.get("layer", "0"), "слой", 255)}
    for flag in ("reference", "notch"):
        if flag in entity:
            clean[flag] = _flag(entity[flag], flag)
    if "parentId" in entity:
        if not clean.get("notch"):
            raise CadDesignError("Связь с контуром должна принадлежать насечке.")
        clean["parentId"] = _text(entity["parentId"], "контур насечки", 128)

    count = 1
    if kind == "line":
        start, end = _point(entity.get("start")), _point(entity.get("end"))
        if math.dist(start, end) <= POINT_EPSILON_MM:
            raise CadDesignError("Длина линии слишком мала.")
        clean.update(start=start, end=end)
        count = 2
    elif kind == "polyline":
        closed = _flag(entity.get("closed", False), "замыкание полилинии")
        points = _points(entity.get("points"), 2)
        clean.update(points=points, closed=closed)
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

    points = _planar(transform.transform_vertices(vertices))
    if any(bulges):
        ocs = entity.ocs()
        u, v = transform.transform_directions((ocs.ux, ocs.uy))
        if not _similar_axes(u, v):
            segments = list(entity.virtual_entities())
            converted = [shape for segment in segments for shape in _native_shapes(segment, transform, skipped)]
            skipped["полилинии под неравномерным масштабом разложены на линии и эллипсы"] += 1
            yield from converted
            return
        if u.cross(v).z < 0:
            bulges = [-bulge for bulge in bulges]
        yield {"type": "polyline", "points": points, "closed": closed, "bulges": bulges}
    else:
        yield {"type": "polyline", "points": points, "closed": closed}


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
    def __init__(self, document):
        self.hidden_layers = {layer.dxf.name.casefold() for layer in document.layers
                              if layer.is_off() or layer.is_frozen()}
        self.skipped = Counter()
        self.visited = 0

    def _count(self, count=1):
        self.visited += count
        if self.visited > MAX_ENTITIES:
            raise CadDesignError("В чертеже более 50 000 объектов.")

    def shapes(self, layout, transform, inherited_layer="0", ancestry=()):
        for entity in layout:
            self._count()
            kind = entity.dxftype()
            if kind != "INSERT" and kind not in VECTOR_TYPES:
                self.skipped[kind] += 1
                continue
            layer = entity.dxf.get("layer", "0")
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
                    raise CadDesignError("Обнаружена циклическая или слишком глубокая вложенность блоков.")
                inserts = entity.multi_insert() if entity.mcount > 1 else (entity,)
                self._count(max(0, entity.mcount - 1))
                for insert in inserts:
                    yield from self.shapes(block, insert.matrix44() @ transform, layer, (*ancestry, name))
                continue
            try:
                for shape in _native_shapes(entity, transform, self.skipped):
                    yield {**shape, "layer": layer}
            except _NonPlanar:
                self.skipped["объекты вне плоскости XY"] += 1
            except (ezdxf.DXFError, ArithmeticError, ValueError, IndexError, TypeError) as error:
                raise CadDesignError(f"Объект {kind} содержит недопустимую геометрию.") from error


def _dxf_units(data):
    # ezdxf synthesizes a metre-based R12 header for DXF streams without one.
    if not data.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
        with TextIOWrapper(BytesIO(data), encoding="latin-1") as stream:
            return dxf_info(stream).insert_units
    tags = iter(binary_tags_loader(data))
    if next(tags, None) == (0, "SECTION") and next(tags, None) == (2, "HEADER"):
        for tag in tags:
            if tag == (0, "ENDSEC"):
                break
            if tag == (9, "$INSUNITS"):
                return next(tags).value
    return 0


def import_design(data, filename):
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(filename).suffix.lower()
    if extension not in {".dxf", ".dwg"}:
        raise CadImportError("Выберите файл DXF или DWG.")
    if not data:
        raise CadImportError("Выбранный файл пуст.")
    if len(data) > MAX_FILE_BYTES:
        raise CadImportError("Размер файла превышает 25 МБ.")
    document = _read_document(data, extension)
    warnings = []
    factor = 1
    try:
        drawing_units = _dxf_units(data) if extension == ".dxf" else document.units
        if drawing_units:
            factor = units.conversion_factor(drawing_units, units.MM)
    except (ezdxf.DXFError, TypeError, ValueError, IndexError, ZeroDivisionError, StopIteration) as error:
        raise CadImportError("Неизвестные единицы чертежа. Задайте единицы в CAD-редакторе.") from error
    if not drawing_units:
        warnings.append("В чертеже не заданы единицы. Одна единица принята за 1 мм.")
    drawing = _NativeDrawing(document)
    entities = []
    point_count = 0
    for index, shape in enumerate(drawing.shapes(document.modelspace(), Matrix44.scale(factor)), start=1):
        entity, count = _entity({**shape, "id": f"dxf-{index}"})
        point_count += count
        if index > MAX_ENTITIES or point_count > MAX_POINTS:
            raise CadDesignError("В чертеже слишком много объектов или опорных точек.")
        entities.append(entity)
    if drawing.skipped:
        summary = ", ".join(f"{name} ({count})" for name, count in sorted(drawing.skipped.items()))
        warnings.append(f"Особенности импорта: {summary}.")
    if not entities:
        detail = f" {' '.join(warnings)}" if warnings else ""
        raise CadDesignError(f"В пространстве модели нет доступной двумерной геометрии.{detail}")
    return {"filename": filename, "units": "mm", "entities": entities, "warnings": warnings}


def _curve_points(center, u, v, start, sweep):
    # The larger ellipse axis also bounds the second derivative in parameter space.
    radius = max(math.hypot(*u), math.hypot(*v))
    angle_step = min(math.pi / 4, math.sqrt(8 * CURVE_TOLERANCE_MM / radius))
    count = math.ceil(abs(math.radians(sweep)) / angle_step)
    if count > MAX_POINTS:
        raise CadDesignError("Кривая требует слишком много точек для резки.")
    for index in range(count + 1):
        angle = math.radians(start + sweep * index / count)
        yield [value + first * math.cos(angle) + second * math.sin(angle)
               for value, first, second in zip(center, u, v)]


def _bulge_points(start, end, bulge):
    if abs(bulge) < 1e-12:
        yield end
        return
    center, first_angle, last_angle, radius = bulge_to_arc(start, end, bulge)
    sweep = math.degrees(4 * math.atan(bulge))
    angle = math.degrees(first_angle if bulge > 0 else last_angle)
    points = iter(_curve_points(center, (radius, 0), (0, radius), angle, sweep))
    next(points)
    previous = next(points)
    for point in points:
        yield previous
        previous = point
    yield end


def _span_points(evaluate, left, right, a, b, depth=0):
    chord = Vec3(b) - Vec3(a)
    squared = chord.magnitude_square
    samples = [Vec3(evaluate(left + (right - left) * ratio)) for ratio in (0.25, 0.5, 0.75)]
    for point in samples:
        delta = point - Vec3(a)
        projection = max(0, min(1, delta.dot(chord) / squared)) if squared else 0
        if (delta - chord * projection).magnitude > CURVE_TOLERANCE_MM:
            if depth == 24:
                raise CadDesignError("Не удалось построить сплайн с точностью для резки.")
            middle = (left + right) / 2
            yield from _span_points(evaluate, left, middle, a, samples[1], depth + 1)
            yield from _span_points(evaluate, middle, right, samples[1], b, depth + 1)
            return
    yield b


def _spline_points(entity):
    if "fitPoints" in entity:
        points, closed = entity["fitPoints"], entity["closed"]
        size = len(points)
        yield points[0]
        for index in range(size if closed else size - 1):
            neighbors = [point % size if closed else max(0, min(size - 1, point))
                         for point in range(index - 1, index + 3)]
            a, b, c, d = [Vec3(points[neighbor]) for neighbor in neighbors]
            linear = c - a
            quadratic = a * 2 - b * 5 + c * 4 - d
            cubic = -a + b * 3 - c * 3 + d

            def evaluate(t):
                return b + (linear + (quadratic + cubic * t) * t) * (t / 2)

            yield from _span_points(evaluate, 0, 1, b, c)
        return

    control_points = entity["controlPoints"]
    degree = entity["degree"]
    old_knots = entity["knots"]
    # Normalize the whole vector consistently before passing it to the evaluator.
    span = old_knots[-1] - old_knots[0]
    knots = [(knot - old_knots[0]) / span for knot in old_knots]
    spline = BSpline(control_points, order=degree + 1, knots=knots, weights=entity["weights"] or None)
    evaluate = spline.evaluator.point
    bounds = sorted(set(knots[degree:len(control_points) + 1]))
    start = evaluate(bounds[0])
    yield start
    for left, right in pairwise(bounds):
        end = evaluate(right)
        yield from _span_points(evaluate, left, right, start, end)
        start = end


def _path_points(entity):
    kind = entity["type"]
    if kind == "line":
        yield entity["start"]
        yield entity["end"]
    elif kind == "polyline":
        points = entity["points"]
        bulges = entity.get("bulges")
        yield points[0]
        for index in range(len(points) if entity["closed"] else len(points) - 1):
            start, end = points[index], points[(index + 1) % len(points)]
            if math.dist(start, end) > POINT_EPSILON_MM:
                yield from _bulge_points(start, end, bulges[index] if bulges else 0)
    elif kind == "spline":
        yield from _spline_points(entity)
    else:
        if kind == "ellipse":
            x, y = entity["axis"]
            u, v = (x, y), (-y * entity["ratio"], x * entity["ratio"])
        else:
            radius = entity["radius"]
            u, v = (radius, 0), (0, radius)
        yield from _curve_points(entity["center"], u, v, entity.get("startAngle", 0), entity.get("sweep", 360))


def _closed(entity):
    return (entity["type"] == "circle" or entity.get("closed", False)
            or entity["type"] == "ellipse" and abs(entity["sweep"]) == 360)


def _path(entity, budget=MAX_POINTS):
    points = []
    try:
        for index, raw in enumerate(_path_points(entity), start=1):
            if index > budget:
                raise CadDesignError("После построения кривых в чертеже слишком много точек.")
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


def prepare_design(entities):
    entities = validate_design(entities)
    references = {entity["id"] for entity in entities if entity.get("reference")}
    paths = []
    notches = []
    point_count = 0
    for entity in entities:
        if entity["id"] in references or entity.get("parentId") in references:
            continue
        path = _path(entity, MAX_POINTS - point_count)
        point_count += len(path["points"])
        if path.get("notch"):
            notches.append(len(paths))
        paths.append(path)
    repaired = repair_closed_contours(paths, notches)
    return {"paths": repaired.paths, "warnings": [], "closedCount": repaired.closed_count}


def _line_geometry(path):
    points = path["points"]
    return LineString([*points, points[0]] if path["closed"] else points)


def _new_shape(source, kind, **geometry):
    shape = {"id": uuid4().hex, "layer": source["layer"], "type": kind, **geometry}
    if source.get("reference"):
        shape["reference"] = True
    return shape


def _shape_from_points(source, points, closed=False):
    points = [list(point) for point in points]
    if closed and points[0] == points[-1]:
        points.pop()
    if len(points) == 2 and not closed:
        return _new_shape(source, "line", start=points[0], end=points[1])
    return _new_shape(source, "polyline", points=points, closed=closed)


def _notch(parent, point, depth):
    if parent["type"] == "circle":
        x, y = parent["center"]
        radius = parent["radius"]
        if depth > 2 * radius:
            raise CadDesignError("Насечка выходит за границу детали. Уменьшите глубину.")
        angle = math.atan2(point[1] - y, point[0] - x)
        dx, dy = math.cos(angle), math.sin(angle)
        return _new_shape(parent, "line", start=[x + radius * dx, y + radius * dy],
                          end=[x + (radius - depth) * dx, y + (radius - depth) * dy],
                          notch=True, parentId=parent["id"])
    path = _path(parent)
    line = _line_geometry(path)
    at = line.project(Point(point))
    attach = line.interpolate(at)
    width = min(line.length / 4, 0.01)
    if path["closed"]:
        before = line.interpolate((at - width) % line.length)
        after = line.interpolate((at + width) % line.length)
    else:
        before = line.interpolate(max(0, at - width))
        after = line.interpolate(min(line.length, at + width))
    dx, dy = after.x - before.x, after.y - before.y
    magnitude = math.hypot(dx, dy)
    if magnitude <= POINT_EPSILON_MM:
        raise CadDesignError("В этой точке не удалось определить направление насечки.")
    side = 1 if not path["closed"] or Polygon(line).exterior.is_ccw else -1
    normal_x, normal_y = -side * dy / magnitude, side * dx / magnitude
    if path["closed"]:
        polygon = Polygon(line)
        if not polygon.is_valid:
            raise CadDesignError("Для насечки нужен корректный замкнутый контур.")
        step = min(0.01, depth / 4)
        if not polygon.covers(Point(attach.x + step * normal_x, attach.y + step * normal_y)):
            raise CadDesignError("Выберите точку насечки на стороне контура вдали от вершины.")
        end = Point(attach.x + depth * normal_x, attach.y + depth * normal_y)
        inside = Point(attach.x + step * normal_x, attach.y + step * normal_y)
        if not polygon.covers(LineString((inside, end))):
            raise CadDesignError("Насечка выходит за границу детали. Уменьшите глубину.")
    return _new_shape(parent, "line", start=[attach.x, attach.y],
                      end=[attach.x + depth * normal_x, attach.y + depth * normal_y],
                      notch=True, parentId=parent["id"])


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


def _offset_shapes(entity, distance):
    kind = entity["type"]
    if kind in ("circle", "arc"):
        direction = 1 if kind == "circle" else -math.copysign(1, entity["sweep"])
        radius = entity["radius"] + direction * distance
        if radius <= POINT_EPSILON_MM:
            raise CadDesignError("Смещение полностью убирает контур.")
        return [{**entity, "radius": radius, "id": uuid4().hex}]

    path = _path(entity)
    line = _line_geometry(path)
    result = []
    if path["closed"]:
        polygon = Polygon(line)
        if not polygon.is_valid:
            raise CadDesignError("Смещение требует замкнутого контура без самопересечений.")
        offset = polygon.buffer(distance, join_style="mitre")
        polygons = offset.geoms if offset.geom_type == "MultiPolygon" else (offset,)
        for polygon in polygons:
            if not polygon.is_empty:
                result.append(_shape_from_points(entity, polygon.exterior.coords, closed=True))
                result.extend(_shape_from_points(entity, ring.coords, closed=True) for ring in polygon.interiors)
    else:
        offset = line.offset_curve(distance, join_style="mitre")
        lines = offset.geoms if offset.geom_type == "MultiLineString" else (offset,)
        result.extend(_shape_from_points(entity, curve.coords) for curve in lines if not curve.is_empty)
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


def _join_shapes(entities, tolerance):
    if len(entities) < 2:
        raise CadDesignError("Выберите не менее двух открытых объектов.")
    if len({entity["layer"] for entity in entities}) > 1:
        raise CadDesignError("Для объединения объекты должны лежать на одном слое.")
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
                            raise CadDesignError("В точке сходятся несколько концов. Выберите одну цепочку.")
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
        raise CadDesignError("Выбранные объекты не образуют одну непрерывную цепочку.")
    closed = current == first
    if closed:
        points.pop()
        bulges.pop()
    result = _new_shape(entities[0], "polyline", points=points, closed=closed)
    if any(bulges):
        result["bulges"] = bulges
    return result


def _selected(by_id, value):
    entity = by_id.get(_text(value, "идентификатор объекта", 128))
    if entity is None or entity.get("notch"):
        raise CadDesignError("Выберите существующий контур.")
    return entity


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


def edit_design(entities, operation, options):
    entities = validate_design(entities)
    if not isinstance(options, dict):
        raise CadDesignError("Неверные параметры операции с чертежом.")
    by_id = {entity["id"]: entity for entity in entities}
    warnings = []
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
        else:
            entity = _selected(by_id, options.get("id"))
            if operation == "offset":
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
            elif operation == "notch":
                shapes = [_notch(entity, _point(options.get("point")), _positive(options.get("depth"), "Глубина"))]
                result = [*entities, *shapes]
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
    return {"entities": validate_design(result), "warnings": warnings,
            "selectedIds": [shape["id"] for shape in shapes]}
