from __future__ import annotations

from dataclasses import dataclass
from itertools import islice, pairwise
import math
from threading import RLock
from time import monotonic


Point = tuple[float, float]
MAX_PATHS = 10_000
MAX_POINTS = 250_000
MAX_REPEATED_POINTS = 1_000_000
FEED_SPEED = 150.0


class PlotterError(ValueError):
    pass


def number(value, label, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise PlotterError(f"{label}: допустимо от {minimum:g} до {maximum:g}.")
    return float(value)


@dataclass(frozen=True)
class Contour:
    points: tuple[Point, ...]
    closed: bool
    layer: str

    @property
    def end(self):
        return self.points[0] if self.closed else self.points[-1]


@dataclass(frozen=True)
class Plan:
    contours: tuple[Contour, ...]
    speed: float
    travel_speed: float
    feed: float
    repeats: int
    bounds: dict
    cut_distance: float
    travel_distance: float
    duration: float

    def summary(self):
        return {
            "contours": len(self.contours),
            "points": sum(len(contour.points) for contour in self.contours),
            "cutDistance": self.cut_distance,
            "travelDistance": self.travel_distance,
            "duration": self.duration,
            "bounds": self.bounds,
        }


def cut_route(contour):
    return contour.points + (contour.points[0],) if contour.closed else contour.points


def order_contours(contours, start):
    remaining = iter(contours)
    candidates = list(islice(remaining, 48))
    ordered = []
    position = start
    while candidates:
        def approach(contour):
            distance = math.dist(position, contour.points[0])
            if not contour.closed:
                distance = min(distance, math.dist(position, contour.points[-1]))
            return distance

        index = min(range(len(candidates)), key=lambda index: approach(candidates[index]))
        contour = candidates.pop(index)
        if not contour.closed and math.dist(position, contour.end) < math.dist(position, contour.points[0]):
            contour = Contour(contour.points[::-1], False, contour.layer)
        ordered.append(contour)
        position = contour.end
        next_contour = next(remaining, None)
        if next_contour is not None:
            candidates.append(next_contour)
    return ordered


def build_plan(job, width, height, start=(0.0, 0.0)):
    if not isinstance(job, dict):
        raise PlotterError("Передайте параметры задания.")
    paths = job.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        raise PlotterError(f"Задание должно содержать от 1 до {MAX_PATHS} контуров.")

    speed = number(job.get("speed", 250), "Скорость раскроя, мм/с", 1, 3000)
    travel_speed = number(job.get("travelSpeed", 500), "Скорость перемещения, мм/с", 1, 3000)
    feed = number(job.get("feed", 0), "Подача между циклами, мм", 0, 10_000)
    repeats = job.get("repeats", 1)
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise PlotterError("Количество циклов должно быть целым числом от 1 до 100.")
    optimize = job.get("optimize", False)
    if type(optimize) is not bool:
        raise PlotterError("Укажите, нужно ли оптимизировать порядок контуров.")

    contours = []
    point_count = 0
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for index, path in enumerate(paths, start=1):
        if not isinstance(path, dict) or not isinstance(path.get("points"), list):
            raise PlotterError(f"Контур {index}: ожидается список точек.")
        closed = path.get("closed", False)
        layer = path.get("layer", "0")
        if type(closed) is not bool or not isinstance(layer, str) or len(layer) > 255:
            raise PlotterError(f"Контур {index}: неверные свойства контура.")
        point_count += len(path["points"])
        if point_count > MAX_POINTS or point_count * repeats > MAX_REPEATED_POINTS:
            raise PlotterError("Слишком много точек в задании. Уменьшите чертёж или число циклов.")
        points = []
        for point in path["points"]:
            if not isinstance(point, list) or len(point) != 2:
                raise PlotterError(f"Контур {index}: у каждой точки должны быть координаты X и Y.")
            try:
                x = number(point[0], "X", -0.000001, width + 0.000001)
                y = number(point[1], "Y", -0.000001, height + 0.000001)
            except PlotterError as error:
                raise PlotterError(f"Контур {index} выходит за поле {width:g} × {height:g} мм ({error}).") from error
            x, y = max(0.0, min(width, x)), max(0.0, min(height, y))
            if not points or points[-1] != (x, y):
                points.append((x, y))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
        if closed and len(points) > 1 and points[0] == points[-1]:
            points.pop()
        minimum = 3 if closed else 2
        if len(set(points)) < minimum:
            raise PlotterError(f"Контур {index}: требуется не меньше {minimum} различных точек.")
        contours.append(Contour(tuple(points), closed, layer or "0"))

    if optimize:
        contours = order_contours(contours, start)
    cut_distance = sum(math.dist(first, second) for contour in contours
                       for first, second in pairwise(cut_route(contour)))
    first_point = contours[0].points[0]
    between = sum(math.dist(first.end, second.points[0]) for first, second in pairwise(contours))
    travel_distance = math.dist(start, first_point) + between * repeats
    travel_distance += math.dist(contours[-1].end, first_point) * (repeats - 1)
    cut_distance *= repeats
    duration = cut_distance / speed + travel_distance / travel_speed + feed * (repeats - 1) / FEED_SPEED
    bounds = {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y}
    return Plan(tuple(contours), speed, travel_speed, feed, repeats, bounds, cut_distance, travel_distance, duration)


@dataclass(frozen=True)
class Move:
    start: Point
    end: Point
    seconds: float
    phase: str
    feed: float = 0.0


def path_moves(points, speed, phase):
    for first, second in pairwise(points):
        if first != second:
            yield Move(first, second, math.dist(first, second) / speed, phase)


def job_moves(plan, start):
    position = start
    for cycle in range(plan.repeats):
        for contour in plan.contours:
            yield from path_moves((position, contour.points[0]), plan.travel_speed, "travel")
            yield from path_moves(cut_route(contour), plan.speed, "cut")
            position = contour.end
        if cycle < plan.repeats - 1 and plan.feed:
            yield Move(position, position, plan.feed / FEED_SPEED, "feed", plan.feed)


class Simulator:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.lock = RLock()
        self.x = self.y = self.w = 0.0
        self.z = 1.0
        self.origin_x = self.origin_y = 0.0
        self.vacuum = self.clamp = False
        self.feed_total = 0.0
        self.state = "idle"
        self.phase = "idle"
        self.progress = 0.0
        self._moves = iter(())
        self._move = None
        self._elapsed = self._move_elapsed = self._duration = 0.0
        self._updated = monotonic()

    def _advance(self, now):
        delta = max(0.0, now - self._updated)
        self._updated = now
        if self.state != "running":
            return
        while self._move is not None:
            move = self._move
            step = min(delta, move.seconds - self._move_elapsed)
            self._move_elapsed += step
            self._elapsed += step
            ratio = self._move_elapsed / move.seconds
            self.x = move.start[0] + ratio * (move.end[0] - move.start[0])
            self.y = move.start[1] + ratio * (move.end[1] - move.start[1])
            self.feed_total += move.feed * step / move.seconds
            if ratio < 1.0:
                break
            delta -= step
            self._next_move()
        self.progress = min(1.0, self._elapsed / self._duration) if self._move is not None else 1.0

    def _next_move(self):
        self._move = next(self._moves, None)
        self._move_elapsed = 0.0
        if self._move is None:
            self.state, self.phase, self.z = "completed", "idle", 1.0
            self.progress = 1.0
        else:
            self.phase = self._move.phase
            self.z = 0.0 if self.phase == "cut" else 1.0

    def _begin(self, moves, duration):
        self.state = "running"
        self._moves = iter(moves)
        self._elapsed = self.progress = 0.0
        self._duration = duration
        self._updated = monotonic()
        self._next_move()

    def _snapshot(self):
        return {
            "state": self.state, "phase": self.phase, "progress": self.progress,
            "x": self.x, "y": self.y, "z": self.z, "w": self.w,
            "originX": self.origin_x, "originY": self.origin_y,
            "vacuum": self.vacuum, "clamp": self.clamp, "feedTotal": self.feed_total,
        }

    def snapshot(self):
        with self.lock:
            self._advance(monotonic())
            return self._snapshot()

    def command(self, command):
        with self.lock:
            self._advance(monotonic())
            action = command.get("action")
            if action == "pause" and self.state == "running":
                self.state = "paused"
            elif action == "resume" and self.state == "paused":
                self.state = "running"
            elif action == "cancel" and self.state in ("running", "paused"):
                self._moves, self._move = iter(()), None
                self.state, self.phase, self.z = "cancelled", "idle", 1.0
                self.vacuum, self.clamp = False, False
            elif self.state in ("running", "paused"):
                raise PlotterError("Для этой команды сначала остановите симуляцию.")
            else:
                self._manual(action, command)
            return self._snapshot()

    def _manual(self, action, command):
        position = (self.x, self.y)
        if action == "jog":
            axis = command.get("axis")
            if axis not in ("x", "y", "z", "w"):
                raise PlotterError("Выберите ось X, Y, Z или W.")
            distance = number(command.get("distance"), "Шаг перемещения", -500, 500)
            target = getattr(self, axis) + distance
            limits = {"x": (0, self.width), "y": (0, self.height), "z": (-100, 100), "w": (-360, 360)}
            if not limits[axis][0] <= target <= limits[axis][1]:
                raise PlotterError("Перемещение выходит за пределы симулятора.")
            setattr(self, axis, target)
        elif action == "home":
            self.x = self.y = self.w = 0.0
            self.z = 1.0
        elif action == "set-zero":
            self.origin_x, self.origin_y = position
        elif action == "go-zero":
            self.x, self.y, self.z = self.origin_x, self.origin_y, 1.0
        elif action == "vacuum":
            self.vacuum = not self.vacuum
        elif action == "clamp":
            self.clamp = not self.clamp
        elif action == "feed":
            distance = number(command.get("distance"), "Подача, мм", 1, 10_000)
            duration = distance / FEED_SPEED
            self._begin([Move(position, position, duration, "feed", distance)], duration)
        elif action in ("start", "range"):
            plan = build_plan(command.get("job"), self.width, self.height, start=position)
            if action == "start":
                self._begin(job_moves(plan, position), plan.duration)
            else:
                min_x, min_y, max_x, max_y = (plan.bounds[key] for key in ("minX", "minY", "maxX", "maxY"))
                corners = ((min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y))
                moves = tuple(path_moves((position, *corners, corners[0]), plan.travel_speed, "range"))
                self._begin(moves, sum(move.seconds for move in moves))
        elif action in ("pause", "resume", "cancel"):
            raise PlotterError("Нет активного задания для этой команды.")
        else:
            raise PlotterError("Неизвестная команда.")
