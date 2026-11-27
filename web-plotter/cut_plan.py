"""Prepare the geometry shown by the simulator before using a real controller."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import replace
from itertools import pairwise
from math import dist, nextafter
import re

from shapely import LineString, Polygon, STRtree, box, get_parts
from shapely.errors import GEOSException

from machine import (FEED_SPEED, MAX_PATHS, MAX_POINTS, MIN_WASTE_TRIM_Y_MM, PlotterError,
                     build_plan, cut_route, number)
from route_optimizer import optimize_page_route
from xiao_job import COORDINATE_OFFSET_MM, JobEncodingError, prepare_single_page, xiao_coordinates


MAX_COORDINATE_MM = 1_000_000_000
MAX_ROLL_PAGES = 128
MAX_PLANNED_POINTS = MAX_POINTS * 2
EPSILON_MM = 1e-6
MAX_UNSPLIT_MARK_MM = 10
XIAO_GRID_MM = 0.001
TRIM_CLEARANCE_MM = XIAO_GRID_MM + EPSILON_MM
DEFAULT_WASTE_MARGIN_MM = 10
MAX_WASTE_TRIM_Y_MM = 1420.0


def section_local_limit(width, page_add):
    limit = width - max(page_add, COORDINATE_OFFSET_MM)
    return nextafter(limit, 0) if limit + page_add > width else limit


def section_local_x(x, start_x, local_limit):
    local = x - start_x
    # Subtraction at a late roll boundary can round just beyond the working span.
    return local_limit if local_limit < local <= local_limit + EPSILON_MM else local


def _drawing_extents(job):
    if not isinstance(job, dict):
        raise PlotterError("Передайте параметры задания.")
    paths = job.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        raise PlotterError(f"Задание должно содержать от 1 до {MAX_PATHS} контуров.")

    extents = []
    count = 0
    for index, path in enumerate(paths, 1):
        points = path.get("points") if isinstance(path, dict) else None
        if not isinstance(points, list) or len(points) < 2:
            raise PlotterError(f"Контур {index}: требуется не меньше двух точек.")
        count += len(points)
        if count > MAX_POINTS:
            raise PlotterError("Слишком много точек в задании. Уменьшите чертёж.")
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) not in (int, float) or not -MAX_COORDINATE_MM <= value <= MAX_COORDINATE_MM
                           for value in point)):
                raise PlotterError(f"Контур {index}: неверные координаты точки в миллиметрах.")
            x, y = point
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        extents.append((min_x, min_y, max_x, max_y))
    return extents


def roll_page_boundaries(extents, last_x, usable, part_groups=(), *, closed_paths=None):
    """Pick feed boundaries, keeping short numbered parts on a single section."""
    groups = []
    long_members = set()
    for members in part_groups:
        low = min(extents[index][0] for index in members)
        high = max(extents[index][2] for index in members)
        if high - low > usable + EPSILON_MM:
            long_members.update(members)
        elif len(members) > 1 and high > low:
            groups.append((low, high, tuple(members)))

    protected = [(low, high, (index,)) for index, (low, _, high, _) in enumerate(extents)
                 if 0 < high - low <= usable + EPSILON_MM]
    cuts, issues = _page_boundaries([*protected, *groups], last_x, usable)
    if issues and long_members and closed_paths is not None:
        # An overlength part needs a seam. Its open lines may continue across the
        # same seam if they would otherwise obstruct a whole short part or hole.
        required = [(low, high, indices) for low, high, indices in protected
                    if indices[0] not in long_members or closed_paths[indices[0]]
                    or high - low <= MAX_UNSPLIT_MARK_MM]
        if len(required) < len(protected):
            return _page_boundaries([*required, *groups], last_x, usable)
    return cuts, issues


def _page_boundaries(protected, last_x, usable):
    ranges = []
    for low, high, indices in sorted(protected):
        if ranges and low < ranges[-1][1]:
            start, end, members = ranges[-1]
            members.update(indices)
            ranges[-1] = (start, max(end, high), members)
        else:
            ranges.append((low, high, set(indices)))

    start = 0.0
    cuts = []
    obstruction = 0
    while last_x - start > usable + EPSILON_MM:
        if len(cuts) >= MAX_ROLL_PAGES - 1:
            return [], [{"message": f"Раскладка занимает больше {MAX_ROLL_PAGES} проходов.", "indices": []}]
        limit = start + usable
        while obstruction < len(ranges) and ranges[obstruction][1] <= limit:
            obstruction += 1
        if obstruction < len(ranges) and ranges[obstruction][0] < limit:
            limit, _, indices = ranges[obstruction]
            if limit - start <= EPSILON_MM:
                return [], [{
                    "message": "Граница пересекает детали, которые помещаются на одном проходе. "
                               "Передвиньте выделенные контуры или оставьте запас между ними.",
                    "indices": sorted(indices),
                }]
        cuts.append(limit)
        start = limit
    return cuts, []


def _segment_pieces(first, second, cuts, previous_page):
    x1, y1 = first
    x2, y2 = second
    lower, upper = sorted((x1, x2))
    crossed = cuts[bisect_right(cuts, lower):bisect_left(cuts, upper)]
    if x1 > x2:
        crossed.reverse()
    points = [first]
    points.extend((x, y1 + (y2 - y1) * (x - x1) / (x2 - x1)) for x in crossed)
    points.append(second)
    for start, end in pairwise(points):
        low = min(start[0], end[0])
        page = bisect_right(cuts, low)
        # There are no boundaries strictly inside a fragment. For an edge
        # running along a boundary, stay on the incoming side if possible.
        if start[0] == end[0] and page > 0 and low == cuts[page - 1] and previous_page == page - 1:
            page = previous_page
        yield page, start, end


def _initial_boundary_side(route, cuts, closed):
    start = route[0][0]
    boundary = bisect_left(cuts, start)
    if boundary == len(cuts) or cuts[boundary] != start:
        return None
    # A closed contour has an incoming side at its first vertex. An open
    # contour that starts along the boundary follows its first departing edge.
    for x, _ in reversed(route) if closed else route:
        if x != start:
            return boundary if x < start else boundary + 1
    return None


def _contour_pieces(contour, cuts, point_budget):
    chunks = []
    seams = []
    points_total = 0
    route = cut_route(contour)
    previous_page = _initial_boundary_side(route, cuts, contour.closed)
    for first, second in pairwise(route):
        for page, start, end in _segment_pieces(first, second, cuts, previous_page):
            previous_page = page
            if chunks and page == chunks[-1][0]:
                chunks[-1][1].append(end)
                points_total += 1
            else:
                if chunks:
                    seams.append(start)
                chunks.append((page, [start, end]))
                points_total += 2
            # A closed route may drop one point when joining its first and last chunks.
            if points_total > point_budget + int(contour.closed):
                return None

    if len(chunks) == 1:
        return [(chunks[0][0], contour.points, contour.closed)], []
    if contour.closed:
        last_page, last_points = chunks[-1]
        if last_page == chunks[0][0]:
            chunks[0] = (last_page, last_points + chunks[0][1][1:])
            chunks.pop()
        else:
            seams.append(route[0])
    return [(page, points, False) for page, points in chunks], list(dict.fromkeys(seams))


def _page(number, start, end, feed):
    return {"number": number, "startX": start, "endX": end, "feedAfterMm": feed, "paths": []}


def _path(index, layer, points, closed):
    return {"index": index, "layer": layer, "points": [list(point) for point in points], "closed": closed}


def _stationary_notch(contour, is_notch, start_x):
    if not is_notch or contour.closed:
        return False
    points = iter(contour.points)
    x, y = next(points)
    start = xiao_coordinates(x - start_x, y)
    return all(xiao_coordinates(x - start_x, y) == start for x, y in points)


def _intersection_bands(shape, slice_):
    return [(part.bounds[1], part.bounds[3]) for part in get_parts(shape.intersection(slice_)) if not part.is_empty]


def _encoded_trim_ring(contour, boundary, start, local_limit, *, next_pass=False):
    # Match the splitter, including its newly inserted vertex at a pass edge.
    origin = boundary if next_pass else start
    points = []
    for first, second in pairwise((*contour.points, contour.points[0])):
        points.append(first)
        (x1, y1), (x2, y2) = first, second
        if min(x1, x2) < boundary < max(x1, x2):
            points.append((boundary, y1 + (y2 - y1) * (boundary - x1) / (x2 - x1)))
    rounded = [xiao_coordinates(section_local_x(x, origin, local_limit), y) for x, y in points]
    return Polygon([(float(x) + (origin - start), float(y) - COORDINATE_OFFSET_MM) for x, y in rounded])


def _trim_contact_vertices(page, boundary):
    contacts = defaultdict(list)
    for path in page["paths"]:
        contacts[path["index"]].extend(y for x, y in path["points"] if x == boundary)
    return contacts


def _ambiguous_trim_groups(shapes, contours, outlines, part_numbers, notch_flags):
    outline_indices = [index for index, valid in enumerate(outlines) if valid]
    outline_tree = STRtree([shapes[index] for index in outline_indices])
    markers = [not contour.closed and notch for contour, notch in zip(contours, notch_flags)]
    for index, (contour, part_number) in enumerate(zip(contours, part_numbers)):
        if not contour.closed and not markers[index] and part_number is not None:
            owners = outline_tree.query(shapes[index], predicate="covered_by")
            markers[index] = any(part_numbers[outline_indices[owner]] == part_number for owner in owners)
    unresolved = {index for index, outline in enumerate(outlines) if not outline and not markers[index]}
    groups = {part_numbers[index] for index in unresolved}
    ranges = {}
    for index, (part_number, shape) in enumerate(zip(part_numbers, shapes)):
        if index not in unresolved and (part_number is None or part_number not in groups):
            continue
        x0, y0, x1, y1 = shape.bounds
        low_x, low_y, high_x, high_y = ranges.get(part_number, (x0, y0, x1, y1))
        ranges[part_number] = (min(x0, low_x), min(y0, low_y), max(x1, high_x), max(y1, high_y))
    return markers, ranges


def _trim_blocked_bands(shapes, tree, contours, outlines, markers, uncertain, page,
                        boundary, machine_x, local_limit, trim_start, trim_end, final):
    start = page["startX"]
    effective_x = start + machine_x - COORDINATE_OFFSET_MM
    rounding = XIAO_GRID_MM / 2 + EPSILON_MM
    # Protect the intended footprint and any part of it that can reach the line
    # when a source X is rounded on either side of the feed boundary.
    min_x, max_x = min(boundary, effective_x - rounding), max(boundary, effective_x + rounding)
    blocked = []
    for part_number, (x0, y0, x1, y1) in uncertain.items():
        if x0 <= max_x and x1 >= min_x:
            if part_number is None:
                return None, False
            blocked.append((y0 - TRIM_CLEARANCE_MM, y1 + TRIM_CLEARANCE_MM))
    ambiguous = bool(blocked)
    low_y, high_y = trim_start - TRIM_CLEARANCE_MM, trim_end + TRIM_CLEARANCE_MM
    strip = box(min_x, low_y, max_x, high_y)
    raw_line = LineString([(boundary, low_y), (boundary, high_y)])
    rounded_line = LineString([(machine_x, low_y), (machine_x, high_y)])
    contacts = _trim_contact_vertices(page, boundary)
    for index in tree.query(strip):
        shape, contour = shapes[index], contours[index]
        if markers[index]:
            # A known notch is a protected mark, even when Xiao suppresses it.
            blocked.append((shape.bounds[1] - TRIM_CLEARANCE_MM, shape.bounds[3] + TRIM_CLEARANCE_MM))
            continue
        if not outlines[index]:
            continue
        raw_ends = [y for low, high in _intersection_bands(shape, raw_line) if high - low > EPSILON_MM
                    for y in (low, high)]
        valid_contacts = [y for y in contacts.get(index, ())
                          if any(abs(y - end) <= EPSILON_MM for end in raw_ends)]
        encoded_contacts = [float(xiao_coordinates(0, y)[1]) - COORDINATE_OFFSET_MM for y in valid_contacts]
        raw_bands = _intersection_bands(shape, strip)
        rounded_bands = []
        for next_pass in ((False,) if final else (False, True)):
            ring = _encoded_trim_ring(contour, boundary, start, local_limit, next_pass=next_pass)
            if not ring.is_valid or ring.area == 0:
                return None, ambiguous
            rounded_bands.extend(_intersection_bands(ring, rounded_line))
        for bands, allowed in ((raw_bands, valid_contacts), (rounded_bands, encoded_contacts)):
            for low, high in bands:
                # A tangent or an edge from the next pass cannot be a trim endpoint.
                meets = high - low > EPSILON_MM
                blocked.append((low if meets and any(abs(y - low) <= EPSILON_MM for y in allowed)
                                else low - TRIM_CLEARANCE_MM,
                                high if meets and any(abs(y - high) <= EPSILON_MM for y in allowed)
                                else high + TRIM_CLEARANCE_MM))
    return blocked, ambiguous


def _trim_free_bands(blocked, start, end):
    for low, high in sorted(blocked):
        if low >= end:
            break
        if low > start:
            yield start, low
        start = max(start, high)
    if start < end:
        yield start, end


def _inward_trim_y(y, *, upward):
    encoded_y = float(xiao_coordinates(0, y)[1]) - COORDINATE_OFFSET_MM
    if upward and encoded_y < y - EPSILON_MM:
        return round(encoded_y + XIAO_GRID_MM, 3)
    if not upward and encoded_y > y + EPSILON_MM:
        return round(encoded_y - XIAO_GRID_MM, 3)
    return y


def _plan_waste_trim(result, plan, job, width, height, page_add, part_numbers, notch_flags, *, extended_waste_trim):
    pages = result["pages"]
    for page in pages:
        page["wasteTrim"] = {"paths": [], "issues": []}

    def skip_all(message):
        for page in pages:
            page["wasteTrim"]["issues"].append(message)

    if "materialWidthMm" not in job:
        skip_all("Укажите ширину полотна для отреза отходов.")
        return
    fabric = number(job["materialWidthMm"], "Ширина полотна, мм", 1, max(height, 1_000_000))
    margin = number(job.get("materialMarginMm", 0), "Отступ от края, мм", 0, 100) or DEFAULT_WASTE_MARGIN_MM
    fabric_end = _inward_trim_y(fabric, upward=False)
    encoded_end = float(xiao_coordinates(0, fabric_end)[1])
    if fabric_end > height or encoded_end > height:
        skip_all("Отрез через всю ширину полотна выходит за рабочее поле по Y.")
        return
    if plan.bounds["maxY"] > fabric + EPSILON_MM:
        skip_all("Контуры выходят за указанную ширину полотна. Отрез отходов пропущен.")
        return
    trim_start, trim_end = 0.0, fabric_end
    if (extended_waste_trim and fabric <= MAX_WASTE_TRIM_Y_MM
            and float(xiao_coordinates(0, MAX_WASTE_TRIM_Y_MM)[1]) <= height):
        trim_start, trim_end = MIN_WASTE_TRIM_Y_MM, MAX_WASTE_TRIM_Y_MM

    contours = plan.contours
    shapes = [Polygon(contour.points) if contour.closed else LineString(contour.points) for contour in contours]
    outlines = [contour.closed and shape.is_valid and shape.area > 0 for contour, shape in zip(contours, shapes)]
    if not any(outlines):
        skip_all("Не удалось подтвердить внешние границы деталей для отреза отходов.")
        return
    tree = STRtree(shapes)
    markers, uncertain = _ambiguous_trim_groups(shapes, contours, outlines, part_numbers, notch_flags)
    local_limit = section_local_limit(width, page_add)
    points_total = sum(len(path["points"]) + path["closed"] for page in pages for path in page["paths"])
    for page in pages:
        trim = page["wasteTrim"]
        final = page is pages[-1]
        x = plan.bounds["maxX"] + margin if final else page["endX"]
        local_x = section_local_x(x, page["startX"], local_limit)
        encoded_x = float(xiao_coordinates(local_x, 0)[0])
        if (not 0 <= local_x <= local_limit or local_x > result["passLimitMm"] + EPSILON_MM
                or encoded_x > width or encoded_x - COORDINATE_OFFSET_MM > result["passLimitMm"] + EPSILON_MM):
            trim["issues"].append("Отрез отходов выходит за рабочую длину прохода по X.")
            continue
        try:
            blocked, ambiguous = _trim_blocked_bands(shapes, tree, contours, outlines, markers, uncertain, page,
                                                     x, encoded_x, local_limit, trim_start, trim_end, final)
        except GEOSException:
            blocked, ambiguous = None, False
        if blocked is None:
            trim["issues"].append("Граница прохода пересекает незамкнутый или неоднозначный контур.")
            continue
        if final and any(low <= trim_end and high >= trim_start for low, high in blocked):
            trim["issues"].append("После округления Xiao не осталось свободной линии для полного отреза отходов.")
            continue
        paths = []
        for low, high in _trim_free_bands(blocked, trim_start, trim_end):
            low, high = _inward_trim_y(low, upward=True), _inward_trim_y(high, upward=False)
            if high - low > EPSILON_MM and xiao_coordinates(local_x, low) != xiao_coordinates(local_x, high):
                paths.append({"points": [[x, low], [x, high]], "closed": False,
                              "operation": "wasteTrim", "layer": "waste-trim"})
        if not paths:
            trim["issues"].append("На границе прохода нет свободных участков для отреза отходов.")
            continue
        regular_points = sum(len(path["points"]) + path["closed"] for path in page["paths"])
        if (len(page["paths"]) + len(paths) > MAX_PATHS or regular_points + len(paths) * 2 > MAX_POINTS
                or points_total + len(paths) * 2 > MAX_PLANNED_POINTS):
            trim["issues"].append("Отрез отходов превысил бы допустимое число контуров или точек Xiao.")
            continue
        trim["paths"] = paths
        if ambiguous:
            trim["issues"].append("Участки возле незамкнутых или неоднозначных деталей не отрезаются.")
        points_total += len(paths) * 2


def _prepare_route(result, plan, part_numbers, optimize, notch_flags):
    if not optimize:
        return
    before = after = 0.0
    fixed_sources = {seam["index"] for seam in result["seams"]}
    for page in result["pages"]:
        start = (page["startX"], 0.0)
        trims = page["wasteTrim"]["paths"]
        terminal = trims[0]["points"][0] if trims else None
        page["paths"], old, new = optimize_page_route(page["paths"], start, plan.contours, fixed_sources,
                                                      part_numbers, notch_flags, terminal=terminal)
        between_trims = sum(dist(first["points"][-1], second["points"][0]) for first, second in pairwise(trims))
        before += old + between_trims
        after += new + between_trims
    result["routeOptimization"] = {"beforeMm": before, "afterMm": after}


def _calculate_totals(result, plan, job, height):
    # The material calculation reuses the nesting geometry, which uses the
    # same roll boundaries defined in this module.
    from cut_metrics import material_metrics

    cut = travel = feed = 0.0
    work_start = 0.0
    for page in result["pages"]:
        page_cut = page_travel = 0.0
        position = (page["startX"], 0.0)
        for path in (*page["paths"], *page["wasteTrim"]["paths"]):
            points = path["points"]
            route = points + [points[0]] if path["closed"] else points
            page_travel += dist(position, route[0])
            page_cut += sum(dist(first, second) for first, second in pairwise(route))
            position = route[-1]
        cut_seconds = page_cut / plan.speed
        travel_seconds = page_travel / plan.travel_speed
        work_seconds = cut_seconds + travel_seconds
        page.update(cutDistanceMm=page_cut, travelDistanceMm=page_travel,
                    cutSeconds=cut_seconds, travelSeconds=travel_seconds,
                    estimatedWorkSeconds=work_seconds, workStartSeconds=work_start,
                    feedSeconds=page["feedAfterMm"] / FEED_SPEED)
        work_start += work_seconds
        cut += page_cut
        travel += page_travel
        feed += page["feedAfterMm"]
    feed_seconds = feed / FEED_SPEED
    material, repaired = material_metrics(job, plan.bounds, height)
    result.update(cutDistanceMm=cut, travelDistanceMm=travel, feedDistanceMm=feed,
                  cutSeconds=cut / plan.speed, travelSeconds=travel / plan.travel_speed, feedSeconds=feed_seconds,
                  estimatedSeconds=work_start + feed_seconds, estimatedWorkSeconds=work_start,
                  cutSpeedMmS=plan.speed, travelSpeedMmS=plan.travel_speed, feedSpeedMmS=FEED_SPEED,
                  material=material, repairableContourCount=repaired)


def prepare_cut_plan(job, width, height, page_add_mm, *, roll_pass_length_mm=None, extended_waste_trim=False):
    """Return ordered contours in global mm, page boundaries and model timing.

    Roll pages describe a preview, not a verified hardware transfer sequence.
    A missing hardware profile still allows geometric simulation.
    """
    width = number(width, "Размер поля X, мм", 1, MAX_COORDINATE_MM)
    height = number(height, "Размер поля Y, мм", 1, MAX_COORDINATE_MM)
    page_add = 0.0 if page_add_mm is None else number(page_add_mm, "Запас страницы Xiao, мм", 0, width)
    usable = width - max(page_add, COORDINATE_OFFSET_MM)
    if usable <= 0:
        raise PlotterError("Запас страницы Xiao должен быть меньше рабочего поля по X.")
    pass_limit = width if roll_pass_length_mm is None else min(
        width, number(roll_pass_length_mm, "Длина рабочего прохода, мм", 1, MAX_COORDINATE_MM),
    )
    usable = min(usable, pass_limit)

    extents = _drawing_extents(job)
    bounds = {"minX": min(box[0] for box in extents), "minY": min(box[1] for box in extents),
              "maxX": max(box[2] for box in extents), "maxY": max(box[3] for box in extents)}
    result = {"mode": "single" if bounds["maxX"] <= pass_limit + EPSILON_MM else "roll", "pages": [],
              "bounds": bounds, "issues": [], "seams": [], "passLimitMm": usable}
    negatives = [index for index, (x, y, _, _) in enumerate(extents) if min(x, y) < -EPSILON_MM]
    if negatives:
        result["issues"].append({"message": "Часть раскладки находится за рабочим нулём. Сдвиньте контуры в поле.",
                                 "indices": negatives})
    too_wide = [index for index, (_, _, _, y) in enumerate(extents)
                if float(f"{y + COORDINATE_OFFSET_MM:.3f}") > height]
    if too_wide:
        result["issues"].append({"message": f"Контуры выходят за ширину рабочего поля по Y ({height:g} мм).",
                                 "indices": too_wide})
    if result["issues"]:
        return result

    optimize = job.get("optimize", True)
    if type(optimize) is not bool:
        raise PlotterError("Укажите, нужно ли оптимизировать порядок контуров.")
    # The planner needs stable source indices until after roll paths are split into sections.
    plan = build_plan({**job, "optimize": False}, max(width, bounds["maxX"]), height)
    if plan.repeats != 1 or plan.feed != 0:
        raise PlotterError("Повторы и фиксированная протяжка недоступны. Протяжка рассчитывается по границам проходов.")
    result["bounds"] = plan.bounds
    last_x = plan.bounds["maxX"]
    part_numbers = [path.get("partNumber") for path in job["paths"]]
    part_numbers = [number if type(number) is int and 1 <= number < 1 << 53 else None for number in part_numbers]
    notch_flags = [path.get("notch") is True for path in job["paths"]]

    groups = {}
    cuts, issues = [], []
    if last_x > usable + EPSILON_MM:
        for index, part_number in enumerate(part_numbers):
            if part_number is not None:
                groups.setdefault(part_number, []).append(index)
        cuts, issues = roll_page_boundaries(extents, last_x, usable, groups.values(),
                                           closed_paths=[contour.closed for contour in plan.contours])
        # A layout inside nominal X can still need a feed because of the Xiao allowance.
        # Require a cut on the first section before switching such a layout to roll mode.
        if cuts and plan.bounds["minX"] < cuts[0]:
            result["mode"] = "roll"

    if result["mode"] == "single":
        indexed = [(index, contour) for index, contour in enumerate(plan.contours)
                   if not _stationary_notch(contour, notch_flags[index], 0.0)]
        if not indexed:
            result["issues"].append({"message": "Насечки слишком малы для реза на Xiao. Добавьте контуры для раскроя.",
                                     "indices": list(range(len(plan.contours)))})
            return result
        indices, contours = zip(*indexed)
        try:
            prepare_single_page(replace(plan, contours=contours), tool=4, width=width, height=height, page_add_mm=page_add)
        except JobEncodingError as error:
            match = re.match(r"Контур (\d+)", str(error))
            result["issues"].append({"message": str(error), "indices": [indices[int(match[1]) - 1]] if match else []})
            return result
        page = _page(1, 0.0, last_x, 0.0)
        page["paths"] = [_path(index, contour.layer, contour.points, contour.closed) for index, contour in indexed]
        result["pages"] = [page]
        _plan_waste_trim(result, plan, job, width, height, page_add, part_numbers, notch_flags,
                         extended_waste_trim=extended_waste_trim)
        _prepare_route(result, plan, part_numbers, optimize, notch_flags)
        _calculate_totals(result, plan, job, height)
        return result

    if issues:
        result["issues"] = issues
        return result
    group_x_spans = {number: (min(extents[index][0] for index in members),
                              max(extents[index][2] for index in members))
                     for number, members in groups.items()}

    intervals = list(pairwise([0.0, *cuts, last_x]))
    pages = [_page(index + 1, start, end, end - start if index < len(cuts) else 0.0)
             for index, (start, end) in enumerate(intervals)]
    points_total = 0
    for index, contour in enumerate(plan.contours):
        low, _, high, _ = extents[index]
        # A contour ending at a boundary stays on the preceding page. This is
        # especially important for a closed outline with an edge on the boundary.
        first_page = bisect_right(cuts, low)
        if low == high and first_page > 0 and low == cuts[first_page - 1]:
            group_low, group_high = group_x_spans.get(part_numbers[index], (low, high))
            if group_low < low == group_high:
                first_page -= 1
        if first_page == len(cuts) or high <= cuts[first_page]:
            if _stationary_notch(contour, notch_flags[index], pages[first_page]["startX"]):
                continue
            pieces = [(first_page, contour.points, contour.closed)]
            seams = []
        else:
            fragments = _contour_pieces(contour, cuts, MAX_PLANNED_POINTS - points_total)
            if fragments is None:
                result["issues"].append({"message": "После разбиения в плане слишком много точек.", "indices": [index]})
                result["seams"].clear()
                return result
            pieces, seams = fragments
        points_total += sum(len(points) + closed for _, points, closed in pieces)
        if points_total > MAX_PLANNED_POINTS:
            result["issues"].append({"message": "После разбиения в плане слишком много точек.", "indices": [index]})
            result["seams"].clear()
            return result
        for page_index, points, closed in pieces:
            pages[page_index]["paths"].append(_path(index, contour.layer, points, closed))
        result["seams"].extend({"x": x, "y": y, "index": index} for x, y in seams)
    result["pages"] = pages
    _plan_waste_trim(result, plan, job, width, height, page_add, part_numbers, notch_flags,
                     extended_waste_trim=extended_waste_trim)
    _prepare_route(result, plan, part_numbers, optimize, notch_flags)
    _calculate_totals(result, plan, job, height)
    return result
