"""Deterministic nesting on a roll with section-aware layout selection."""

from collections import defaultdict, deque
from dataclasses import dataclass
from functools import lru_cache
from math import atan2, cos, degrees, dist, fsum, radians, sin
from random import Random

from ezdxf.math.triangulation import mapbox_earcut_2d

from cad import MAX_ABS_COORDINATE_MM, MAX_ENTITIES, MAX_POINTS, repair_closed_contours
from contour_packing import MAX_PARTS as MAX_CONTOUR_PARTS, ROUNDING_ALLOWANCE_MM, compact_contour_layout
from cut_plan import roll_page_boundaries
from machine import PlotterError, number
from rect_packing import MAX_PARTITION_PARTS, max_rects_layout, partition_layout
from xiao_job import COORDINATE_OFFSET_MM


GEOMETRY_EPSILON = 1e-7
JOINT_GRID_MM = 0.0001
MAX_NOTCH_LENGTH_MM = 10.0
NOTCH_EDGE_TOLERANCE_MM = 0.25
NOTCH_EXTENSION_MM = 0.75
MAX_GEOMETRY_TESTS = 8_000_000
SEARCH_BINS = 12
SEARCH_RECTS = 96
ANGLE_STEP_DEGREES = 5
MAX_EDGE_ANGLES = 32
MAX_ANGLE_REFINEMENTS = 12
MAX_PART_NUMBER = (1 << 53) - 1
MAX_ORDER_TRIALS = 1600
ORDER_SEARCH_BUDGET = 70_000
MAX_CONTOUR_POINTS = 128
MAX_CLEARANCE_TESTS = 200_000
MAX_CONTOUR_ROTATION_PARTS = 20


class NestingFitError(PlotterError):
    def __init__(self, message, indices, *, usable_width_mm, minimum_part_width_mm,
                 edge_allowance_mm, quarter_turn_part_width_mm, blocked_part_count):
        super().__init__(message)
        self.indices = sorted(set(indices))
        self.usable_width_mm = usable_width_mm
        self.minimum_part_width_mm = minimum_part_width_mm
        self.minimum_width_mm = minimum_part_width_mm + edge_allowance_mm
        self.quarter_turn_minimum_width_mm = quarter_turn_part_width_mm + edge_allowance_mm
        self.quarter_turn_fits = quarter_turn_part_width_mm <= usable_width_mm
        self.blocked_part_count = blocked_part_count


@dataclass(frozen=True)
class Box:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def length(self):
        return self.max_x - self.min_x

    @property
    def width(self):
        return self.max_y - self.min_y

    def overlaps(self, other):
        return (self.min_x <= other.max_x and other.min_x <= self.max_x
                and self.min_y <= other.max_y and other.min_y <= self.max_y)

    def contains(self, other):
        epsilon = GEOMETRY_EPSILON
        return (self.min_x - epsilon <= other.min_x and other.max_x <= self.max_x + epsilon
                and self.min_y - epsilon <= other.min_y and other.max_y <= self.max_y + epsilon)

    def expanded(self, distance):
        return Box(self.min_x - distance, self.min_y - distance,
                   self.max_x + distance, self.max_y + distance)


def _bounds(points):
    xs, ys = zip(*points)
    return Box(min(xs), min(ys), max(xs), max(ys))


def _union(boxes):
    return Box(min(box.min_x for box in boxes), min(box.min_y for box in boxes),
               max(box.max_x for box in boxes), max(box.max_y for box in boxes))


@dataclass(frozen=True)
class Path:
    points: tuple
    box: Box
    layer: str
    closed: bool
    part_number: int | None
    part_rotation: str | None


@dataclass(frozen=True)
class Part:
    indices: tuple
    box: Box
    number: int | None = None
    rotation: str | None = None


def _paths(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ENTITIES:
        raise PlotterError(f"Для автораскладки выберите от 1 до {MAX_ENTITIES} контуров.")
    paths = []
    point_count = 0
    for index, path in enumerate(value, 1):
        if not isinstance(path, dict):
            raise PlotterError(f"Контур {index}: неверный формат.")
        layer, closed, points = path.get("layer", "0"), path.get("closed", False), path.get("points")
        if not isinstance(layer, str) or len(layer) > 255 or type(closed) is not bool or not isinstance(points, list):
            raise PlotterError(f"Контур {index}: неверные свойства.")
        part_number, part_rotation = path.get("partNumber"), path.get("partRotation")
        if "partNumber" in path and (type(part_number) is not int or not 1 <= part_number <= MAX_PART_NUMBER):
            raise PlotterError(f"Контур {index}: неверный номер детали.")
        if "partRotation" in path and (part_number is None or part_rotation not in ("none", "any", "quarter")):
            raise PlotterError(f"Контур {index}: неверное правило поворота детали.")
        point_count += len(points)
        if point_count > MAX_POINTS:
            raise PlotterError("Слишком много точек для автораскладки.")
        if any(not isinstance(point, (list, tuple)) or len(point) != 2
               or any(type(axis) not in (int, float) or not -MAX_ABS_COORDINATE_MM <= axis <= MAX_ABS_COORDINATE_MM
                      for axis in point) for point in points):
            raise PlotterError(f"Контур {index}: неверные координаты.")
        vertices = tuple((float(x), float(y)) for x, y in points)
        if len(set(vertices)) < (3 if closed else 2):
            raise PlotterError(f"Контур {index}: недостаточно различных точек.")
        paths.append(Path(vertices, _bounds(vertices), layer, closed, part_number, part_rotation))
    return paths


class Groups:
    def __init__(self, size):
        self.parents = list(range(size))
        self.sizes = [1] * size

    def find(self, index):
        while self.parents[index] != index:
            self.parents[index] = self.parents[self.parents[index]]
            index = self.parents[index]
        return index

    def merge(self, first, second):
        first, second = self.find(first), self.find(second)
        if first == second:
            return
        if self.sizes[first] < self.sizes[second]:
            first, second = second, first
        self.parents[second] = first
        self.sizes[first] += self.sizes[second]


class BoxTree:
    """An index for contour and edge bounds. Leaves keep small sorted lists."""

    def __init__(self, entries):
        self.box = _union([box for box, _ in entries])
        self.entries = entries
        self.children = ()
        if len(entries) > 8:
            lengthwise = self.box.length >= self.box.width
            entries.sort(key=lambda entry: entry[0].min_x + entry[0].max_x if lengthwise
                         else entry[0].min_y + entry[0].max_y)
            middle = len(entries) // 2
            self.entries = ()
            self.children = (BoxTree(entries[:middle]), BoxTree(entries[middle:]))

    def search(self, target):
        if not self.box.overlaps(target):
            return
        for box, item in self.entries:
            if box.overlaps(target):
                yield item
        for child in self.children:
            yield from child.search(target)


class Budget:
    def __init__(self, limit=MAX_GEOMETRY_TESTS):
        self.remaining = limit

    def check(self):
        self.remaining -= 1
        if self.remaining < 0:
            raise PlotterError("В чертеже слишком много пересекающихся контуров. "
                               "Разделите раскладку или удалите вспомогательную геометрию.")


def _cross(first, second, point):
    return ((second[0] - first[0]) * (point[1] - first[1])
            - (second[1] - first[1]) * (point[0] - first[0]))


def _on_segment(first, second, point):
    tolerance = GEOMETRY_EPSILON * max(1, abs(second[0] - first[0]), abs(second[1] - first[1]))
    return (min(first[0], second[0]) - GEOMETRY_EPSILON <= point[0] <= max(first[0], second[0]) + GEOMETRY_EPSILON
            and min(first[1], second[1]) - GEOMETRY_EPSILON <= point[1] <= max(first[1], second[1]) + GEOMETRY_EPSILON
            and abs(_cross(first, second, point)) <= tolerance)


def _edges_cross(first, second, third, fourth):
    if (_on_segment(first, second, third) or _on_segment(first, second, fourth)
            or _on_segment(third, fourth, first) or _on_segment(third, fourth, second)):
        return True
    return (_cross(first, second, third) * _cross(first, second, fourth) < 0
            and _cross(third, fourth, first) * _cross(third, fourth, second) < 0)


def _distance_to_edge(point, first, second):
    length = dist(first, second)
    dx, dy = (second[0] - first[0]) / length, (second[1] - first[1]) / length
    x, y = point[0] - first[0], point[1] - first[1]
    projection = min(length, max(0, x * dx + y * dy))
    return dist((x, y), (projection * dx, projection * dy))


def _extended_segment(first, second):
    length = dist(first, second)
    dx = (second[0] - first[0]) / length * NOTCH_EXTENSION_MM
    dy = (second[1] - first[1]) / length * NOTCH_EXTENSION_MM
    return (first[0] - dx, first[1] - dy), (second[0] + dx, second[1] + dy)


class Boundary:
    def __init__(self, owner, points, *, closed):
        self.owner = owner
        self.points = points
        self.box = _bounds(points)
        ends = (*points[1:], points[0]) if closed else points[1:]
        self.edges = tuple((a, b) for a, b in zip(points, ends) if a != b)
        self.tree = None

    def nearby_edges(self, target):
        if self.tree is None:
            self.tree = BoxTree([(_bounds(edge), edge) for edge in self.edges])
        return self.tree.search(target)

    def notch_distance(self, segments, budget):
        closest = float("inf")
        for first, second, extended, box in segments:
            budget.check()
            for third, fourth in self.nearby_edges(box):
                budget.check()
                if _edges_cross(first, second, third, fourth):
                    return 0.0
                distance = min(_distance_to_edge(first, third, fourth), _distance_to_edge(second, third, fourth),
                               _distance_to_edge(third, first, second), _distance_to_edge(fourth, first, second))
                # Nearby edges may be displaced by curve flattening. Allow a
                # slightly larger gap when the notch points toward the edge.
                if distance <= NOTCH_EDGE_TOLERANCE_MM or _edges_cross(*extended, third, fourth):
                    closest = min(closest, distance)
        return closest


class Outline(Boundary):
    def __init__(self, owner, points):
        super().__init__(owner, points, closed=True)

    def has_point(self, point, budget):
        x, y = point
        if not self.box.contains(Box(x, y, x, y)):
            return False
        inside = False
        for first, second in self.nearby_edges(Box(x - GEOMETRY_EPSILON, y - GEOMETRY_EPSILON,
                                                    self.box.max_x, y + GEOMETRY_EPSILON)):
            budget.check()
            if _on_segment(first, second, point):
                return True
            if (first[1] > y) != (second[1] > y):
                crossing = first[0] + (second[0] - first[0]) * (y - first[1]) / (second[1] - first[1])
                if x < crossing:
                    inside = not inside
        return inside

    def touches(self, other, budget):
        if len(self.edges) > len(other.edges):
            return other.touches(self, budget)
        for first, second in self.edges:
            budget.check()
            for third, fourth in other.nearby_edges(_bounds((first, second))):
                budget.check()
                if _edges_cross(first, second, third, fourth):
                    return True
        return False


def _endpoint(point):
    return tuple(round(axis / JOINT_GRID_MM) for axis in point)


def _connect_paths(paths, groups):
    ends = {}
    by_end = defaultdict(list)
    for index, path in enumerate(paths):
        if path.closed:
            continue
        ends[index] = (_endpoint(path.points[0]), _endpoint(path.points[-1]))
        for key in ends[index]:
            if by_end[key]:
                groups.merge(index, by_end[key][0])
            by_end[key].append(index)
    for index, path in enumerate(paths):
        if path.closed:
            for point in path.points:
                attached = by_end.get(_endpoint(point))
                if attached:
                    groups.merge(index, attached[0])
    return ends, by_end


def _closed_chains(paths, ends, by_end):
    """Keep the loops of open entities after stripping any dangling marks."""
    # Coincident entities still move and cut as drawn. Count them once when
    # looking for loops so a duplicated notch still has a dangling tip.
    unique = {}
    for index in ends:
        points = paths[index].points
        unique.setdefault(min(points, points[::-1]), index)
    alive = set(unique.values())
    degree = {key: sum(edge in alive for edge in edges) for key, edges in by_end.items()}
    leaves = deque(key for key, count in degree.items() if count == 1)
    while leaves:
        key = leaves.popleft()
        if degree[key] != 1:
            continue
        edge = next(edge for edge in by_end[key] if edge in alive)
        alive.remove(edge)
        for endpoint in ends[edge]:
            degree[endpoint] -= 1
            if degree[endpoint] == 1:
                leaves.append(endpoint)

    consumed = set()
    for start in sorted(alive):
        if start in consumed:
            continue
        origin = ends[start][0]
        key, edge, points = origin, start, []
        while degree[key] == 2 and edge not in consumed:
            consumed.add(edge)
            first, second = ends[edge]
            forward = first == key
            segment = paths[edge].points
            points.extend((segment if forward else segment[::-1])[:-1])
            key = second if forward else first
            if key == origin and degree[key] == 2:
                if len(set(points)) >= 3:
                    yield Outline(start, tuple(points))
                break
            if degree[key] == 2:
                edge = next((neighbor for neighbor in by_end[key] if neighbor in alive and neighbor not in consumed),
                            edge)


def _is_short_mark(paths, members, bounds):
    limit = MAX_NOTCH_LENGTH_MM + GEOMETRY_EPSILON
    return (max(bounds.length, bounds.width) <= limit
            and all(sum(dist(first, second) for first, second in zip(paths[member].points, paths[member].points[1:]))
                    <= limit for member in members))


def _notch_owner(paths, members, boundaries, index, bounds, groups, budget):
    segments = []
    for member in members:
        points = paths[member].points
        for first, second in zip(points, points[1:]):
            if first != second:
                extended = _extended_segment(first, second)
                segments.append((first, second, extended, _bounds(extended).expanded(NOTCH_EDGE_TOLERANCE_MM)))
    distances = {}
    for position in index.search(bounds.expanded(NOTCH_EXTENSION_MM)):
        budget.check()
        boundary = boundaries[position]
        distance = boundary.notch_distance(segments, budget)
        if distance <= NOTCH_EXTENSION_MM + GEOMETRY_EPSILON:
            owner = groups.find(boundary.owner)
            distances[owner] = min(distances.get(owner, float("inf")), distance)
    if not distances:
        return None
    best = min(distances.values())
    nearest = [owner for owner, distance in distances.items() if distance <= best + GEOMETRY_EPSILON]
    return nearest[0] if len(nearest) == 1 else None


def _containing_outline(paths, members, outlines, index, bounds, budget):
    containers = []
    for position in index.search(bounds):
        budget.check()
        outline = outlines[position]
        if outline.box.contains(bounds):
            containers.append(outline)
    for outline in sorted(containers, key=lambda item: (item.box.length * item.box.width, item.owner)):
        if all(outline.has_point(point, budget) for member in members for point in paths[member].points):
            return outline.owner
    return None


def _parts(paths):
    groups = Groups(len(paths))
    ends, by_end = _connect_paths(paths, groups)
    outlines = [Outline(index, path.points) for index, path in enumerate(paths) if path.closed]
    outlines.extend(_closed_chains(paths, ends, by_end))
    budget = Budget()
    if outlines:
        index = BoxTree([(outline.box, position) for position, outline in enumerate(outlines)])
        for position, outline in enumerate(outlines):
            for other_position in index.search(outline.box):
                budget.check()
                if other_position <= position:
                    continue
                other = outlines[other_position]
                if groups.find(outline.owner) == groups.find(other.owner):
                    continue
                if ((outline.box.contains(other.box) and outline.has_point(other.points[0], budget))
                        or (other.box.contains(outline.box) and other.has_point(outline.points[0], budget))
                        or outline.touches(other, budget)):
                    groups.merge(outline.owner, other.owner)
    outlined = {groups.find(outline.owner) for outline in outlines}
    open_groups = defaultdict(list)
    for path_index in ends:
        root = groups.find(path_index)
        if root not in outlined:
            open_groups[root].append(path_index)
    marks = []
    boundaries = list(outlines)
    for root, members in open_groups.items():
        bounds = _union([paths[member].box for member in members])
        if outlines:
            owner = _containing_outline(paths, members, outlines, index, bounds, budget)
            if owner is not None:
                groups.merge(root, owner)
                continue
        if _is_short_mark(paths, members, bounds):
            marks.append((root, members, bounds))
        else:
            boundaries.extend(Boundary(member, paths[member].points, closed=False) for member in members)
    if marks and boundaries:
        boundary_index = BoxTree([(boundary.box, position) for position, boundary in enumerate(boundaries)])
        for root, members, bounds in marks:
            owner = _notch_owner(paths, members, boundaries, boundary_index, bounds, groups, budget)
            if owner is not None:
                groups.merge(root, owner)
    by_part = defaultdict(list)
    for index in range(len(paths)):
        by_part[groups.find(index)].append(index)
    return [Part(tuple(members), _union([paths[index].box for index in members])) for members in by_part.values()]


def _identified_parts(paths):
    numbered = defaultdict(list)
    unnumbered = []
    for index, path in enumerate(paths):
        if path.part_number is None:
            unnumbered.append(index)
        else:
            numbered[path.part_number].append(index)
    parts = []
    for number, indices in numbered.items():
        rotations = {paths[index].part_rotation for index in indices if paths[index].part_rotation is not None}
        if "none" in rotations and len(rotations) > 1:
            raise PlotterError(f"Деталь {number}: правила поворота контуров различаются.")
        parts.append(Part(tuple(indices), _union([paths[index].box for index in indices]),
                          number, next((mode for mode in ("any", "quarter", "none") if mode in rotations), None)))
    next_number = 1
    for part in _parts([paths[index] for index in unnumbered]):
        while next_number in numbered:
            next_number += 1
        parts.append(Part(tuple(unnumbered[index] for index in part.indices), part.box, next_number))
        next_number += 1
    return sorted(parts, key=lambda part: part.indices[0])


def _hull(points):
    ordered = sorted(set(points))
    if len(ordered) < 3:
        return ordered

    def half(sequence):
        result = []
        for point in sequence:
            while len(result) > 1 and _cross(result[-2], result[-1], point) <= 0:
                result.pop()
            result.append(point)
        return result[:-1]

    return half(ordered) + half(reversed(ordered))


def _rotation_components(angle):
    if angle == 0:
        return 1, 0
    if angle == 90:
        return 0, 1
    if angle >= 180:
        cosine, sine = _rotation_components(angle - 180)
        return -cosine, -sine
    radians_angle = radians(angle)
    return cos(radians_angle), sin(radians_angle)


def _project(points, cosine, sine):
    return ((x * cosine - y * sine, x * sine + y * cosine) for x, y in points)


def _part_hull(part, paths):
    origin_x, origin_y = part.box.min_x, part.box.min_y
    return _hull((x - origin_x, y - origin_y) for member in part.indices for x, y in paths[member].points)


def _area_twice(points):
    return abs(fsum(_cross(points[0], first, last) for first, last in zip(points[1:], points[2:])))


def _simple_outline(outline, budget):
    edges = outline.edges
    # Collinear edges can extend a straight side, but cannot backtrack over it.
    for (first, middle), (_, last) in zip(edges, (*edges[1:], edges[0])):
        if _on_segment(first, middle, last) or _on_segment(middle, last, first):
            return False
    tree = BoxTree([(_bounds(edge), index) for index, edge in enumerate(edges)])
    for index, edge in enumerate(edges):
        for other in tree.search(_bounds(edge)):
            budget.check()
            if other > index + 1 and (index != 0 or other != len(edges) - 1):
                if _edges_cross(*edge, *edges[other]):
                    return False
    return True


def _concave_footprint(part, paths, hull, budget):
    members = [paths[index] for index in part.indices]
    if len(hull) < 3 or sum(len(path.points) for path in members) > MAX_CONTOUR_POINTS:
        return None
    origin_x, origin_y = part.box.min_x, part.box.min_y
    geometry = [{"layer": path.layer, "closed": path.closed,
                 "points": [(x - origin_x, y - origin_y) for x, y in path.points]} for path in members]
    repaired = repair_closed_contours(geometry)
    outlines = [path["points"] for path in repaired.paths if path["closed"]]
    if not outlines:
        return None
    points = tuple(map(tuple, max(outlines, key=_area_twice)))
    area = _area_twice(points)
    if area <= GEOMETRY_EPSILON or area >= _area_twice(hull) - GEOMETRY_EPSILON:
        return None
    outline = Outline(0, points)
    if not _simple_outline(outline, budget):
        return None

    edges = {frozenset(edge) for edge in outline.edges}
    for path in geometry:
        boundary = Boundary(0, path["points"], closed=path["closed"])
        for edge in boundary.edges:
            if frozenset(edge) in edges:
                continue
            # All cuts, including holes and marks, must stay in the occupied outline.
            if not outline.has_point(edge[0], budget) or any(
                    _edges_cross(*edge, *side) for side in outline.nearby_edges(_bounds(edge))):
                return None

    triangles = tuple(tuple(_hull((point.x, point.y) for point in triangle))
                      for triangle in mapbox_earcut_2d(points))
    if not triangles or any(len(triangle) != 3 for triangle in triangles):
        return None
    if abs(fsum(map(_area_twice, triangles)) - area) > max(GEOMETRY_EPSILON, area * 1e-9):
        return None
    return points, triangles


def _outlines_conflict(first, second, gap, budget):
    if not first.box.expanded(gap).overlaps(second.box):
        return False
    for edge in first.edges:
        budget.check()
        for other in second.nearby_edges(_bounds(edge).expanded(gap)):
            budget.check()
            if _edges_cross(*edge, *other):
                return True
            if min(_distance_to_edge(point, *other) for point in edge) < gap - GEOMETRY_EPSILON:
                return True
            if min(_distance_to_edge(point, *edge) for point in other) < gap - GEOMETRY_EPSILON:
                return True
    return first.has_point(second.points[0], budget) or second.has_point(first.points[0], budget)


def _contours_clear(positions, outline_at, gap):
    budget = Budget(limit=MAX_CLEARANCE_TESTS)
    gap = max(gap, ROUNDING_ALLOWANCE_MM)
    active = []
    try:
        for index, (x, y, angle, _, _) in sorted(positions.items(), key=lambda item: item[1][0]):
            outline = Outline(index, tuple((x + u, y + v) for u, v in outline_at(index, angle)))
            active = [other for other in active if other.box.max_x + gap >= outline.box.min_x]
            if any(_outlines_conflict(other, outline, gap, budget) for other in active):
                return False
            active.append(outline)
    except PlotterError:
        return False
    return True


def _edge_angles(hull):
    edges = []
    opposite = 1
    narrowest = (float("inf"), 0)
    # A polygon's minimum crosswise width aligns one side of its convex hull.
    # Move the opposite support point forward once around the hull.
    for first, second in zip(hull, hull[1:] + hull[:1]):
        length = dist(first, second)
        angle = -degrees(atan2(second[1] - first[1], second[0] - first[0])) % 180
        cardinal = round(angle / 90) * 90
        if abs(angle - cardinal) < 1e-10:
            angle = cardinal % 180
        support = _cross(first, second, hull[opposite])
        while True:
            following = (opposite + 1) % len(hull)
            next_support = _cross(first, second, hull[following])
            if next_support <= support:
                break
            opposite, support = following, next_support
        narrowest = min(narrowest, (support / length, angle))
        edges.append((length, angle))
    longest = sorted(edges, reverse=True)[:MAX_EDGE_ANGLES]
    return {angle for _, angle in longest} | {narrowest[1]}, narrowest[1]


def _rotation_boxes(part, paths, usable_x, usable_y, may_rotate):
    unrotated = Box(0, 0, part.box.length, part.box.width)
    if not may_rotate:
        return {0: unrotated}
    hull = _part_hull(part, paths)
    boxes = {0: unrotated, 90: Box(-part.box.width, 0, 0, part.box.length)}

    def footprint(angle):
        if angle not in boxes:
            boxes[angle] = _bounds(_project(hull, *_rotation_components(angle)))
        return boxes[angle]

    # Each orientation and its half-turn have the same rectangular footprint.
    edge_angles, _ = _edge_angles(hull)
    angles = sorted(set(range(0, 180, ANGLE_STEP_DEGREES)) | edge_angles |
                    {(angle + 90) % 180 for angle in edge_angles})
    for angle in angles:
        footprint(angle)

    # Fit a piece right up to the usable width or section length when the
    # best feasible angle lies between the samples.
    for axis, limit in (("length", usable_x), ("width", usable_y)):
        samples = [*angles, 180]
        crossings = [(first, last) for first, last in zip(samples, samples[1:])
                     if (getattr(footprint(first), axis) <= limit) !=
                     (getattr(footprint(last % 180), axis) <= limit)]
        for first, last in crossings[:MAX_ANGLE_REFINEMENTS]:
            first_fits = getattr(footprint(first), axis) <= limit
            for _ in range(14):
                middle = (first + last) / 2
                fits = getattr(_bounds(_project(hull, *_rotation_components(middle))), axis) <= limit
                if fits == first_fits:
                    first = middle
                else:
                    last = middle
            footprint((first if first_fits else last) % 180)
    # Drop larger footprints when an angle fits in both dimensions better.
    candidates = sorted(boxes, key=lambda angle: (boxes[angle].length, boxes[angle].width, angle))
    best_width = float("inf")
    retained = {0, 90}
    for angle in candidates:
        if boxes[angle].width < best_width:
            retained.add(angle)
            best_width = boxes[angle].width
    return {angle: boxes[angle] for angle in sorted(retained)}


def identify_parts(paths):
    if paths == []:
        return []
    return [
        {"number": part.number, "indices": list(part.indices),
         **({"rotation": part.rotation} if part.rotation is not None else {})}
        for part in _identified_parts(_paths(paths))
    ]


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    length: float
    width: float

    @property
    def area(self):
        return self.length * self.width


class Bin:
    def __init__(self, length, width, *, roll=False):
        self.free = [Rect(0, 0, length, width)]
        self.used_x = 0.0
        self.roll = roll

    def option(self, variants, gap):
        best = None
        for rect_index, rect in enumerate(self.free):
            for rotation, length, width in variants:
                need_x, need_y = length + gap, width + gap
                if need_x > rect.length + GEOMETRY_EPSILON or need_y > rect.width + GEOMETRY_EPSILON:
                    continue
                score = (max(0, rect.x + length - self.used_x),
                         min(rect.length - need_x, rect.width - need_y),
                         rect.area - need_x * need_y, rect.x, rect.y, rotation)
                if best is None or score < best[0]:
                    best = (score, rect_index, rotation, length, width)
        return best

    def place(self, option, gap):
        _, rect_index, rotation, length, width = option
        rect = self.free.pop(rect_index)
        used_x, used_y = length + gap, width + gap
        remaining_x = rect.length - used_x
        remaining_y = rect.width - used_y
        roll_end = self.roll and rect.x >= self.used_x
        if roll_end or remaining_x * rect.width >= remaining_y * rect.length:
            pieces = (Rect(rect.x + used_x, rect.y, remaining_x, rect.width),
                      Rect(rect.x, rect.y + used_y, used_x, remaining_y))
        else:
            pieces = (Rect(rect.x, rect.y + used_y, rect.length, remaining_y),
                      Rect(rect.x + used_x, rect.y, remaining_x, used_y))
        self.free.extend(piece for piece in pieces if min(piece.length, piece.width) >= gap)
        if len(self.free) > SEARCH_RECTS:
            self.free.sort(key=lambda free: -free.area)
            del self.free[SEARCH_RECTS:]
        self.used_x = max(self.used_x, rect.x + length)
        return rect.x, rect.y, rotation, length, width


def _regular_layout(regular, usable_x, usable_y, gap, sort_key):
    bins, positions = [], {}
    for index, variants in sorted(regular, key=sort_key):
        winner = None
        for page in range(max(0, len(bins) - SEARCH_BINS), len(bins)):
            option = bins[page].option(variants, gap)
            if option is not None:
                winner = (page, option)
                break
        if winner is None:
            page = len(bins)
            bins.append(Bin(usable_x + gap, usable_y + gap))
            winner = (page, bins[-1].option(variants, gap))
        page, option = winner
        x, y, rotation, length, width = bins[page].place(option, gap)
        positions[index] = (page, x, y, rotation, length, width)
    starts, start = [], 0.0
    for section in bins:
        starts.append(start)
        start += section.used_x + gap
    return start, {index: (starts[page] + x, y, angle, length, width)
                   for index, (page, x, y, angle, length, width) in positions.items()}


def _long_layout(long, usable_y, gap):
    """Pack long pieces side by side without consuming a slot for each contour."""
    lanes = []
    positions = {}
    for index, variants in sorted(long, key=lambda item: (-min(v[1] for v in item[1]), item[0])):
        best = None
        for lane_index in range(max(0, len(lanes) - SEARCH_BINS), len(lanes) + 1):
            old_width, old_length = lanes[lane_index] if lane_index < len(lanes) else (0.0, 0.0)
            y = old_width + gap if old_width else 0.0
            for rotation, length, width in variants:
                if y + width > usable_y + GEOMETRY_EPSILON:
                    continue
                growth = max(0, length - old_length)
                score = (growth, max(length, old_length), y + width, rotation)
                if best is None or score < best[0]:
                    best = (score, lane_index, y, rotation, length, width)
        _, lane_index, y, rotation, length, width = best
        if lane_index == len(lanes):
            lanes.append((y + max(width, GEOMETRY_EPSILON), length))
        else:
            lanes[lane_index] = (y + max(width, GEOMETRY_EPSILON), max(length, lanes[lane_index][1]))
        positions[index] = (lane_index, y, rotation, length, width)
    starts, start = [], 0.0
    for _, length in lanes:
        starts.append(start)
        start += length + gap
    return {index: (starts[lane], y, rotation, length, width)
            for index, (lane, y, rotation, length, width) in positions.items()}


def _continuous_layout(pieces, usable_y, gap, sort_key):
    # Putting every piece end to end would always fit inside this upper bound.
    length_limit = sum(max(variant[1] for variant in variants) + gap for _, variants in pieces)
    strip = Bin(length_limit, usable_y + gap, roll=True)
    positions = {}
    for index, variants in sorted(pieces, key=sort_key):
        option = strip.option(variants, gap)
        if option is None:
            return None
        positions[index] = strip.place(option, gap)
    return positions


def _layout_pages(positions, parts, source, rotation_boxes, start_margin, page_length, projected_x, closed_paths):
    end_x = start_margin + max(x + length for x, _, _, length, _ in positions.values())
    if end_x <= page_length + GEOMETRY_EPSILON:
        return end_x, 1, True
    extents = [None] * len(source)
    for index, (x, _, angle, _, _) in positions.items():
        part = parts[index]
        origin = start_margin + x
        offset = origin - rotation_boxes[index][angle].min_x
        for member in part.indices:
            key = (member, angle)
            if key not in projected_x:
                box = source[member].box
                if angle == 0:
                    low, high = box.min_x - part.box.min_x, box.max_x - part.box.min_x
                elif angle == 90:
                    low, high = part.box.min_y - box.max_y, part.box.min_y - box.min_y
                else:
                    cosine, sine = _rotation_components(angle)
                    values = [(u - part.box.min_x) * cosine - (v - part.box.min_y) * sine
                              for u, v in source[member].points]
                    low, high = min(values), max(values)
                projected_x[key] = low, high
            low, high = projected_x[key]
            extents[member] = (offset + low, 0.0, offset + high, 0.0)
    cuts, issues = roll_page_boundaries(extents, end_x, page_length, (part.indices for part in parts),
                                       closed_paths=closed_paths)
    return end_x, len(cuts) + 1 if not issues else 0, not issues


def _search_orders(pieces, usable_x, usable_y, gap, consider):
    """Try alternative placement sequences with a fixed work budget and seed."""
    if len(pieces) < 6:
        return
    trial_count = min(MAX_ORDER_TRIALS, ORDER_SEARCH_BUDGET // sum(len(variants) for _, variants in pieces))
    if trial_count < 40:
        return
    dimensions = {index: variants[0][1:] for index, variants in pieces}
    random = Random(57)
    seen = set()
    feasible, blocked = [], []

    def priority(index, x_weight, y_weight):
        length, width = dimensions[index]
        return -max(length, gap) ** x_weight * max(width, gap) ** y_weight, index

    for trial in range(trial_count):
        pool = blocked if trial % 5 == 0 and blocked else feasible
        if trial < 60:
            weight = 2 * trial / 59
            order = sorted(dimensions, key=lambda index: priority(index, weight, 2 - weight))
        elif trial < 350 or trial % 10 == 0 or not pool:
            order = sorted(dimensions, key=lambda index: priority(index, random.uniform(.85, 1.15),
                                                                  random.uniform(.85, 1.15)))
        else:
            order = list(random.choice(pool)[2])
            for _ in range(random.choice((1, 1, 1, 2, 3))):
                first, second = random.sample(range(len(order)), 2)
                if trial % 2:
                    order.insert(second, order.pop(first))
                else:
                    order[first], order[second] = order[second], order[first]
        signature = tuple(order)
        if signature in seen:
            continue
        seen.add(signature)
        rank = {index: position for position, index in enumerate(order)}
        order_key = lambda item: rank[item[0]]
        for candidate in (max_rects_layout(pieces, usable_x, usable_y, gap, order_key, continuous=True),
                          _continuous_layout(pieces, usable_y, gap, order_key)):
            if candidate is None:
                continue
            invalid, length, pages = consider(candidate)
            pool = blocked if invalid else feasible
            pool.append(((length, pages), trial, signature))
            pool.sort()
            del pool[12:]


def _existing_layout(parts, usable_y, gap, outline_at=None):
    """Keep a compact input when its footprints already respect width and clearance."""
    if len(parts) > 300:
        return None
    first_x = min(part.box.min_x for part in parts)
    first_y = min(part.box.min_y for part in parts)
    if max(part.box.max_y for part in parts) - first_y > usable_y:
        return None
    positions = {index: (part.box.min_x - first_x, part.box.min_y - first_y, 0, part.box.length, part.box.width)
                 for index, part in enumerate(parts)}
    if outline_at is not None:
        return positions if _contours_clear(positions, outline_at, gap) else None
    active = []
    for part in sorted(parts, key=lambda part: part.box.min_x):
        box = part.box
        active = [other for other in active if box.min_x < other.max_x + gap - GEOMETRY_EPSILON]
        if any(box.min_y < other.max_y + gap - GEOMETRY_EPSILON
               and other.min_y < box.max_y + gap - GEOMETRY_EPSILON for other in active):
            return None
        active.append(box)
    return positions


def _reorder_bands(positions, gap, consider):
    """Reorder groups separated by a full-width gap to reduce the number of feeds."""
    bands, starts, ends = [], [], []
    for index, (x, _, _, length, _) in sorted(positions.items(), key=lambda item: (item[1][0], item[0])):
        if not bands or x >= ends[-1] + gap - GEOMETRY_EPSILON:
            bands.append([])
            starts.append(x)
            ends.append(x)
        bands[-1].append(index)
        ends[-1] = max(ends[-1], x + length)
    if not 2 <= len(bands) <= 18:
        return

    original = list(range(len(bands)))
    for first in original:
        for last in original:
            if last == first:
                continue
            order = original.copy()
            order.insert(last, order.pop(first))
            shifted = {}
            start = starts[0]
            for slot, band in enumerate(order):
                for index in bands[band]:
                    x, y, angle, length, width = positions[index]
                    shifted[index] = (start + (x - starts[band]), y, angle, length, width)
                start += ends[band] - starts[band]
                if slot + 1 < len(bands):
                    start += starts[slot + 1] - ends[slot]
            consider(shifted)


def _contour_variants(positions, pieces, rotation_boxes, hulls, outlines, *, keep_angle):
    variants = {}
    for index, options in pieces:
        placed = positions[index][2:]
        shape = outlines.get(index, hulls[index])
        if (len(rotation_boxes[index]) == 1
                or _area_twice(shape) >= 1.9 * placed[1] * placed[2]):
            variants[index] = (placed,)
            continue
        cardinal = [variant for variant in options if variant[0] in (0, 90)]
        shortest = min(options, key=lambda variant: (variant[1], variant[2]))
        narrowest = min(options, key=lambda variant: (variant[2], variant[1]))
        chosen = [placed] if keep_angle else list(dict.fromkeys((placed, *cardinal, shortest, narrowest)))
        for angle, length, width in tuple(chosen):
            box = rotation_boxes[index][angle]
            opposite = angle + 180
            rotation_boxes[index][opposite] = Box(-box.max_x, -box.max_y, -box.min_x, -box.min_y)
            chosen.append((opposite, length, width))
        variants[index] = chosen
    return variants


def nest_paths(paths, *, width_mm, gap_mm, margin_mm, rotation, page_length_mm):
    """Lay out complete pieces without rescaling or reflecting the source paths.

    X runs along the material and Y across it. Compare layouts that preserve
    clearance and prefer those whose short parts fit on the roll sections.
    """
    width = number(width_mm, "Ширина полотна, мм", 1, MAX_ABS_COORDINATE_MM)
    page_length = number(page_length_mm, "Длина рабочего прохода, мм", 1, MAX_ABS_COORDINATE_MM)
    margin = number(margin_mm, "Отступ от края, мм", 0, width / 2)
    spacing = number(gap_mm, "Зазор между деталями, мм", 0, MAX_ABS_COORDINATE_MM)
    if rotation not in ("none", "any", "half", "quarter"):
        raise PlotterError("Выберите допустимый поворот деталей.")
    allow_rotation = rotation in ("any", "quarter")
    edge_allowance = 2 * margin + COORDINATE_OFFSET_MM
    usable_y = width - edge_allowance
    edge = max(COORDINATE_OFFSET_MM, min(spacing / 2, page_length / 8))
    start_margin = max(margin, edge)
    usable_x = page_length - start_margin - edge
    if min(usable_y, usable_x) <= 0:
        raise PlotterError("Для автораскладки уменьшите отступы от края или зазор между деталями.")
    gap = max(spacing, 1e-5)
    source = _paths(paths)
    parts = _identified_parts(source)
    pieces, orthogonal, unrotated, blockers = [], [], [], []
    rotation_boxes = []
    minimum_part_width = 0.0
    blocked_part_count = 0
    for index, part in enumerate(parts):
        may_rotate = part.rotation in ("any", "quarter") or (part.rotation is None and allow_rotation)
        boxes = _rotation_boxes(part, source, usable_x, usable_y, may_rotate)
        rotation_boxes.append(boxes)
        variants = [(angle, box.length, box.width) for angle, box in boxes.items()]
        possible = [variant for variant in variants if variant[2] <= usable_y]
        if not possible:
            blocked_part_count += 1
            narrowest_angle, _, narrowest_width = min(variants, key=lambda variant: variant[2])
            minimum_part_width = max(minimum_part_width, narrowest_width)
            cosine, sine = _rotation_components(narrowest_angle)
            actual = []
            for member in part.indices:
                path = source[member]
                width = path.box.width
                if narrowest_angle:
                    widths = [(u - part.box.min_x) * sine + (v - part.box.min_y) * cosine
                              for u, v in path.points]
                    width = max(widths) - min(widths)
                if width > usable_y:
                    actual.append(member)
            blockers.extend(actual or part.indices)
            continue
        pieces.append((index, possible))
        cardinal = [0, 90] if may_rotate else [0]
        right_angles = [(angle, boxes[angle].length, boxes[angle].width) for angle in cardinal
                        if boxes[angle].width <= usable_y]
        orthogonal.append((index, right_angles))
        unrotated.append((index, [variant for variant in right_angles if variant[0] == 0]))
    if blockers:
        allowed_widths = []
        for index, part in enumerate(parts):
            boxes = rotation_boxes[index]
            part_width = min(box.width for box in boxes.values())
            if part.rotation is None and not allow_rotation:
                hull = _part_hull(part, source)
                _, narrowest = _edge_angles(hull)
                part_width = min(part_width, _bounds(_project(hull, *_rotation_components(narrowest))).width)
            allowed_widths.append(part_width)
        raise NestingFitError(
            f"Детали не помещаются на полотне шириной {width:g} мм.",
            blockers, usable_width_mm=usable_y, minimum_part_width_mm=minimum_part_width,
            edge_allowance_mm=edge_allowance, quarter_turn_part_width_mm=max(allowed_widths),
            blocked_part_count=blocked_part_count,
        )

    def by_area(item):
        index, variants = item
        _, length, width = variants[0]
        return (-(length + gap) * (width + gap), -max(length, width), index)

    def by_length(item):
        return (-min(variant[1] for variant in item[1]), *by_area(item))

    def by_width(item):
        return (-min(variant[2] for variant in item[1]), *by_area(item))

    orders = (by_area, by_length, by_width) if len(parts) <= 2_000 else (by_area,)
    best = None
    projected_x = {}
    closed_paths = tuple(path.closed for path in source)

    def consider(candidate, *, keep_on_tie=False):
        nonlocal best
        length, pages, fits = _layout_pages(candidate, parts, source, rotation_boxes, start_margin,
                                            page_length, projected_x, closed_paths)
        score = (not fits, length, pages)
        if (best is None or score[0] < best[0][0] or score[0] == best[0][0]
                and (length < best[0][1] - GEOMETRY_EPSILON
                     or abs(length - best[0][1]) <= GEOMETRY_EPSILON
                     and (pages < best[0][2] or keep_on_tie and pages == best[0][2]))):
            best = (score, candidate)
        return score

    variants_sets = []
    for variants_by_part in (unrotated, orthogonal, pieces):
        if all(variants for _, variants in variants_by_part) and variants_by_part not in variants_sets:
            variants_sets.append(variants_by_part)
    partition_inputs = []
    for variants_by_part in variants_sets:
        regular, long = [], []
        for index, variants in variants_by_part:
            normal = [variant for variant in variants if variant[1] <= usable_x]
            (regular if normal else long).append((index, normal or variants))
        long_positions = _long_layout(long, usable_y, gap)
        if 2 <= len(regular) <= MAX_PARTITION_PARTS and len(long_positions) <= 1:
            partition_inputs.append((regular, long_positions))
        for order in orders:
            start, blocks = _regular_layout(regular, usable_x, usable_y, gap, order)
            consider({**blocks, **{index: (x + start, y, angle, length, width)
                                   for index, (x, y, angle, length, width) in long_positions.items()}})
            continuous = _continuous_layout(variants_by_part, usable_y, gap, order)
            if continuous is not None:
                consider(continuous)
            if len(parts) <= 500:
                maximal = max_rects_layout(regular, usable_x, usable_y, gap, order)
                if maximal is not None:
                    start = max((x + length + gap for x, _, _, length, _ in maximal.values()), default=0)
                    consider({**maximal, **{index: (x + start, y, angle, length, width)
                                           for index, (x, y, angle, length, width) in long_positions.items()}})
                continuous = max_rects_layout(variants_by_part, usable_x, usable_y, gap, order, continuous=True)
                if continuous is not None:
                    consider(continuous)

    _search_orders(variants_sets[0], usable_x, usable_y, gap, consider)
    existing = _existing_layout(parts, usable_y, gap)
    if existing is not None:
        consider(existing)

    band_gap = gap
    contour_shapes = None
    if 2 <= len(parts) <= MAX_CONTOUR_PARTS:
        initial = best[1]
        hulls = [_part_hull(part, source) for part in parts]
        outlines = {}
        triangles = {}
        budget = Budget(limit=MAX_CLEARANCE_TESTS)
        try:
            for index, part in enumerate(parts):
                shape = _concave_footprint(part, source, hulls[index], budget)
                if shape is not None:
                    outlines[index], triangles[index] = shape
        except PlotterError:
            # The bounded optional pass can fall back to hulls for the remaining parts.
            pass

        def transform(index, angle, points):
            box = rotation_boxes[index][angle]
            return tuple((x - box.min_x, y - box.min_y)
                         for x, y in _project(points, *_rotation_components(angle)))

        @lru_cache(maxsize=None)
        def hull_at(index, angle):
            return transform(index, angle, hulls[index])

        compacted = compact_contour_layout(initial, lambda index, angle: (hull_at(index, angle),), usable_y, gap)
        if compacted is not None:
            consider(compacted)

        if outlines:
            @lru_cache(maxsize=None)
            def outline_at(index, angle):
                return transform(index, angle, outlines.get(index, hulls[index]))

            @lru_cache(maxsize=None)
            def footprint(index, angle):
                return tuple(transform(index, angle, polygon) for polygon in triangles.get(index, (hulls[index],)))

            compacted = compact_contour_layout(initial, footprint, usable_y, gap, allow_equal=True)
            if compacted is not None and _contours_clear(compacted, outline_at, gap):
                consider(compacted, keep_on_tie=not _contours_clear(best[1], outline_at, gap))
            existing = _existing_layout(parts, usable_y, gap, outline_at)
            if existing is not None:
                consider(existing, keep_on_tie=True)
            if best[1] is compacted or best[1] is existing:
                # Keep the contour clearance between reordered bands.
                band_gap = max(gap, ROUNDING_ALLOWANCE_MM)
            contour_shapes = (footprint, outline_at)
        else:
            contour_shapes = (lambda index, angle: (hull_at(index, angle),), hull_at)
    if len(parts) <= 300 and best[0][2] > 1:
        _reorder_bands(best[1], band_gap, consider)

    for regular, long_positions in partition_inputs:
        consider(partition_layout(regular, usable_x, usable_y, gap, orders, long_positions))

    if contour_shapes is not None:
        footprint, outline_at = contour_shapes
        initial = best[1]
        contour_variants = [None]
        if len(parts) <= MAX_CONTOUR_ROTATION_PARTS:
            half_turns = _contour_variants(initial, pieces, rotation_boxes, hulls, outlines, keep_angle=True)
            if any(len(variants) > 1 for variants in half_turns.values()):
                contour_variants.extend((half_turns, _contour_variants(initial, pieces, rotation_boxes,
                                                                       hulls, outlines, keep_angle=False)))
        order_keys = (lambda index: (*initial[index][:2], index),
                      lambda index: (-initial[index][3] * initial[index][4], index),
                      lambda index: (-initial[index][3], -initial[index][4], index))
        contour_orders = dict.fromkeys(tuple(sorted(initial, key=order_key)) for order_key in order_keys)
        for variants in contour_variants:
            for order in contour_orders:
                for prefer_larger_angle in (False, True) if variants is not None else (False,):
                    candidate = compact_contour_layout(initial, footprint, usable_y, gap, order=order,
                                                       variants=variants, prefer_larger_angle=prefer_larger_angle)
                    if candidate is not None and _contours_clear(candidate, outline_at, gap):
                        consider(candidate)

    (_, _, pages), positions = best
    result = [None] * len(source)
    placements = []
    end_x = 0.0
    for index, part in enumerate(parts):
        x, y, angle, length, width = positions[index]
        x += start_margin
        y += margin
        end_x = max(end_x, x + length)
        placements.append({"indices": list(part.indices), "partNumber": part.number, "x": x, "y": y, "angle": angle,
                           "lengthMm": length, "widthMm": width})
        metadata = {"partNumber": part.number}
        if part.rotation is not None:
            metadata["partRotation"] = part.rotation
        cosine, sine = _rotation_components(angle)
        footprint = rotation_boxes[index][angle]
        base_x, base_y = x - footprint.min_x, y - footprint.min_y
        for member in part.indices:
            path = source[member]
            relative = ((u - part.box.min_x, v - part.box.min_y) for u, v in path.points)
            points = [[base_x + u, base_y + v] for u, v in _project(relative, cosine, sine)]
            result[member] = {"points": points, "closed": path.closed, "layer": path.layer, **metadata}
    if end_x + margin > MAX_ABS_COORDINATE_MM:
        raise PlotterError("Раскладка слишком длинная. Разбейте работу на несколько чертежей.")
    return {
        "paths": result, "partCount": len(parts), "lengthMm": end_x + margin,
        "pageCount": pages, "longPartCount": sum(length > page_length for _, _, _, length, _ in positions.values()),
        "widthMm": width_mm,
        "rotatedCount": sum(placement["angle"] != 0 for placement in placements), "placements": placements,
    }
