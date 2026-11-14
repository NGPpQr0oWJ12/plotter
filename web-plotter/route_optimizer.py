"""Order cuts independently on each working section of the material."""

from bisect import bisect_right
from heapq import heapify, heappop, heappush
from itertools import pairwise
from math import dist


MAX_STARTS = 12
FULL_SEARCH_LIMIT = 256
GEOMETRY_LIMIT = 1_000_000
EPSILON = 1e-7
NOTCH_GAP_MM = 0.25
NUMBERED_NOTCH_GAP_MM = 0.75


class GeometryBudget:
    def __init__(self):
        self.remaining = GEOMETRY_LIMIT

    def visit(self, count=1):
        self.remaining -= count
        if self.remaining < 0:
            raise GeometryLimit


class GeometryLimit(Exception):
    pass


def _bounds(points):
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _contains_box(outer, inner):
    return all(lo - EPSILON <= value <= hi + EPSILON for lo, hi, value in (
        (outer[0], outer[2], inner[0]), (outer[0], outer[2], inner[2]),
        (outer[1], outer[3], inner[1]), (outer[1], outer[3], inner[3]),
    ))


def _signed_area_twice(points):
    x, y = points[0]
    return sum((a - x) * (d - y) - (b - y) * (c - x)
               for (a, b), (c, d) in pairwise((*points, points[0])))


def _point_distance_to_edge(point, first, second):
    dx, dy = second[0] - first[0], second[1] - first[1]
    length_squared = dx * dx + dy * dy
    if not length_squared:
        return dist(point, first)
    offset_x, offset_y = point[0] - first[0], point[1] - first[1]
    fraction = min(1, max(0, (offset_x * dx + offset_y * dy) / length_squared))
    return dist((offset_x, offset_y), (fraction * dx, fraction * dy))


def _inside(point, edges, budget):
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in edges:
        budget.visit()
        if min(ay, by) - EPSILON <= y <= max(ay, by) + EPSILON:
            if (min(ax, bx) - EPSILON <= x <= max(ax, bx) + EPSILON
                    and _point_distance_to_edge(point, (ax, ay), (bx, by)) <= EPSILON):
                return True
            if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
                inside = not inside
    return inside


def _cross(first, second, point):
    return ((second[0] - first[0]) * (point[1] - first[1])
            - (second[1] - first[1]) * (point[0] - first[0]))


def _touches_notch(points, outline, gap, budget):
    for first, second in pairwise(points):
        for third, fourth in outline:
            budget.visit()
            if (max(first[0], second[0]) + gap < min(third[0], fourth[0])
                    or max(third[0], fourth[0]) + gap < min(first[0], second[0])
                    or max(first[1], second[1]) + gap < min(third[1], fourth[1])
                    or max(third[1], fourth[1]) + gap < min(first[1], second[1])):
                continue
            if (_cross(first, second, third) * _cross(first, second, fourth) < 0
                    and _cross(third, fourth, first) * _cross(third, fourth, second) < 0):
                return True
            if min(_point_distance_to_edge(first, third, fourth), _point_distance_to_edge(second, third, fourth),
                   _point_distance_to_edge(third, first, second), _point_distance_to_edge(fourth, first, second)
                   ) <= gap:
                return True
    return False


def _dependencies(paths, source, fixed_sources, part_numbers, notch_flags):
    """Precede the outer cut with its marks/holes, and retain the order of seam fragments."""
    count = len(paths)
    predecessors = [set() for _ in paths]
    by_source = {}
    path_bounds = [_bounds(path["points"]) for path in paths]
    by_x = sorted(range(count), key=lambda index: path_bounds[index][0])
    left_x = [path_bounds[index][0] for index in by_x]
    areas = {}
    budget = GeometryBudget()
    for index, path in enumerate(paths):
        by_source.setdefault(path["index"], []).append(index)
    for source_index, members in by_source.items():
        if source_index in fixed_sources:
            for first, second in pairwise(members):
                predecessors[second].add(first)
        if source[source_index].closed:
            areas[source_index] = abs(_signed_area_twice(source[source_index].points))

    for source_index, area in areas.items():
        if area <= EPSILON:
            continue
        outline = source[source_index].points
        edges = tuple(pairwise((*outline, outline[0])))
        outer_box = _bounds(outline)
        number = part_numbers[source_index]
        widest_gap = NOTCH_GAP_MM if number is None else NUMBERED_NOTCH_GAP_MM
        limit = bisect_right(left_x, outer_box[2] + widest_gap)
        for position in range(limit):
            budget.visit()
            child = by_x[position]
            child_source = paths[child]["index"]
            if child_source == source_index or areas.get(child_source, -1) >= area - EPSILON:
                continue
            inner_box = path_bounds[child]
            if (inner_box[2] + widest_gap < outer_box[0]
                    or inner_box[3] + widest_gap < outer_box[1]
                    or inner_box[1] - widest_gap > outer_box[3]):
                continue
            points = paths[child]["points"]
            contained = _contains_box(outer_box, inner_box) and all(_inside(point, edges, budget) for point in points)
            if not contained:
                if child_source in areas or child_source in fixed_sources:
                    continue
                if number is not None and part_numbers[child_source] not in (number, None):
                    continue
                gap = widest_gap if part_numbers[child_source] == number else NOTCH_GAP_MM
                # CAD seam marks are short strokes that can extend outside the cut outline.
                short = (max(inner_box[2] - inner_box[0], inner_box[3] - inner_box[1]) <= 10.0 + EPSILON
                         and sum(dist(a, b) for a, b in pairwise(points)) <= 10.0 + EPSILON)
                bound_notch = number is not None and part_numbers[child_source] == number and notch_flags[child_source]
                if (not short and not bound_notch) or not _touches_notch(points, edges, gap, budget):
                    continue
            for member in by_source[source_index]:
                predecessors[member].add(child)
    return predecessors


def _closed_starts(points):
    count = len(points)
    if count <= MAX_STARTS:
        return range(count)
    # Spread candidates by arc length so a densely flattened curve does not crowd out long edges.
    lengths = [0.0]
    for first, second in pairwise((*points, points[0])):
        lengths.append(lengths[-1] + dist(first, second))
    starts = {0}
    for axis in (0, 1):
        starts.add(min(range(count), key=lambda index: points[index][axis]))
        starts.add(max(range(count), key=lambda index: points[index][axis]))
    for step in range(1, MAX_STARTS - 4):
        starts.add(bisect_right(lengths, lengths[-1] * step / (MAX_STARTS - 4)) - 1)
    return sorted(starts)


def _entry_options(path, fixed):
    points = path["points"]
    if path["closed"]:
        return [(points[index], points[index], index) for index in _closed_starts(points)]
    options = [(points[0], points[-1], 0)]
    if not fixed and points[0] != points[-1]:
        options.append((points[-1], points[0], -1))
    return options


def _choose_entries(order, options, origin):
    """Find the cheapest combination of entry choices for an ordered sequence."""
    previous = [(origin, origin, 0)]
    costs = [0.0]
    transitions = []
    for index in order:
        choices = options[index]
        next_costs = []
        back = []
        for start, _, _ in choices:
            cost, last = min((cost + dist(end, start), last) for last, ((_, end, _), cost)
                             in enumerate(zip(previous, costs)))
            next_costs.append(cost)
            back.append(last)
        transitions.append(back)
        previous, costs = choices, next_costs
    winner = min(range(len(costs)), key=costs.__getitem__)
    best = costs[winner]
    chosen = []
    for index, back in zip(reversed(order), reversed(transitions)):
        chosen.append(options[index][winner])
        winner = back[winner]
    chosen.reverse()
    return best, chosen


def _ready_after(index, indegrees, successors, ready):
    for following in successors[index]:
        indegrees[following] -= 1
        if indegrees[following] == 0:
            ready.add(following)


def _initial_order(options, predecessors, successors, origin, nearest=True):
    count = len(options)
    indegrees = [len(dependencies) for dependencies in predecessors]
    ready = {index for index, degree in enumerate(indegrees) if degree == 0}
    # Keep search bounded on unusually crowded sections, using spatial order instead of DXF order.
    if not nearest or count > FULL_SEARCH_LIMIT:
        spatial = sorted(range(count), key=lambda index: (options[index][0][0], index)) if nearest else range(count)
        ranks = [0] * count
        for rank, index in enumerate(spatial):
            ranks[index] = rank
        pending = [(ranks[index], index) for index in ready]
        heapify(pending)
        order = []
        while pending:
            _, index = heappop(pending)
            order.append(index)
            new_ready = set()
            _ready_after(index, indegrees, successors, new_ready)
            for candidate in new_ready:
                heappush(pending, (ranks[candidate], candidate))
        return order

    order = []
    position = origin
    while ready:
        _, index, choice = min(
            (dist(position, entry), candidate, variant)
            for candidate in ready
            for variant, (entry, _, _) in enumerate(options[candidate])
        )
        ready.remove(index)
        order.append(index)
        position = options[index][choice][1]
        _ready_after(index, indegrees, successors, ready)
    return order


def _improve_order(order, chosen, predecessors, successors, fixed, origin):
    """Find the best legal reversal or single-cut relocation of the current route."""
    count = len(order)
    positions = {index: position for position, index in enumerate(order)}
    earliest = [max((positions[pred] for pred in predecessors[index]), default=-1) for index in order]
    latest = [min((positions[succ] for succ in successors[index]), default=count) for index in order]
    best = -EPSILON
    move = None
    span = count if count <= FULL_SEARCH_LIMIT else 24
    for left, (start, end, _) in enumerate(chosen):
        previous = chosen[left - 1][1] if left else origin
        following = chosen[left + 1][0] if left + 1 < count else None
        removal = -dist(previous, start)
        if following is not None:
            removal += dist(previous, following) - dist(end, following)
        # Insert the cut before a later path, after first removing its existing pair of links.
        for gap in range(max(left + 2, earliest[left] + 1), min(latest[left], left + span, count) + 1):
            gap_previous = chosen[gap - 1][1]
            delta = removal + dist(gap_previous, start)
            if gap < count:
                gap_next = chosen[gap][0]
                delta += dist(end, gap_next) - dist(gap_previous, gap_next)
            if delta < best:
                best, move = delta, ("relocate", left, gap - 1)
        for gap in range(max(0, earliest[left] + 1, left - span), left):
            gap_previous = chosen[gap - 1][1] if gap else origin
            gap_next = chosen[gap][0]
            delta = removal + dist(gap_previous, start) + dist(end, gap_next) - dist(gap_previous, gap_next)
            if delta < best:
                best, move = delta, ("relocate", left, gap)

        if fixed[order[left]]:
            continue
        # Internal links have the same length after reversing both the order and open cut directions.
        for right in range(left + 1, min(count, left + span)):
            if earliest[right] >= left or fixed[order[right]]:
                break
            last_start, last_end, _ = chosen[right]
            delta = dist(previous, last_end) - dist(previous, start)
            if right + 1 < count:
                after = chosen[right + 1][0]
                delta += dist(start, after) - dist(last_end, after)
            if delta < best:
                best, move = delta, ("reverse", left, right)
    if move is None:
        return None
    kind, first, second = move
    updated = order.copy()
    if kind == "reverse":
        updated[first:second + 1] = reversed(updated[first:second + 1])
    else:
        updated.insert(second, updated.pop(first))
    return updated


def _travel(paths, origin):
    total = 0.0
    for path in paths:
        points = path["points"]
        total += dist(origin, points[0])
        origin = points[0] if path["closed"] else points[-1]
    return total


def optimize_page_route(paths, origin, source, fixed_sources, part_numbers, notch_flags):
    before = _travel(paths, origin)
    if not paths:
        return paths, before, before
    try:
        predecessors = _dependencies(paths, source, fixed_sources, part_numbers, notch_flags)
    except GeometryLimit:
        # Preserve the source order if the geometric checks cannot complete.
        return paths, before, before
    successors = [set() for _ in paths]
    for index, dependencies in enumerate(predecessors):
        for predecessor in dependencies:
            successors[predecessor].add(index)
    fixed = [path["index"] in fixed_sources for path in paths]
    options = [_entry_options(path, fixed[index]) for index, path in enumerate(paths)]
    candidates = [_initial_order(options, predecessors, successors, origin, nearest=nearest)
                  for nearest in (False, True)]
    if any(len(candidate) != len(paths) for candidate in candidates):
        return paths, before, before
    best = None
    for order in candidates:
        cost, chosen = _choose_entries(order, options, origin)
        # Each pass strictly reduces travel. Bound work for very large drawings.
        for _ in range(12 if len(paths) <= FULL_SEARCH_LIMIT else 2):
            improved = _improve_order(order, chosen, predecessors, successors, fixed, origin)
            if improved is None:
                break
            new_cost, new_choices = _choose_entries(improved, options, origin)
            if new_cost + EPSILON >= cost:
                break
            order, cost, chosen = improved, new_cost, new_choices
        if best is None or cost + EPSILON < best[0]:
            best = cost, order, chosen
    cost, order, chosen = best
    result = []
    for index, (_, _, entry) in zip(order, chosen):
        path = paths[index]
        points = path["points"]
        if entry == -1:
            points = points[::-1]
        elif entry:
            points = points[entry:] + points[:entry]
        result.append({**path, "points": points})
    return result, before, cost
