"""In-memory controller for the demo, driven by the same requests as the real panel."""

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import accumulate, pairwise
from math import dist, hypot
import re
import secrets
from threading import RLock
from time import monotonic

from controller import (
    ACTUATOR_COMMAND_EFFECTS, AXES, CONTROL_SESSION_SECONDS, EVENT_LABELS, FEED_MODES, HOLD_LEASE_SECONDS,
    IDLE_CANCEL_OPERATIONS, OPERATIONS_REQUIRING_CLEARANCE, OPERATION_LABELS, ControlSession,
    ControllerBusyError, ControllerError, JobReceipt,
)
from machine import Point
from run_monitor import RunMonitor


PAGE_ADD_MM = 0.12


@dataclass
class Motion:
    action: str
    points: tuple[Point, ...]
    seconds: float
    ends: tuple[float, ...] = field(init=False)
    elapsed: float = 0.0

    def __post_init__(self):
        self.ends = tuple(accumulate(dist(start, end) for start, end in pairwise(self.points)))

    def advance(self, seconds):
        self.elapsed = min(self.seconds, self.elapsed + seconds)
        distance = self.ends[-1] * (self.elapsed / self.seconds)
        completed = bisect_right(self.ends, distance)
        start = self.points[completed]
        if completed == len(self.ends):
            return start, completed
        previous = self.ends[completed - 1] if completed else 0.0
        ratio = (distance - previous) / (self.ends[completed] - previous)
        end = self.points[completed + 1]
        return (start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio), completed


@dataclass
class Jog:
    session: str
    axis: str
    direction: int
    expires: float


class DemoController:
    auto_run_readback = True
    upload_uncertain = False
    upload_in_progress = False

    def __init__(self, width, height):
        self._lock = RLock()
        self._limits = {"x": (0, width), "y": (0, height), "z": (-20, 20), "w": (-360, 360)}
        self._position = dict.fromkeys(AXES, 0.0)
        self._material_offset_mm = 0.0
        self._zero = (0.0, 0.0)
        self._updated = monotonic()
        self._control = None
        self._jog = None
        self._finished_holds = OrderedDict()
        self._motion = None
        self._coordinate_move = None
        self._manual_speed = "fast"
        self._actuators = {"epoch": secrets.token_urlsafe(16), "revision": 0,
                           "clampLowered": False, "vacuumEnabled": False}
        self._receipt = None
        self._points = ()
        self._tool_down = ()
        self._index = 0
        self._event = 0
        self._run = RunMonitor()

    def upload_page_add(self):
        return PAGE_ADD_MM

    def actuator_snapshot(self):
        with self._lock:
            return self._actuators.copy()

    def _status(self):
        return {"demo": True, "responded": True, "busy": False, "message": "Демо-контроллер на связи.",
                "lastEvent": self._last_event(), "uploadUncertain": False}

    def _last_event(self):
        return {"code": self._event, "label": EVENT_LABELS[self._event]}

    def _job_status(self):
        if not self._points:
            return {"jobLoaded": False}
        return {"jobLoaded": True, "currentIndex": self._index, "indexLimit": len(self._points)}

    def _position_status(self):
        clear = self._clearance()
        hold = {}
        for axis, (low, high) in self._limits.items():
            allowed = clear or axis == "z"
            hold[axis + "Minus"] = allowed and self._position[axis] > low
            hold[axis + "Plus"] = allowed and self._position[axis] < high
        return {"position": self._position.copy(), "fields": list(self._position.values()), "hold": hold,
                "operations": dict.fromkeys(("feed", "cut", "move-to", "upload"), clear),
                "motionInhibited": False}

    def _live(self):
        return {**self._status(), **self._position_status(), **self._job_status(), "autoRunOff": True,
                "manualSpeed": self._manual_speed, "run": self._run.snapshot(), "externalMotion": None,
                "stopAvailable": bool(self._motion), "coordinateMove": self._move_status(),
                "manualMovement": self._jog is not None,
                "actuators": self._actuators.copy(), "jobReceipt": self._receipt.value if self._receipt else None}

    def read_live(self):
        with self._lock:
            self._advance()
            return self._live()

    def read_status(self):
        with self._lock:
            self._advance()
            return {**self._status(), "fields": [0, 0, 0, 0, 0, self._event]}

    def read_position(self):
        with self._lock:
            self._advance()
            return {**self._status(), **self._position_status()}

    def read_job_index(self):
        with self._lock:
            self._advance()
            return {**self._status(), **self._job_status(), "fields": [self._index, 0, len(self._points)]}

    def read_feed_mode(self):
        with self._lock:
            self._advance()
            return {**self._status(), "feedMode": 0, "label": FEED_MODES[0]}

    def read_panel_snapshot(self, extended=False):
        with self._lock:
            return {"demo": True, "firmware": 0, "status": self.read_status()["fields"], "parameters": {}}

    def _observe(self):
        self._run.observe(self._event, EVENT_LABELS[self._event], (self._index, 0, len(self._points)), False)

    def _advance(self):
        now = monotonic()
        if self._jog is not None:
            jog = self._jog
            limit = min(jog.expires, self._control.expires)
            seconds = max(0, min(now, limit) - self._updated)
            minimum, maximum = self._limits[jog.axis]
            speed = 30 if self._manual_speed == "fast" else 10
            if jog.axis == "z":
                speed /= 3
            value = self._position[jog.axis] + jog.direction * speed * seconds
            self._position[jog.axis] = max(minimum, min(value, maximum))
            if now >= limit or value < minimum or value > maximum:
                self._finish_hold("expired" if now >= limit else "stopped")
        if self._motion is not None and self._run.state != "paused":
            motion = self._motion
            position, completed = motion.advance(max(0, now - self._updated))
            self._position["x"], self._position["y"] = position
            done = motion.elapsed >= motion.seconds
            if motion.action == "cut":
                self._index = min(completed + 1, len(self._points))
                self._position["z"] = 0.0 if done else float(self._tool_down[self._index - 1])
                if done:
                    self._event = 4
            if done:
                self._motion = None
                if motion.action == "move-to":
                    self._coordinate_move["state"] = "reached"
                    self._event = 48
                elif motion.action == "range":
                    self._event = 0
        self._updated = now
        if self._control is not None and now >= self._control.expires:
            self._control = None
            self._clear_receipt()
        self._observe()

    def _clearance(self):
        return abs(self._position["z"]) <= 0.03

    def _require_clearance(self):
        if not self._clearance():
            raise ControllerError("Поднимите виртуальный нож перед перемещением.")

    def _clear_receipt(self):
        self._receipt = None
        self._run.invalidate_ready()

    def _motion_idle(self):
        if self._motion is not None or self._jog is not None:
            raise ControllerBusyError("Дождитесь остановки в деморежиме.")

    def _move_status(self):
        return self._coordinate_move.copy() if self._coordinate_move else None

    def require_coordinate_move_idle(self):
        with self._lock:
            self._advance()
            if self._coordinate_move and self._coordinate_move["state"] == "moving":
                raise ControllerBusyError("Дождитесь перемещения к заданным координатам.")

    def _valid_control(self, token):
        return isinstance(token, str) and self._control is not None and self._control.token == token

    def control_is_armed(self):
        with self._lock:
            self._advance()
            return self._control is not None

    def require_control(self, token):
        with self._lock:
            self._advance()
            if not self._valid_control(token):
                raise ControllerError("Сеанс закончился. Включите управление в деморежиме.")

    def control_session(self, action, token=None, ready=None):
        with self._lock:
            self._advance()
            if action == "renew":
                if not self._valid_control(token):
                    return {"armed": False}
                self._control = ControlSession(token, self._updated + CONTROL_SESSION_SECONDS)
                return {"armed": True, "expiresIn": CONTROL_SESSION_SECONDS}
            if action == "disarm":
                if self._valid_control(token):
                    self._finish_hold("cancelled")
                    self._control = None
                    self._clear_receipt()
                return {"armed": False}
            if action == "verify-stop":
                self.require_control(token)
                self._motion_idle()
                if ready is not True:
                    raise ControllerError("Подтвердите остановку в деморежиме.")
                return {**self._live(), "armed": True}
            if action != "arm" or ready is not True:
                raise ControllerError("Подтвердите включение демоуправления.")
            if self._jog is not None:
                raise ControllerBusyError("Сначала отпустите кнопку движения.")
            self._clear_receipt()
            self._control = ControlSession(secrets.token_urlsafe(24), self._updated + CONTROL_SESSION_SECONDS)
            return {**self._live(), "controlToken": self._control.token, "expiresIn": CONTROL_SESSION_SECONDS}

    def _remember_hold(self, session, result):
        self._finished_holds[session] = result
        if len(self._finished_holds) > 128:
            self._finished_holds.popitem(last=False)
        return result.copy()

    def _finish_hold(self, phase):
        if self._jog is not None:
            session = self._jog.session
            self._jog = None
            return self._remember_hold(session, {"session": session, "phase": phase, "sent": True,
                                                "positionStable": True, **self._position_status()})

    def stop_active_hold(self):
        with self._lock:
            self._advance()
            self._finish_hold("cancelled")

    def hold(self, action, session, axis=None, direction=None, token=None, ready=None, control_token=None):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", session):
            raise ControllerError("Недопустимый идентификатор удержания.")
        if action not in ("start", "renew", "stop"):
            raise ControllerError("Неизвестное действие ручного движения.")
        with self._lock:
            self._advance()
            if action == "start":
                self.require_control(control_token)
            if session in self._finished_holds:
                return self._finished_holds[session].copy()
            if self._jog and self._jog.session == session:
                if action == "stop":
                    return self._finish_hold("released")
                if action == "renew":
                    self._jog.expires = self._updated + HOLD_LEASE_SECONDS
            elif action == "start":
                self._motion_idle()
                if not isinstance(axis, str) or axis not in self._limits or type(direction) is not int or abs(direction) != 1:
                    raise ControllerError("Выберите направление по X, Y, Z или W.")
                if not self._position_status()["hold"][axis + ("Plus" if direction > 0 else "Minus")]:
                    raise ControllerError("Направление движения сейчас недоступно.")
                self._jog = Jog(session, axis, direction, self._updated + HOLD_LEASE_SECONDS)
            else:
                result = {"session": session, "phase": "cancelled", "sent": False}
                return self._remember_hold(session, result) if action == "stop" else result
            return {"session": session, "phase": "holding", "sent": True}

    def upload_job(self, job, finalization, page_add, token=None, ready=None, auto_run_off=None, control_token=None,
                   *, filename=None):
        with self._lock:
            self.require_control(control_token)
            self._motion_idle()
            self._require_clearance()
            if page_add != self.upload_page_add():
                raise ControllerError("Параметры демозагрузки изменились. Подготовьте задание заново.")
            points, tool_down = [], []
            for chunk in job.chunks:
                for point, drawing in chunk.vertices():
                    points.append(point)
                    tool_down.append(drawing)
            if not points or len(points) != job.point_count:
                raise ControllerError("Не удалось прочитать точки демонстрационного задания.")
            verification_required = len(points) == len(self._points)
            self._points, self._tool_down = tuple(points), tuple(tool_down)
            self._index = 0
            self._receipt = JobReceipt(secrets.token_urlsafe(24), control_token, len(points), verification_required)
            self._event = 8
            result = {"uploaded": True, "points": len(points), "phase": "confirmed", "commitSent": True,
                      "verificationRequired": verification_required, "message": "Проход загружен в деморежиме."}
            self._run.begin_upload(filename)
            self._run.finish_upload(result)
            self._observe()
            return {**self._live(), **result, "baselineEvent": self._last_event()}

    def _xy(self):
        return self._position["x"], self._position["y"]

    def advance_material(self, distance_mm, *, control_token):
        with self._lock:
            self.require_control(control_token)
            self._motion_idle()
            self._require_clearance()
            maximum = self._limits["x"][1]
            if type(distance_mm) not in (int, float) or not 0 < distance_mm <= maximum:
                raise ControllerError(f"Укажите положительную длину протяжки до {maximum:g} мм.")
            self._material_offset_mm += distance_mm
            return {"feedMm": distance_mm, "materialOffsetMm": self._material_offset_mm}

    def _move_to(self, x, y):
        self._require_clearance()
        target = []
        for axis, value in (("x", x), ("y", y)):
            maximum = self._limits[axis][1]
            if type(value) not in (int, float) or not 0 <= value <= maximum:
                raise ControllerError(f"Координата {axis.upper()} должна быть от 0 до {maximum:g} мм.")
            target.append(value)
        distance = dist(self._xy(), target)
        self._coordinate_move = {"x": x, "y": y, "state": "moving" if distance else "reached"}
        if distance:
            self._motion = Motion("move-to", (self._xy(), tuple(target)), min(2.0, max(0.25, distance / 300)))
        return bool(distance)

    def _start_job(self, action, receipt, verified, message):
        if not self._receipt or receipt != self._receipt.value:
            raise ControllerError("Загрузите текущий проход в деморежиме.")
        if action == "cut":
            if verified is not True or self._run.state != "ready":
                raise ControllerError("Сверьте загруженное демозадание перед запуском.")
            route = (self._xy(), *self._points)
            distance = sum(dist(start, end) for start, end in pairwise(route))
            self._motion = Motion(action, route, min(30.0, max(8.0, distance / 250)))
            self._run.start(len(self._points), self._event, message, receipt)
            self._event = 3
        else:
            xs, ys = zip(*self._points)
            left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
            route = (self._xy(), (left, bottom), (right, bottom), (right, top), (left, top), (left, bottom))
            seconds = min(12.0, max(2.0, hypot(right - left, top - bottom) / 180))
            self._motion = Motion(action, route, seconds)
            self._event = 40

    def operate(self, action, token=None, ready=None, job_confirmed=None, recover_upload=None, control_token=None,
                job_receipt=None, job_verified=None, *, confirmed_stop=False, x=None, y=None):
        if not isinstance(action, str):
            raise ControllerError("Неизвестная операция демоконтроллера.")
        with self._lock:
            self._advance()
            if action != "cancel-cut" or not confirmed_stop:
                self.require_control(control_token)
            if action not in ("pause-cut", "resume-cut", "cancel-cut"):
                self._motion_idle()
            if action in OPERATIONS_REQUIRING_CLEARANCE:
                self._require_clearance()
            message = f"{OPERATION_LABELS.get(action, 'Перемещение')} в деморежиме."
            sent = True
            if action in ("cut", "range"):
                self._start_job(action, job_receipt, job_verified, message)
            elif action == "pause-cut" and self._run.state == "running":
                self._event = 9
            elif action == "resume-cut" and self._run.state == "paused":
                self._event = 3
            elif action == "cancel-cut":
                if not self._motion and not self._jog:
                    raise ControllerError("Активное движение в деморежиме не подтверждено.")
                self._finish_hold("cancelled")
                if self._motion and self._motion.action == "move-to":
                    self._coordinate_move = None
                self._motion = None
                self._position["z"] = 0.0
                self._event = 5
                self._clear_receipt()
            elif action == "move-to":
                sent = self._move_to(x, y)
            elif action == "home":
                self._position = dict.fromkeys(AXES, 0.0)
                self._coordinate_move = None
                self._clear_receipt()
                self._event = 2
            elif action == "set-zero":
                self._zero = self._xy()
                self._clear_receipt()
                self._event = 11
            elif action == "go-zero":
                self._move_to(*self._zero)
            elif action == "feed":
                self._event = 49
            elif action == "manual-speed":
                self._manual_speed = "slow" if self._manual_speed == "fast" else "fast"
            elif action not in ACTUATOR_COMMAND_EFFECTS:
                raise ControllerError("Операция недоступна в текущем состоянии деморежима.")
            changes = ACTUATOR_COMMAND_EFFECTS.get(action)
            if changes is not None:
                self._actuators.update(changes)
                self._actuators["revision"] += 1
            if action in IDLE_CANCEL_OPERATIONS:
                self._position["z"] = 0.0
                self._clear_receipt()
            self._observe()
            return {**self._live(), "operation": action, "sent": sent, "message": message,
                    **({"actuatorChanges": changes.copy()} if changes is not None else {})}
