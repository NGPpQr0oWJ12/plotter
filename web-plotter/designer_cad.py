"""Editable model-space CAD entities and their derived cutting geometry."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
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
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box
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


MAX_NOTCH_SERIES = 100
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

    def _count(self, count=1):
        self.visited += count
        if self.visited > MAX_ENTITIES:
            raise CadDesignError("В чертеже более 50 000 объектов.")

    def shapes(self, layout, transform, inherited_layer="0", ancestry=(), inherited_issues=()):
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
                    yield from self.shapes(block, matrix, layer, (*ancestry, name), issues)
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
    return {"filename": filename, "units": "mm", "entities": entities, "warnings": warnings}


def _curve_points(center, u, v, start, sweep, tolerance=CURVE_TOLERANCE_MM):
    # The larger ellipse axis also bounds the second derivative in parameter space.
    radius = max(math.hypot(*u), math.hypot(*v))
    angle_step = min(math.pi / 4, math.sqrt(8 * tolerance / radius))
    count = math.ceil(abs(math.radians(sweep)) / angle_step)
    if count > MAX_POINTS:
        raise CadDesignError("Кривая требует слишком много точек для резки.")
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
            if entity.get("fitMethod") == "centripetal":
                previous, middle, following = (math.sqrt((q - p).magnitude) for p, q in ((a, b), (b, c), (c, d)))
                start_tangent = (c - b if previous == 0 else
                                 ((b - a) / previous - (c - a) / (previous + middle) + (c - b) / middle) * middle)
                end_tangent = (c - b if following == 0 else
                               ((c - b) / middle - (d - b) / (middle + following) + (d - c) / following) * middle)
            else:
                start_tangent, end_tangent = (c - a) / 2, (d - b) / 2
            quadratic = (c - b) * 3 - start_tangent * 2 - end_tangent
            cubic = (b - c) * 2 + start_tangent + end_tangent

            def evaluate(t):
                return b + (start_tangent + (quadratic + cubic * t) * t) * t

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
    from nesting import annotate_notches, identify_parts

    entities = validate_design(entities)
    references = {entity["id"] for entity in entities if entity.get("reference")}
    paths = []
    cut_entities = []
    point_count = 0
    for entity in entities:
        if entity["id"] in references or entity.get("parentId") in references:
            continue
        path = _path(entity, MAX_POINTS - point_count)
        point_count += len(path["points"])
        paths.append(path)
        cut_entities.append(entity)
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
        if abs(bulge) < 1e-12:
            yield _QuickTrimEdge(start, end)
        else:
            center, first, last, radius = bulge_to_arc(start, end, bulge)
            yield _QuickTrimEdge(start, end, center, radius, first if bulge > 0 else last, 4 * math.atan(bulge))


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
        for edge in edges:
            self.offsets.append(self.length)
            self.length += edge.length

    def partitions(self, positions):
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
            return _new_shape(self.entity, "polyline", points=points, bulges=[*bulges, 0], closed=False)
        return _shape_from_points(self.entity, points)


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

    edges, owners, bounds = [], [], []
    for entity in entities:
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
    strokes = [_QuickTrimEdge(start, end) for start, end in pairwise(stroke)
               if math.dist(start, end) > POINT_EPSILON_MM]
    if strokes:
        for segment in strokes:
            hits = defaultdict(list)
            xmin, ymin, xmax, ymax = segment.geometry.bounds
            area = box(xmin - tolerance, ymin - tolerance, xmax + tolerance, ymax + tolerance)
            for index in nearby(area):
                contour, offset = owners[index]
                edge = edges[index]
                crossings = list(_quick_trim_crossings(edge, segment))
                if crossings:
                    hits[contour].extend((offset + position, 0) for position in crossings)
                    continue
                position, distance = edge.nearest_to(segment)
                if distance <= tolerance:
                    hits[contour].append((offset + position, distance))
            for contour, contacts in hits.items():
                # Keep distinct crossings, but a wide eraser may graze several
                # adjacent intervals while following one edge of a contour.
                exact = [contact for contact in contacts if contact[1] <= POINT_EPSILON_MM]
                targets[contour].extend(exact or [min(contacts, key=lambda contact: contact[1])])
    else:
        x, y = stroke[0]
        for index in nearby(box(x - tolerance, y - tolerance, x + tolerance, y + tolerance)):
            contour, offset = owners[index]
            position, distance = edges[index].project(stroke[0])
            if distance <= tolerance:
                targets[contour].append((offset + position, distance))

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
                            raise CadDesignError("В выбранных линиях есть развилка. Выберите ветви одного контура.")
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


def _repair_outline(entity):
    shape = dict(entity)
    changed = False
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

    path = _path(shape)
    line = _line_geometry(path)
    if not line.is_simple:
        raise CadDesignError("Контур самопересекается или имеет наложенные рёбра. Исправьте пересечения вручную.")
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
    return shape, changed


def _repair_selected(entities, by_id, options):
    ids = options.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTITIES:
        raise CadDesignError("Выберите контур или несколько фрагментов одной детали.")
    selected = [_selected(by_id, value) for value in ids]
    if len(set(ids)) != len(ids):
        raise CadDesignError("Выбранные объекты повторяются.")

    sources = [selected[0]]
    if len(selected) > 1:
        if any(_closed(entity) for entity in selected) or len({entity["layer"] for entity in selected}) > 1:
            raise CadDesignError("Для соединения выберите открытые фрагменты одной детали на одном слое.")
        if any(entity["type"] in ("spline", "ellipse") for entity in selected):
            raise CadDesignError("Сплайн или эллипс нельзя соединить этим методом без изменения формы. Соедините вручную.")
        sources = selected
        try:
            contour = _join_shapes(selected, MANUAL_CLOSURE_TOLERANCE_MM)
        except CadDesignError as error:
            return entities, selected, [str(error)], False
    else:
        contour = selected[0]

    try:
        repaired, changed = _repair_outline(contour)
    except CadDesignError as error:
        return entities, selected, [str(error)], False
    repaired["id"] = sources[0]["id"]
    unresolved = list(dict.fromkeys(issue for entity in sources for issue in
                       entity.get("importIssues", [LEGACY_IMPORT_ISSUE] if entity.get("importDamaged") else [])
                       if issue != IMPORT_DUPLICATE_POINTS))
    if unresolved:
        repaired.update(importDamaged=True, importIssues=unresolved[:12])
    else:
        repaired.pop("importDamaged", None)
        repaired.pop("importIssues", None)
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
    return result, [repaired], [message], True


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
            result, shapes, warnings, changed = _repair_selected(entities, by_id, options)
        elif operation == "scale":
            result, shapes, warnings = _scale_design(entities, by_id, options)
        elif operation in ("notch", "notch-series", "notch-edit"):
            result, shapes = _edit_notches(entities, by_id, operation, options)
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
    if operation in ("trim-quick", "repair-part"):
        response["changed"] = changed
    return response
