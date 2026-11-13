"""Compact a roll layout using convex components and a conservative clearance."""

from bisect import bisect_right
from math import atan2, sqrt, tau


MAX_PARTS = 96
MAX_OBSTACLE_EDGES = 200_000
MAX_Y_OPTIONS = 96
EPSILON = 1e-7
ROUNDING_ALLOWANCE_MM = 0.002


def _polygon_sum(polygons):
    """Add convex polygons by walking their edges in counterclockwise order."""
    x = y = 0.0
    edges = []
    for polygon in polygons:
        left, bottom = min(polygon, key=lambda point: (point[1], point[0]))
        x, y = x + left, y + bottom
        for (ax, ay), (bx, by) in zip(polygon, (*polygon[1:], polygon[0])):
            dx, dy = bx - ax, by - ay
            if dx or dy:
                edges.append((atan2(dy, dx) % tau, dx, dy))
    points = []
    for _, dx, dy in sorted(edges):
        points.append((x, y))
        x, y = x + dx, y + dy
    return points


class Obstacle:
    """The forbidden origins of a convex component relative to a placed component."""

    def __init__(self, x, y, boundary):
        self.x, self.y = x, y
        right, left = [], []
        for first, last in zip(boundary, (*boundary[1:], boundary[0])):
            if first[1] < last[1]:
                right.append((first, last))
            elif last[1] < first[1]:
                left.append((last, first))
        self.right = right
        self.left = left[::-1]
        self.right_starts = [first[1] for first, _ in right]
        self.left_starts = [first[1] for first, _ in self.left]
        self.low_y = min(point[1] for point in boundary)
        self.high_y = max(point[1] for point in boundary)

    @staticmethod
    def _x_at(y, edges, starts):
        first, last = edges[max(0, bisect_right(starts, y) - 1)]
        return first[0] + (last[0] - first[0]) * (y - first[1]) / (last[1] - first[1])

    def interval(self, y):
        y -= self.y
        if self.low_y < y < self.high_y:
            low = self.x + self._x_at(y, self.left, self.left_starts)
            high = self.x + self._x_at(y, self.right, self.right_starts)
            if low < high:
                return low, high
        return None

    def y_options(self):
        # Corners in the left boundary can open space for the moving hull.
        yield self.y + self.low_y
        yield self.y + self.high_y
        if self.left:
            step = max(1, len(self.left_starts) // 3)
            for y in self.left_starts[1::step]:
                yield self.y + y


def _leftmost_x(obstacles, y):
    intervals = sorted(interval for obstacle in obstacles if (interval := obstacle.interval(y)) is not None)
    x = 0.0
    for low, high in intervals:
        if x <= low:
            break
        x = max(x, high)
    return x


def compact_contour_layout(positions, footprints, usable_y, gap, *, allow_equal=False,
                           order=None, variants=None, prefer_larger_angle=False):
    """Return a candidate with at most the original roll length.

    Each footprint is a collection of counterclockwise convex components relative
    to the placement origin. The clearance octagon encloses a circle of radius gap.
    """
    if not 2 <= len(positions) <= MAX_PARTS:
        return None
    original = order is None and variants is None
    if order is None:
        order = sorted(positions, key=lambda index: (*positions[index][:2], index))
    if variants is None:
        variants = {index: (position[2:],) for index, position in positions.items()}
    shapes = {(index, angle): footprints(index, angle) for index, options in variants.items()
              for angle, _, _ in options}
    if original:
        component_count = sum(map(len, shapes.values()))
        edge_work = sum((component_count - len(shape)) * sum(len(polygon) + 4 for polygon in shape)
                        for shape in shapes.values())
    else:
        edge_work = 0
        fixed_components = fixed_edges = 0
        for index in order:
            options = [shapes[index, angle] for angle, _, _ in variants[index]]
            for shape in options:
                edge_work += fixed_components * sum(len(polygon) + 8 for polygon in shape) + fixed_edges * len(shape)
            fixed_components += max(map(len, options))
            fixed_edges += max(sum(map(len, shape)) for shape in options)
    if edge_work > MAX_OBSTACLE_EDGES:
        return None
    clearance = gap + ROUNDING_ALLOWANCE_MM
    corner = clearance * (sqrt(2) - 1)
    padding = ((-corner, -clearance), (corner, -clearance), (clearance, -corner), (clearance, corner),
               (corner, clearance), (-corner, clearance), (-clearance, corner), (-clearance, -corner))
    end_limit = max(x + length for x, _, _, length, _ in positions.values())
    packed = {}
    used_x = 0.0
    for index in order:
        _, old_y, _, _, _ = positions[index]
        best = None
        for angle, length, width in variants[index]:
            mirrored = [tuple((-x, -y) for x, y in polygon) for polygon in shapes[index, angle]]
            high_y = max(0.0, usable_y - width)
            clamped_old_y = max(0.0, min(old_y, high_y))
            options = {0.0, clamped_old_y, high_y}
            obstacles = []
            for other, (x, y, other_angle, _, other_width) in packed.items():
                options.update((y, y + other_width + clearance, y - width - clearance))
                for fixed in shapes[other, other_angle]:
                    for moving in mirrored:
                        obstacle = Obstacle(x, y, _polygon_sum((fixed, moving, padding)))
                        obstacles.append(obstacle)
                        options.update(obstacle.y_options())
            choices = sorted({max(0.0, min(high_y, y)) for y in options})
            if len(choices) > MAX_Y_OPTIONS:
                choices = sorted({clamped_old_y, *(choices[k * (len(choices) - 1) // (MAX_Y_OPTIONS - 1)]
                                                   for k in range(MAX_Y_OPTIONS))})
            x, y = min((_leftmost_x(obstacles, y), y) for y in choices)
            score = (x + length, x, y, -angle if prefer_larger_angle else angle)
            if best is None or score < best[0]:
                best = score, (x, y, angle, length, width)
        score, position = best
        used_x = max(used_x, score[0])
        if used_x > end_limit + EPSILON:
            return None
        packed[index] = position
    return packed if allow_equal or used_x < end_limit - EPSILON else None
