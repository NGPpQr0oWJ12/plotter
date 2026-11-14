"""Import visible 2D CAD geometry into millimetre toolpaths."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from itertools import pairwise
import math
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import ezdxf
from ezdxf import path, units
from ezdxf.math import Matrix44, Vec2, intersection_line_line_2d


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_CONVERTED_BYTES = 75 * 1024 * 1024
MAX_ENTITIES = 50_000
MAX_POINTS = 250_000
MAX_ABS_COORDINATE_MM = 1_000_000_000
CURVE_TOLERANCE_MM = 0.05
POINT_EPSILON_MM = 0.000001
LOOP_ENDPOINT_TOLERANCE_MM = 0.001
MANUAL_CLOSURE_TOLERANCE_MM = 0.02
PLANAR_EPSILON_MM = 0.001
VECTOR_TYPES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE"}


class CadImportError(ValueError):
    """The selected CAD drawing could not be imported."""


@dataclass
class ContourRepair:
    paths: list
    sources: list
    excluded: list
    closed_count: int = 0
    small_gap_count: int = 0


def _has_enclosed_area(points):
    # Translate before the shoelace sum so large DXF offsets don't erase small areas.
    x0, y0 = points[0]
    vertices = [(x - x0, y - y0) for x, y in points]
    area2 = math.fsum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairwise(vertices))
    return abs(area2) > POINT_EPSILON_MM ** 2


def _split_at_own_endpoints(points):
    """Expose the junction when a slit ends inside one of its outline's segments."""
    if len(points) < 4:
        return points
    first, last = points[0], points[-1]
    split = [first]
    for start, end in pairwise(points):
        dx, dy = end[0] - start[0], end[1] - start[1]
        joints = []
        for x, y in (first, last):
            vx, vy = x - start[0], y - start[1]
            position = vx * dx + vy * dy
            if 0 < position < dx * dx + dy * dy and vx * dy == vy * dx:
                joints.append((position, [x, y]))
        split.extend(point for _, point in sorted(joints))
        split.append(end)
    return split if len(split) > len(points) else points


def _unambiguous_loops(paths, indices):
    adjacency = defaultdict(set)
    segments = defaultdict(list)
    for index in indices:
        vertices = map(tuple, paths[index]["points"])
        for offset, (first, last) in enumerate(pairwise(vertices)):
            if first == last:
                continue
            adjacency[first].add(last)
            adjacency[last].add(first)
            segments[min(first, last), max(first, last)].append((index, offset))

    # Strip open branches from the topology only. Their segments stay in the job.
    ends = deque(vertex for vertex, neighbors in adjacency.items() if len(neighbors) == 1)
    while ends:
        vertex = ends.popleft()
        neighbors = adjacency[vertex]
        if len(neighbors) != 1:
            continue
        neighbor = neighbors.pop()
        adjacency[neighbor].remove(vertex)
        if len(adjacency[neighbor]) == 1:
            ends.append(neighbor)

    seen = set()
    for start in adjacency:
        if start in seen or len(adjacency[start]) != 2:
            continue
        loop, previous, current = [], None, start
        while current not in seen and len(adjacency[current]) == 2:
            loop.append(current)
            seen.add(current)
            other = min(neighbor for neighbor in adjacency[current] if neighbor != previous)
            previous, current = current, other
        if current != start or len(loop) < 3 or not _has_enclosed_area(loop):
            continue
        edges = [segments[min(first, last), max(first, last)]
                 for first, last in pairwise([*loop, start])]
        if any(len(references) != 1 for references in edges):
            continue
        references = [edge[0] for edge in edges]
        index, offset = min(references)
        first, last = map(tuple, paths[index]["points"][offset:offset + 2])
        if loop[(loop.index(first) + 1) % len(loop)] != last:
            loop.reverse()
        head = loop.index(first)
        yield loop[head:] + loop[:head], references


def _closing_edge_is_clear(points):
    origin = Vec2(points[0])
    end = Vec2(points[-1]) - origin
    bridge = (Vec2(), end)
    tolerance = POINT_EPSILON_MM * end.magnitude
    length_squared = end.dot(end)
    for offset, (start, finish) in enumerate(pairwise(points)):
        a, b = Vec2(start) - origin, Vec2(finish) - origin
        if abs(end.det(a)) <= tolerance and abs(end.det(b)) <= tolerance:
            low, high = sorted((end.dot(a), end.dot(b)))
            if high > tolerance and low < length_squared - tolerance:
                return False
        elif 0 < offset < len(points) - 2:
            if intersection_line_line_2d(bridge, (a, b), virtual=False) is not None:
                return False
    return True


def repair_closed_contours(paths, excluded=(), *, bridge_small_gaps=False):
    """Recover closed cut outlines and keep all original open branches."""
    excluded_set = set(excluded)
    groups = defaultdict(list)
    replacement = {}
    consumed = defaultdict(set)
    small_gaps = []
    closed_count = 0
    prepared = list(paths)
    for index, path in enumerate(paths):
        if index in excluded_set or path.get("closed", False):
            continue
        points = path["points"]
        gap = math.dist(points[0], points[-1])
        if (len(points) >= 4 and gap <= MANUAL_CLOSURE_TOLERANCE_MM
                and len(set(map(tuple, points[:-1]))) >= 3 and _has_enclosed_area(points[:-1])):
            if gap <= LOOP_ENDPOINT_TOLERANCE_MM:
                vertices = points[:-1] if gap == 0 else points
                replacement[index] = [{**path, "points": vertices, "closed": True}]
                closed_count += 1
                continue
            small_gaps.append(index)
        groups[path.get("layer", "0"), path.get("partNumber")].append(index)
        vertices = _split_at_own_endpoints(points)
        if vertices is not points:
            prepared[index] = {**path, "points": vertices}

    loops = defaultdict(list)
    for indices in groups.values():
        for points, references in _unambiguous_loops(prepared, indices):
            members = sorted({index for index, _ in references})
            # Keep the perimeter after its last source fragment and any retained notch there.
            outline = {**paths[members[0]], "points": [list(point) for point in points], "closed": True}
            for key in ("partRotation", "partAngle"):
                value = next((paths[index][key] for index in members if key in paths[index]), None)
                if value is not None:
                    outline[key] = value
            loops[members[-1]].append((outline, members))
            for index, offset in references:
                consumed[index].add(offset)
            closed_count += 1

    # Exact loops take priority over adding a short edge to an open polyline.
    small_gap_count = 0
    for index in small_gaps:
        if index in consumed or not _closing_edge_is_clear(paths[index]["points"]):
            continue
        small_gap_count += 1
        if bridge_small_gaps:
            replacement[index] = [{**paths[index], "closed": True}]
            closed_count += 1

    if not closed_count:
        return ContourRepair(paths, [[index] for index in range(len(paths))], list(excluded),
                             small_gap_count=small_gap_count)

    result, sources, result_excluded = [], [], []
    for index, path in enumerate(paths):
        if index in replacement:
            remaining = replacement[index]
        elif index in consumed:
            # Reconstruct untouched runs in their original direction, including attached notches.
            remaining = []
            points = prepared[index]["points"]
            start = 0
            for offset in sorted(consumed[index]):
                if start < offset:
                    remaining.append({**path, "points": points[start:offset + 1]})
                start = offset + 1
            if start < len(points) - 1:
                remaining.append({**path, "points": points[start:]})
        else:
            remaining = [path]
        if index in excluded_set:
            result_excluded.append(len(result))
        result.extend(remaining)
        sources.extend([[index] for _ in remaining])
        for outline, members in loops.get(index, ()):
            result.append(outline)
            sources.append(members)

    if len(result) > MAX_ENTITIES or sum(len(path["points"]) for path in result) > MAX_POINTS:
        return ContourRepair(paths, [[index] for index in range(len(paths))], list(excluded))
    return ContourRepair(result, sources, result_excluded, closed_count, small_gap_count)


def _dwg_converter() -> Path | None:
    configured = os.environ.get("PLOTTER_DWG2DXF")
    if configured:
        executable = Path(configured)
        if executable.is_absolute() and executable.is_file():
            return executable
        return None

    name = "dwg2dxf.exe" if os.name == "nt" else "dwg2dxf"
    bundled = Path(__file__).resolve().parent / ".tools" / "libredwg" / name
    if bundled.is_file():
        return bundled
    located = shutil.which(name)
    return Path(located).resolve() if located else None


def dwg_available() -> bool:
    return _dwg_converter() is not None


def _convert_dwg(source: Path) -> Path:
    executable = _dwg_converter()
    if executable is None:
        raise CadImportError("Для DWG требуется локальный конвертер LibreDWG (dwg2dxf).")

    converted = source.with_suffix(".dxf")
    try:
        result = subprocess.run(
            [str(executable), "-o", converted.name, source.name],
            cwd=source.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CadImportError("Преобразование DWG заняло больше 45 секунд.") from error
    except OSError as error:
        raise CadImportError("Не удалось запустить локальный конвертер LibreDWG.") from error

    if result.returncode or not converted.is_file():
        raise CadImportError("LibreDWG не смог преобразовать файл. Проверьте DWG или сохраните его в DXF.")
    if converted.stat().st_size > MAX_CONVERTED_BYTES:
        raise CadImportError("Преобразованный чертёж превышает 75 МБ.")
    return converted


def _read_document(data: bytes, extension: str):
    if extension == ".dwg" and not data.startswith(b"AC10"):
        raise CadImportError("У файла отсутствует заголовок DWG.")

    with TemporaryDirectory(prefix="plotter-cad-") as directory:
        source = Path(directory) / f"drawing{extension}"
        source.write_bytes(data)
        if extension == ".dwg":
            source = _convert_dwg(source)
        try:
            return ezdxf.readfile(source)
        except (ezdxf.DXFError, OSError, ValueError, IndexError) as error:
            raise CadImportError("Не удалось прочитать структуру чертежа. Проверьте файл в CAD-редакторе.") from error


class _DrawingGeometry:
    def __init__(self, document):
        self.hidden_layers = {
            layer.dxf.name.casefold()
            for layer in document.layers
            if layer.is_off() or layer.is_frozen()
        }
        self.skipped = Counter()
        self.entity_count = 0
        self.point_count = 0

    def paths(self, layout, transform, inherited_layer="0", ancestry=()):
        for entity in layout:
            self._count_entities()

            entity_type = entity.dxftype()
            if entity_type != "INSERT" and entity_type not in VECTOR_TYPES:
                self.skipped[entity_type] += 1
                continue
            layer = entity.dxf.get("layer", "0")
            if layer == "0":
                layer = inherited_layer
            if layer.casefold() in self.hidden_layers or entity.dxf.get("invisible", 0):
                self.skipped["скрытые объекты"] += 1
                continue

            if entity_type == "INSERT":
                yield from self._block_paths(entity, transform, layer, ancestry)
            else:
                shape = self._vector_path(entity, transform, layer)
                if shape is not None:
                    yield shape

    def _count_entities(self, count=1):
        self.entity_count += count
        if self.entity_count > MAX_ENTITIES:
            raise CadImportError("В чертеже более 50 000 объектов с учётом вставок блоков.")

    def _block_paths(self, entity, transform, layer, ancestry):
        block = entity.block()
        if block is None or block.block.is_xref or block.block.is_xref_overlay:
            self.skipped["недоступные и внешние блоки"] += 1
            return

        name = block.name.casefold()
        if name in ancestry or len(ancestry) >= 32:
            raise CadImportError("Обнаружена циклическая или слишком глубокая вложенность блоков.")
        insert_count = max(1, entity.mcount)
        self._count_entities(insert_count - 1)
        inserts = entity.multi_insert() if insert_count > 1 else (entity,)
        next_ancestry = (*ancestry, name)
        for insert in inserts:
            yield from self.paths(block, insert.matrix44() @ transform, layer, next_ancestry)

    def _vector_path(self, entity, transform, layer):
        entity_type = entity.dxftype()
        if entity_type == "POLYLINE" and not (entity.is_2d_polyline or entity.is_3d_polyline):
            self.skipped["сетки POLYLINE"] += 1
            return None

        try:
            vector = path.make_path(entity, segments=12, level=5).transform(transform)
        except (ezdxf.DXFError, ArithmeticError, ValueError, IndexError, TypeError) as error:
            raise CadImportError(f"Объект {entity_type} содержит недопустимую геометрию.") from error

        controls = vector.control_vertices()
        if not controls:
            self.skipped["вырожденные контуры"] += 1
            return None
        if any(not math.isfinite(value) or abs(value) > MAX_ABS_COORDINATE_MM for point in controls for value in point):
            raise CadImportError("Координаты чертежа отсутствуют, бесконечны или выходят за допустимый диапазон.")
        heights = [point.z for point in controls]
        if max(heights) - min(heights) > PLANAR_EPSILON_MM:
            self.skipped["контуры вне плоскости XY"] += 1
            return None

        points = []
        for point in vector.flattening(distance=CURVE_TOLERANCE_MM, segments=4):
            self.point_count += 1
            if self.point_count > MAX_POINTS:
                raise CadImportError("После построения кривых в чертеже более 250 000 точек.")
            xy = [round(point.x, 6), round(point.y, 6)]
            if not points or math.dist(points[-1], xy) > POINT_EPSILON_MM:
                points.append(xy)

        closed = len(points) > 2 and math.dist(points[0], points[-1]) <= POINT_EPSILON_MM
        if closed:
            points.pop()
        if len(points) < (3 if closed else 2):
            self.skipped["вырожденные контуры"] += 1
            return None
        return {"points": points, "closed": closed, "layer": layer}


def import_drawing(data: bytes, filename: str) -> dict:
    """Read DXF or DWG and return contour vertices in modelspace millimetres."""
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
    drawing_units = document.units
    if not drawing_units:
        factor = 1.0
        warnings.append("В чертеже не заданы единицы. Одна единица принята за 1 мм.")
    else:
        try:
            factor = units.conversion_factor(drawing_units, units.MM)
        except (ValueError, IndexError, ZeroDivisionError) as error:
            raise CadImportError("Неизвестные единицы чертежа. Задайте единицы в CAD-редакторе.") from error

    geometry = _DrawingGeometry(document)
    paths = list(geometry.paths(document.modelspace(), Matrix44.scale(factor)))
    if not paths:
        ignored = ", ".join(sorted(geometry.skipped))
        detail = f" Пропущены объекты ({ignored})." if ignored else ""
        raise CadImportError(f"В пространстве модели нет доступных двумерных контуров.{detail}")

    if geometry.skipped:
        ignored = ", ".join(f"{name} ({count})" for name, count in sorted(geometry.skipped.items()))
        warnings.append(f"Пропущены объекты {ignored}.")
    repaired = repair_closed_contours(paths)
    return {"filename": filename, "paths": repaired.paths, "warnings": warnings, "units": "mm",
            "closedCount": repaired.closed_count}
