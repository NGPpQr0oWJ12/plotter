"""Material utilization for a prepared cut plan, in global millimetres."""

from itertools import pairwise
from math import dist, fsum

from cad import LOOP_ENDPOINT_TOLERANCE_MM, repair_closed_contours
from machine import PlotterError, number
from nesting import (BoxTree, Budget, GEOMETRY_EPSILON, MAX_NOTCH_LENGTH_MM,
                     Outline, _bounds, _cross, _edges_cross, _on_segment)


def _ring_area(points):
    # Translate before the shoelace sum so coordinates far along the roll
    # don't cause loss of precision when calculating small detail areas.
    x0, y0 = points[0]
    return abs(fsum((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
                    for (x1, y1), (x2, y2) in pairwise((*points, points[0])))) / 2


def _proper_cross(first, second, third, fourth):
    return (_cross(first, second, third) * _cross(first, second, fourth) < 0
            and _cross(third, fourth, first) * _cross(third, fourth, second) < 0)


def _self_intersects(outline, budget):
    entries = [(_bounds(edge), index) for index, edge in enumerate(outline.edges)]
    tree = BoxTree(entries[:])
    for index, (box, _) in enumerate(entries):
        first, second = outline.edges[index]
        for other in tree.search(box):
            budget.check()
            if other <= index + 1 or (index == 0 and other == len(entries) - 1):
                continue
            third, fourth = outline.edges[other]
            if _edges_cross(first, second, third, fourth):
                # A near-coincident closing vertex can leave a microscopic sliver
                # in flattened DXF paths without invalidating the useful area.
                between_closing_edges = index == 0 and other == len(entries) - 2
                if not (between_closing_edges and dist(first, fourth) <= LOOP_ENDPOINT_TOLERANCE_MM):
                    return True
    return False


def _point_relation(point, outline, budget):
    budget.check()
    if not outline.has_point(point, budget):
        return -1
    for edge in outline.nearby_edges(_bounds((point,)).expanded(GEOMETRY_EPSILON)):
        budget.check()
        if _on_segment(*edge, point):
            return 0
    return 1


def _boundary_relations(outline, container, budget):
    states = set()
    for first, second in outline.edges:
        states.add(_point_relation(first, container, budget))
        if 0 in states:
            midpoint = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
            states.add(_point_relation(midpoint, container, budget))
        if -1 in states and 1 in states:
            break
    return states


def _overlap_or_nesting(first, second, budget):
    a, b = first.box, second.box
    if min(a.max_x, b.max_x) - max(a.min_x, b.min_x) <= GEOMETRY_EPSILON:
        return 0
    if min(a.max_y, b.max_y) - max(a.min_y, b.min_y) <= GEOMETRY_EPSILON:
        return 0
    for edge in first.edges:
        budget.check()
        for other in second.nearby_edges(_bounds(edge)):
            budget.check()
            if _proper_cross(*edge, *other):
                return None
    first_states = _boundary_relations(first, second, budget)
    second_states = _boundary_relations(second, first, budget)
    if 1 in first_states and -1 not in first_states and 1 not in second_states and b.contains(a):
        return -1
    if 1 in second_states and -1 not in second_states and 1 not in first_states and a.contains(b):
        return 1
    # Coincident contours and positive-area overlaps make the numerator ambiguous.
    if 1 in first_states or 1 in second_states or first_states == second_states == {0}:
        return None
    return 0


def _parts_area(paths):
    outlines, areas = [], []
    budget = Budget()
    for index, path in enumerate(paths):
        if not path.get("closed", False):
            continue
        vertices = tuple(map(tuple, path["points"]))
        points = tuple(first for first, second in pairwise((*vertices, vertices[0])) if first != second)
        if len(points) < 3 or len(set(points)) != len(points):
            return None
        area = _ring_area(points)
        outline = Outline(index, points)
        if area <= GEOMETRY_EPSILON or _self_intersects(outline, budget):
            return None
        outlines.append(outline)
        areas.append(area)
    if not outlines:
        return None

    depths = [0] * len(outlines)
    index = BoxTree([(outline.box, i) for i, outline in enumerate(outlines)])
    for i, outline in enumerate(outlines):
        for j in index.search(outline.box):
            budget.check()
            if j <= i:
                continue
            relation = _overlap_or_nesting(outline, outlines[j], budget)
            if relation is None:
                return None
            if relation == -1:
                depths[i] += 1
            elif relation == 1:
                depths[j] += 1
    return fsum(area if depth % 2 == 0 else -area for area, depth in zip(areas, depths))


def material_metrics(job, bounds, table_width):
    width = min(table_width, number(job.get("materialWidthMm", table_width), "Ширина полотна, мм",
                                    1, max(table_width, 1_000_000)))
    margin = number(job.get("materialMarginMm", 0), "Отступ от края, мм", 0, 100)
    length = bounds["maxX"] + margin
    fabric_area = width * length
    repaired = repair_closed_contours(job["paths"])
    closed_parts = {path.get("partNumber") for path in repaired.paths
                    if path.get("closed", False) and "partNumber" in path}
    open_count = sum(max(box.width, box.length) > MAX_NOTCH_LENGTH_MM for box in
                     (_bounds(path["points"]) for path in repaired.paths
                      if not path.get("closed", False) and path.get("partNumber") not in closed_parts))
    try:
        parts_area = _parts_area(repaired.paths)
    except PlotterError:
        # A drawing too complex to measure still has a valid cutting plan.
        parts_area = None
    if (parts_area is None or fabric_area <= 0 or parts_area > fabric_area + GEOMETRY_EPSILON
            or bounds["maxY"] > width + GEOMETRY_EPSILON):
        utilization = None
    else:
        utilization = 100 * parts_area / fabric_area
    return {"widthMm": width, "lengthMm": length, "areaMm2": fabric_area,
            "partsAreaMm2": parts_area, "utilizationPercent": utilization,
            "openContourCount": open_count}, repaired.closed_count + repaired.small_gap_count
