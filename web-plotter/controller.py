import os
import re
import secrets
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Lock, Thread
from time import monotonic, sleep

from run_monitor import FAULT_EVENTS, FRESHNESS_TIMEOUT, RunMonitor
from xiao_job import JobTransferError, transfer_job


CONTROLLER_PORT = 1234
STATUS_REPLY = re.compile(rb"\((\d{1,10}(?:,\d{1,10}){5})\)")
POSITION_REPLY = re.compile(rb"\((-?\d{1,10}(?:,-?\d{1,10}){4})\)")
JOB_INDEX_REPLY = re.compile(rb"\((-?\d{1,10}(?:,-?\d{1,10}){2,9})\)")
BOARD_INFO_REPLY = re.compile(rb"\(-?\d{1,10},-?\d{1,10},(\d{1,10})\)")
FEED_CONFIG_REPLY = re.compile(rb"par\((1(?:,-?\d{1,10}){3,40})\)")
FEED_LAYOUT_REPLY = re.compile(rb"par\((18(?:,-?\d{1,10}){4,40})\)")
AXES = ("x", "y", "z", "w", "a")
TICKS_PER_MM = 100
POSITION_TOLERANCE = 3
PILOT_TOKEN_SECONDS = 60
CONTROL_SESSION_SECONDS = 900
FIRMWARE_WITH_FINAL_ACK = 6220406
HOLD_LEASE_SECONDS = 0.60
HOLD_START_SECONDS = 2.0
HOLD_RELEASE_SECONDS = 1.85
HOLD_SETTLE_SECONDS = 1.35
HOLD_POSITION_INTERVAL_SECONDS = 0.12
HOLD_RECOVERY_MAX_GAP_SECONDS = 5.0
COORDINATE_MOVE_TIMEOUT_SECONDS = 120.0
QUERY_TIMEOUT_SECONDS = 0.45
JOG_LIMIT_TICKS = 500 * TICKS_PER_MM  # Native Xiao panel default.
JOG_CODES = {("x", 1): 1010, ("x", -1): 1011, ("y", 1): 1012, ("y", -1): 1013,
             ("z", 1): 1014, ("z", -1): 1015, ("w", 1): 1016, ("w", -1): 1017}
JOG_STOP = b"cmd(1004)\x00"
COORDINATE_MOVE_CODE = 1036
OPERATION_CODES = {"feed": 1048, "cut": 1002, "cancel-cut": 1006, "pause-cut": 1005,
                   "resume-cut": 1005,
                   "vacuum": 1047, "vacuum-off": 1047, "clamp": 1055, "raise-clamp": 1006,
                   "home": 1003, "set-zero": 1008,
                   "go-zero": 1040, "range": 1009, "manual-speed": 1056}
OPERATION_LABELS = {"feed": "Подача ткани", "cut": "Запуск реза", "cancel-cut": "Отмена реза",
                    "pause-cut": "Пауза", "resume-cut": "Продолжение реза", "vacuum": "Вакуум", "clamp": "Зажим",
                    "vacuum-off": "Отключение вакуума",
                    "raise-clamp": "Подъём лапок",
                    "home": "Базирование", "set-zero": "Запись рабочего нуля", "go-zero": "К рабочему нулю",
                    "range": "Обход задания", "manual-speed": "Переключение скорости"}
OPERATIONS_DURING_MOTION = frozenset(("pause-cut", "resume-cut", "cancel-cut"))
CUT_MOTION_EVENTS = {3: "running", 9: "paused", 50: "feeding"}
ACTIVE_CONTROLLER_EVENTS = frozenset((1, 3, 7, 9, 22, 23, 25, 26, 40, 50))
EXTERNAL_CUT_EVENTS = frozenset((3, 9))
IDLE_CANCEL_OPERATIONS = frozenset(("raise-clamp",))
# Expected effects of successfully sent commands, not sensor readback.
# Clamp release uses the operator-approved Cancel sequence, including Z-up.
ACTUATOR_COMMAND_EFFECTS = {
    "clamp": {"clampLowered": True},
    "raise-clamp": {"clampLowered": False, "vacuumEnabled": False},
    "cancel-cut": {"clampLowered": False, "vacuumEnabled": False},
    "vacuum": {"vacuumEnabled": True},
    "vacuum-off": {"vacuumEnabled": False},
}
OPERATIONS_REQUIRING_CLEARANCE = frozenset(("feed", "cut", "go-zero", "range"))
FEED_MODES = {0: "Подача отключена", 1: "Подача с зажимом", 2: "Моторная подача", 3: "Моторная подача, режим 2"}
FEED_LAYOUTS = {0: "Подача балкой не назначена", 1: "Подача одной балкой", 2: "Общая подача двумя балками",
                3: "Раздельная подача двумя балками"}
EVENT_LABELS = {
    0: "Готовность",
    1: "Начато базирование",
    2: "Базирование завершено",
    3: "Начата обработка",
    4: "Обработка завершена",
    5: "Отмена обработки",
    7: "Начато получение задания",
    8: "Задание получено",
    9: "Пауза",
    11: "Рабочий ноль установлен",
    22: "Начато базирование X",
    23: "Начато базирование Y",
    25: "Начато базирование W",
    26: "Начато базирование Z",
    30: "Базирование X завершено",
    31: "Базирование Y завершено",
    33: "Базирование W завершено",
    34: "Базирование Z завершено",
    40: "Обход габаритов задания",
    48: "Достигнута цель перемещения",
    49: "Подача завершена",
    50: "Начата подача",
    101: "Ошибка датчика базирования",
    104: "Требуется базирование",
    109: "X− за пределами поля",
    110: "X+ за пределами поля",
    111: "Y− за пределами поля",
    112: "Y+ за пределами поля",
    115: "Рабочий ноль не установлен",
    126: "Ошибка базирования X",
    127: "Ошибка базирования Y",
    129: "Ошибка базирования W",
    130: "Ошибка базирования Z",
    134: "Аварийный стоп",
    135: "Нажмите базирование на пульте",
    136: "Ошибка скорости",
    140: "Вторая головка слишком близко",
}


class ControllerError(ValueError):
    pass


class ControllerBusyError(ControllerError):
    pass


def coordinates(fields):
    return {axis: value / TICKS_PER_MM for axis, value in zip(AXES, fields)}


def positions_match(first, second, tolerance=POSITION_TOLERANCE):
    return all(abs(left - right) <= tolerance for left, right in zip(first, second))


@dataclass
class Hold:
    session: str
    expires: float
    phase: str = "starting"
    reason: str | None = None
    result: dict | None = None
    control_token: str | None = None


@dataclass
class CoordinateMove:
    target: tuple[int, int]
    deadline: float
    last_response: float
    baseline: int
    state: str = "moving"
    sample: tuple[list[int], float] | None = None

    def expire(self, now):
        if self.state == "moving" and (now >= self.deadline or now - self.last_response > FRESHNESS_TIMEOUT):
            self.state = "unconfirmed"
            self.sample = None

    def snapshot(self):
        return {"state": self.state, **coordinates(self.target)}


@dataclass(frozen=True)
class ControlSession:
    token: str
    expires: float


@dataclass(frozen=True)
class JobReceipt:
    value: str
    control_token: str
    points: int
    verification_required: bool


class Controller:
    def __init__(self, ip, width=2400, height=1750, upload_profile=None, z_travel=0,
                 auto_run_readback=None, manual_speed_readback=None):
        self.endpoint = (ip, CONTROLLER_PORT)
        self.width = width
        self.height = height
        self.z_travel = z_travel
        self.lock = Lock()
        self._pilot = None
        self._hold_condition = Condition()
        self._active_hold = None
        self._finished_holds = {}
        self._motion_inhibited = False
        self._stop_can_recover = False
        self._stop_recovery_sample = None
        self._coordinate_move = None
        self._upload_profile = upload_profile
        self.auto_run_readback = auto_run_readback
        self.manual_speed_readback = manual_speed_readback
        self._upload_state = "idle"
        self._control = None
        self._job_receipt = None
        self._run = RunMonitor()
        self._reported_cut_event = None
        self._reported_cut_at = None
        self._actuators = {
            "epoch": secrets.token_urlsafe(16),
            "revision": 0,
            "clampLowered": None,
            "vacuumEnabled": None,
        }

    def upload_page_add(self):
        return self._upload_profile() if self._upload_profile is not None else None

    def actuator_snapshot(self):
        """Return effects of commands sent by this server, without querying the machine."""
        with self._hold_condition:
            return self._actuators.copy()

    @property
    def upload_uncertain(self):
        return self._upload_state == "uncertain"

    @property
    def upload_in_progress(self):
        return self._upload_state == "transferring"

    def read_status(self):
        with self._io():
            result = self._read(b"sts\x00", STATUS_REPLY, "статуса")
        if "fields" in result:
            code = result["fields"][5]
            result["lastEvent"] = {"code": code, "label": EVENT_LABELS.get(code)}
        result["uploadUncertain"] = self.upload_uncertain
        return result

    def read_job_index(self):
        with self._io():
            result = self._read(b"idx\x00", JOB_INDEX_REPLY, "индекса задания")
        return self._job_index_payload(result)

    @staticmethod
    def _job_index_payload(result):
        if "fields" in result:
            index, _, limit = result["fields"][:3]
            result["jobLoaded"] = limit >= 1
            if limit >= 1:
                result["indexLimit"] = limit
                if 0 <= index <= limit:
                    result["currentIndex"] = index
            else:
                result["message"] = "Контроллер не сообщает о загруженном задании."
        return result

    @staticmethod
    def _matches_job_index(fields, points, at_start=False):
        if not fields:
            return False
        index, _, limit = fields[:3]
        return limit == points and (index == 0 if at_start else 0 <= index <= limit)

    def _clear_job_receipt(self, uncertain=False):
        if self._job_receipt is not None:
            if uncertain:
                self._upload_state = "uncertain"
            self._run.invalidate_ready()
        self._job_receipt = None

    def _report_job_receipt(self, result, fields=None):
        with self._hold_condition:
            receipt = self._job_receipt
            result["jobReceipt"] = None
            if receipt is not None:
                if (self._control is None or self._control.token != receipt.control_token
                        or self._control.expires <= monotonic()):
                    self._clear_job_receipt()
                elif self._matches_job_index(fields, receipt.points):
                    result["jobReceipt"] = receipt.value
                else:
                    self._clear_job_receipt(uncertain=True)
            result["uploadUncertain"] = self.upload_uncertain

    def _cut_motion_snapshot(self):
        motion = CUT_MOTION_EVENTS.get(self._reported_cut_event)
        return {"externalMotion": None if self._run.blocks_machine else motion,
                "stopAvailable": self._run.state != "cancelling"
                and (self._run.blocks_machine or self._recent_external_cut())}

    def _recent_external_cut(self):
        return (self._reported_cut_event in EXTERNAL_CUT_EVENTS
                and monotonic() - self._reported_cut_at <= FRESHNESS_TIMEOUT)

    def _report_cut_event(self, event):
        if event is not None:
            self._reported_cut_event = event
            self._reported_cut_at = monotonic()

    def read_live(self):
        ip, port = self.endpoint
        result = {"ip": ip, "port": port, "responded": False, "busy": False,
                  "uploadUncertain": self.upload_uncertain, "manualSpeed": None}
        try:
            with self._io():
                try:
                    with self._client() as client:
                        fields, pose = self._read_live_fields(client, result)
                except (ControllerError, OSError) as error:
                    result.update(responded=False, message=str(error) if isinstance(error, ControllerError)
                                  else "Не удалось связаться с контроллером.")
                    result.pop("lastEvent", None)
                    fields, pose = None, None
                with self._hold_condition:
                    last_event = result.get("lastEvent", {})
                    event = last_event.get("code")
                    self._report_cut_event(event)
                    if not result["busy"]:
                        self._report_job_receipt(result, fields if "position" in result else None)
                        if event in EXTERNAL_CUT_EVENTS and self._run.state == "ready" and result["jobReceipt"]:
                            self._run.observe_panel_start(event, last_event["label"], result["jobReceipt"])
                    self._run.observe(event, last_event.get("label"), fields, result["busy"])
                    self._observe_stop_recovery(pose if not result["busy"] else None)
                    self._observe_coordinate_move(event, pose, result["busy"] and result["responded"])
                    result["coordinateMove"] = self._coordinate_move_snapshot()
                    result["run"] = self._run.snapshot()
                    result.update(self._cut_motion_snapshot())
                    result["motionInhibited"] = self._motion_inhibited
        except ControllerBusyError as error:
            with self._hold_condition:
                self._stop_recovery_sample = None
                result.update(busy=True, message=str(error), run={**self._run.snapshot(), "fresh": False},
                              motionInhibited=self._motion_inhibited)
                result.update(self._cut_motion_snapshot())
                result["coordinateMove"] = self._coordinate_move_snapshot()
                result["run"].pop("index", None)
                result["run"].pop("indexLimit", None)
        result["actuators"] = self.actuator_snapshot()
        return result

    def _read_live_fields(self, client, result):
        status = self._query(client, b"sts\x00", STATUS_REPLY, "статуса")
        result.update(responded=status["responded"], busy=status["busy"], message=status["message"])
        if "fields" not in status:
            return None, None
        event = status["fields"][5]
        result["lastEvent"] = {"code": event, "label": EVENT_LABELS.get(event)}
        pose = self._query(client, b"pos\x00", POSITION_REPLY, "координат")
        if pose["busy"]:
            result.update(busy=True, message=pose["message"])
            return None, None
        sample = None
        if "fields" in pose:
            sample = (pose["fields"], monotonic())
            result.update(self._position_payload(pose["fields"], issue_pilot=False))
        job = self._job_index_payload(self._query(client, b"idx\x00", JOB_INDEX_REPLY, "индекса задания"))
        if job["busy"]:
            result.update(busy=True, message=job["message"])
            return None, None
        for key in ("jobLoaded", "currentIndex", "indexLimit"):
            if key in job:
                result[key] = job[key]
        if event in CUT_MOTION_EVENTS:
            return job.get("fields"), sample
        read_auto_run = not status["fields"][1] and self.auto_run_readback is not None
        if read_auto_run or self.manual_speed_readback is not None:
            firmware = self._read_firmware(client)
            if read_auto_run:
                result["autoRunOff"] = self.auto_run_readback.read(client, self._exchange, firmware, status["fields"])
            if self.manual_speed_readback is not None:
                result["manualSpeed"] = self.manual_speed_readback.read(client, self._exchange, firmware, status["fields"])
        return job.get("fields"), sample

    def _observe_stop_recovery(self, sample):
        if (not self._motion_inhibited or not self._stop_can_recover or self._active_hold
                or self._run.blocks_machine or self._reported_cut_event in CUT_MOTION_EVENTS):
            self._stop_recovery_sample = None
            return
        previous = self._stop_recovery_sample
        if sample and previous and positions_match(previous[0], sample[0]):
            elapsed = sample[1] - previous[1]
            if HOLD_POSITION_INTERVAL_SECONDS <= elapsed <= HOLD_RECOVERY_MAX_GAP_SECONDS:
                self._clear_motion_inhibit()
                return
            if elapsed < HOLD_POSITION_INTERVAL_SECONDS:
                return
        self._stop_recovery_sample = sample

    def _clear_motion_inhibit(self):
        self._motion_inhibited = False
        self._stop_can_recover = False
        self._stop_recovery_sample = None

    def _coordinate_move_snapshot(self):
        move = self._coordinate_move
        if move is None:
            return None
        move.expire(monotonic())
        return move.snapshot()

    def require_coordinate_move_idle(self):
        with self._hold_condition:
            move = self._coordinate_move
            if move is None:
                return
            move.expire(monotonic())
            if move.state == "moving":
                raise ControllerError("Дождитесь завершения перемещения к заданным координатам.")
            if move.state == "unconfirmed":
                raise ControllerError("Перемещение не подтверждено. Проверьте остановку на штатном пульте.")

    def _observe_coordinate_move(self, event, sample, busy):
        move = self._coordinate_move
        if move is None or move.state == "reached":
            return
        now = monotonic()
        move.expire(now)
        if sample is not None or busy:
            move.last_response = now
        if event in ACTIVE_CONTROLLER_EVENTS or (event in FAULT_EVENTS and event != move.baseline):
            move.state = "unconfirmed"
            move.sample = None
            return
        if busy or sample is None or tuple(sample[0][:2]) != move.target or not self._clearance(sample[0]):
            move.sample = None
            return
        if move.sample and positions_match(move.sample[0], sample[0]):
            elapsed = sample[1] - move.sample[1]
            if elapsed < HOLD_POSITION_INTERVAL_SECONDS:
                return
            if elapsed <= HOLD_RECOVERY_MAX_GAP_SECONDS:
                move.state = "reached"
                move.sample = None
                return
        move.sample = sample

    def _read_firmware(self, client):
        try:
            board, busy = self._exchange(client, b"dog\x00", BOARD_INFO_REPLY)
            if board is not None and not busy:
                return int(board[1])
        except OSError:
            pass
        return None

    def control_session(self, action, token=None, ready=None, auto_run_off=None):
        if action == "renew":
            with self._hold_condition:
                try:
                    self.require_control(token)
                except ControllerError:
                    return {"armed": False}
                self._control = ControlSession(token, monotonic() + CONTROL_SESSION_SECONDS)
            return {"armed": True, "expiresIn": CONTROL_SESSION_SECONDS}
        if action == "verify-stop":
            return self._verify_stop(token, ready)
        if action == "disarm":
            with self._hold_condition:
                if self._control is not None and self._control.token == token:
                    self._control = None
                    self._clear_job_receipt()
                    hold = self._active_hold
                    if hold is not None and hold.control_token == token:
                        hold.reason = hold.reason or "cancelled"
                        self._hold_condition.notify_all()
            return {"armed": False}
        if action != "arm" or ready is not True:
            raise ControllerError("Подтвердите, что вы у станка и зона движения свободна.")
        result = self.read_live()
        if not result["responded"]:
            raise ControllerError(result["message"])
        with self._hold_condition:
            if self._active_hold is not None:
                raise ControllerError("Сначала отпустите кнопку ручного движения.")
            self._clear_job_receipt()
            self._control = ControlSession(secrets.token_urlsafe(24), monotonic() + CONTROL_SESSION_SECONDS)
            return {**result, "controlToken": self._control.token, "expiresIn": CONTROL_SESSION_SECONDS,
                    "jobReceipt": None,
                    "motionInhibited": self._motion_inhibited}

    def _verify_stop(self, token, ready):
        self.require_control(token)
        if ready is not True:
            raise ControllerError("Подтвердите на штатном пульте, что станок остановлен.")
        try:
            with self._io():
                with self._hold_condition:
                    if self._active_hold is not None:
                        raise ControllerError("Дождитесь остановки ручного движения.")
                    move = self._coordinate_move_snapshot()
                    if move is not None and move["state"] == "moving":
                        raise ControllerError("Дождитесь подтверждения целевой позиции.")
                    verify_coordinate_move = move is not None and move["state"] == "unconfirmed"
                with self._client() as client:
                    pose, _ = self._preflight(client, None, require_clearance=False,
                                              settle_interval=HOLD_POSITION_INTERVAL_SECONDS,
                                              require_idle_controller=verify_coordinate_move)
                with self._hold_condition:
                    self.require_control(token)
                    if self._active_hold is not None:
                        raise ControllerError("Дождитесь остановки ручного движения.")
                    if verify_coordinate_move:
                        self._coordinate_move = None
                    self._clear_motion_inhibit()
                    return {"armed": True, "motionInhibited": False,
                            "coordinateMove": self._coordinate_move_snapshot(),
                            **self._position_payload(pose, issue_pilot=False),
                            "message": "Координаты стабильны. Управление доступно."}
        except OSError as error:
            raise ControllerError("Не удалось проверить остановку. Проверьте станок на штатном пульте.") from error

    def control_is_armed(self):
        with self._hold_condition:
            return self._control is not None and monotonic() < self._control.expires

    def require_control(self, token, for_upload=False, auto_run_off=None):
        with self._hold_condition:
            session = self._control
            if (not isinstance(token, str) or session is None or session.token != token
                    or monotonic() >= session.expires):
                raise ControllerError("Сеанс закончился. Включите управление станком.")
            if for_upload and self.auto_run_readback is None and auto_run_off is not True:
                raise ControllerError("Для передачи подтвердите, что Auto Run выключен на штатном пульте.")

    def _require_job_receipt(self, token, value, action, job_verified):
        receipt = self._job_receipt
        if (receipt is None or receipt.control_token != token
                or not isinstance(value, str) or value != receipt.value):
            raise ControllerError("Загрузите текущий чертёж заново и проверьте задание на штатном пульте.")
        if action == "cut" and receipt.verification_required and job_verified is not True:
            raise ControllerError("Число точек совпадает с предыдущим заданием. Сверьте новое задание на штатном пульте.")
        return receipt

    def _require_idle_run(self):
        self._require_motion_available()
        if self.upload_in_progress:
            raise ControllerError("Дождитесь завершения передачи задания.")
        if self._run.blocks_machine:
            raise ControllerError("Завершите текущий раскрой и проверьте состояние станка.")
        if self._reported_cut_event in CUT_MOTION_EVENTS:
            raise ControllerError("Дождитесь окончания раскроя или подачи на штатном пульте.")

    def _require_motion_available(self):
        if self._motion_inhibited:
            raise ControllerError("Остановка ручного движения не подтверждена. Проверьте станок на штатном пульте.")
        self.require_coordinate_move_idle()

    def _observe_preflight(self, event, fields=None):
        self._report_cut_event(event)
        self._run.observe(event, EVENT_LABELS.get(event), fields, busy=False)

    def read_panel_snapshot(self, extended=False):
        """Read controller identity, current status and documented parameter groups."""
        try:
            with self._io(), self._client() as client:
                with self._hold_condition:
                    if self._run.blocks_machine or self.upload_in_progress or self._active_hold is not None:
                        raise ControllerBusyError("Дождитесь остановки станка перед проверкой пульта.")
                status = self._query(client, b"sts\x00", STATUS_REPLY, "статуса")
                if "fields" not in status:
                    raise ControllerError(status["message"])
                board, busy = self._exchange(client, b"dog\x00", BOARD_INFO_REPLY, timeout=0.65)
                if busy or board is None:
                    raise ControllerError("Плата не сообщила версию. Повторите снимок.")
                parameters = {}
                hello, busy = self._exchange(client, b"gpr(0)\x00", re.compile(rb"ok"), timeout=0.65)
                if busy:
                    raise ControllerBusyError("Дождитесь остановки станка и повторите снимок.")
                if hello is not None:
                    # Xiao reads groups 1–17 in its native Get from machine path
                    # and 18–19 on supported controllers. Skip unanswered groups.
                    groups = range(1, 20) if extended else (1, 4, 18, 19)
                    for group in groups:
                        key = str(group)
                        wire_group = key.encode("ascii")
                        pattern = re.compile(rb"par\((" + wire_group + rb"(?:,-?[0-9]{1,10}){1,9})\)")
                        match, busy = self._exchange(client, b"gpr(" + wire_group + b")\x00", pattern,
                                                     timeout=0.25 if extended else QUERY_TIMEOUT_SECONDS)
                        if busy:
                            raise ControllerBusyError("Контроллер занят. Повторите снимок.")
                        if match is not None:
                            fields = [int(value) for value in match[1].split(b",")]
                            if group == 1 and len(fields) > 2:
                                fields[2] = 0  # Native group 1 includes the board authorization code.
                            parameters[key] = fields
                return {"firmware": int(board[1]), "status": status["fields"], "parameters": parameters}
        except OSError as error:
            raise ControllerError("Не удалось прочитать данные платы.") from error

    def read_feed_mode(self):
        ip, port = self.endpoint
        result = {"ip": ip, "port": port, "responded": False, "busy": False}
        try:
            with self._io(), self._client() as client:
                hello, busy = self._exchange(client, b"gpr(0)\x00", re.compile(rb"ok"), timeout=1.0)
                if not hello:
                    return {**result, "responded": busy, "busy": busy,
                            "message": "Контроллер занят." if busy else "Контроллер не подтвердил чтение параметров."}
                mode_reply, busy = self._exchange(client, b"gpr(1)\x00", FEED_CONFIG_REPLY, timeout=1.0)
                if not mode_reply:
                    return {**result, "responded": busy, "busy": busy,
                            "message": "Контроллер занят." if busy else "Режим привода подачи не получен."}
                code = int(mode_reply[1].split(b",")[3])
                try:
                    layout_reply, _ = self._exchange(client, b"gpr(18)\x00", FEED_LAYOUT_REPLY)
                except OSError:
                    layout_reply = None
        except (OSError, ControllerError) as error:
            return {**result, "message": str(error) if isinstance(error, ControllerError) else "Не удалось прочитать режим подачи."}
        result.update(responded=True, feedMode=code, label=FEED_MODES.get(code, f"Режим подачи {code}"),
                      message="Настройка привода подачи считана.")
        if layout_reply:
            layout = int(layout_reply[1].split(b",")[4])
            result.update(feedLayout=layout, layoutLabel=FEED_LAYOUTS.get(layout))
        return result

    def read_position(self):
        with self._io():
            result = self._read(b"pos\x00", POSITION_REPLY, "координат")
            self._pilot = None
            if "fields" in result:
                result.update(self._position_payload(result["fields"]))
            result["uploadUncertain"] = self.upload_uncertain
            return result

    def _position_payload(self, fields, issue_pilot=True):
        position = coordinates(fields)
        safe_position = self._clearance(fields)
        upload_supported = self.upload_page_add() is not None
        result = {"position": position, "fields": list(position.values()), "hold": self._hold_limits(fields),
                  "uploadUncertain": self.upload_uncertain,
                  "operations": {"feed": safe_position, "cut": safe_position, "move-to": safe_position,
                                 "upload": safe_position and upload_supported}}
        if not upload_supported:
            result["uploadUnavailableReason"] = "Прямая загрузка требует проверенного профиля одного листа Xiao."
        elif not safe_position:
            result["uploadUnavailableReason"] = "Проверьте поднятый нож и координаты в пределах рабочего поля."
        if safe_position and issue_pilot:
            token = secrets.token_urlsafe(24)
            self._pilot = (token, fields, monotonic())
            result.update(pilotToken=token, pilotTokenExpiresIn=PILOT_TOKEN_SECONDS)
        elif not safe_position:
            result["holdUnavailableReason"] = result["operationUnavailableReason"] = (
                "Проверьте поднятый нож и координаты в пределах рабочего поля."
            )
        return result

    def _clearance(self, fields):
        x, y, z, _, _ = fields
        return (
            abs(z) <= POSITION_TOLERANCE
            and -POSITION_TOLERANCE <= x <= self.width * TICKS_PER_MM + POSITION_TOLERANCE
            and -POSITION_TOLERANCE <= y <= self.height * TICKS_PER_MM + POSITION_TOLERANCE
        )

    def _hold_limit(self, fields, axis, direction):
        if axis == "z":
            return int(max(0, min(self.z_travel * TICKS_PER_MM, JOG_LIMIT_TICKS)))
        if axis == "w":
            return JOG_LIMIT_TICKS if abs(fields[2]) <= POSITION_TOLERANCE else 0
        if not self._clearance(fields):
            return 0
        index = 0 if axis == "x" else 1
        boundary = (self.width if index == 0 else self.height) * TICKS_PER_MM
        available = boundary - fields[index] if direction > 0 else fields[index]
        return int(max(0, min(available, JOG_LIMIT_TICKS)))

    def _hold_limits(self, fields):
        return {
            axis + label: self._hold_limit(fields, axis, direction) > POSITION_TOLERANCE
            for axis in ("x", "y", "z", "w")
            for label, direction in (("Minus", -1), ("Plus", 1))
        }

    @contextmanager
    def _io(self):
        if not self.lock.acquire(timeout=0.5):
            raise ControllerBusyError("Контроллер занят другим запросом. Отпустите кнопку движения.")
        try:
            yield
        finally:
            self.lock.release()

    def _consume_position(self, token):
        pilot = self._pilot
        if (not isinstance(token, str) or not pilot or token != pilot[0]
                or monotonic() - pilot[2] > PILOT_TOKEN_SECONDS):
            raise ControllerError("Считайте координаты станка заново.")
        self._pilot = None
        return pilot[1]

    def _preflight(self, client, reference, require_clearance=True, settle_interval=0.08, *,
                   require_idle_controller=False):
        status = self._query(client, b"sts\x00", STATUS_REPLY, "статуса")
        if "fields" not in status:
            raise ControllerError(status["message"])
        event = status["fields"][5]
        with self._hold_condition:
            self._report_cut_event(event)
        if event in CUT_MOTION_EVENTS:
            raise ControllerError("Дождитесь окончания раскроя или подачи на штатном пульте.")
        if require_idle_controller and event in ACTIVE_CONTROLLER_EVENTS:
            raise ControllerError("Дождитесь завершения текущей операции на станке.")
        first = self._position_fields(client)
        sleep(settle_interval)
        current = self._position_fields(client)
        if (not positions_match(first, current) or (reference is not None
                and not (positions_match(reference, first) and positions_match(reference, current)))):
            raise ControllerError("Координаты изменились. Считайте их снова после остановки.")
        if require_clearance and not self._clearance(current):
            raise ControllerError("Проверьте поднятый нож и положение в пределах рабочего поля.")
        return current, event

    def hold(self, action, session, axis=None, direction=None, token=None, ready=None, control_token=None):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", session):
            raise ControllerError("Недопустимый идентификатор удержания.")
        with self._hold_condition:
            self._prune_finished()
            if action == "start":
                self._require_idle_run()
                if control_token is not None:
                    self.require_control(control_token)
                elif ready is not True:
                    raise ControllerError("Подтвердите готовность у физического стопа.")
                if not isinstance(axis, str) or type(direction) is not int or (axis, direction) not in JOG_CODES:
                    raise ControllerError("Выберите направление по X, Y, Z или W.")
                if session in self._finished_holds:
                    return self._finished_holds[session][1]
                if self._active_hold is not None:
                    raise ControllerError("Сначала отпустите предыдущую кнопку движения.")
                hold = Hold(session, monotonic() + HOLD_START_SECONDS, control_token=control_token)
                self._active_hold = hold
                Thread(target=self._run_hold, args=(hold, token, axis, direction), daemon=True).start()
                self._hold_condition.wait_for(
                    lambda: hold.phase != "starting" or hold.result is not None,
                    timeout=HOLD_START_SECONDS,
                )
                if hold.result is not None:
                    return hold.result
                if hold.phase == "holding":
                    return {"session": session, "phase": "holding", "sent": True,
                            "message": "Команда движения отправлена. Отпустите кнопку для остановки."}
                hold.reason = hold.reason or "cancelled"
                self._hold_condition.notify_all()
                return {"session": session, "phase": "cancelled", "sent": False,
                        "message": "Проверка не завершилась вовремя. Движение не запущено."}
            if action not in ("renew", "stop"):
                raise ControllerError("Неизвестное действие ручного движения.")
            hold = self._active_hold
            if hold is None or hold.session != session:
                if session in self._finished_holds:
                    return self._finished_holds[session][1]
                result = {"session": session, "phase": "cancelled", "sent": False,
                          "message": "Удержание завершено до запуска."}
                if action == "stop":
                    self._remember_hold(session, result)
                return result
            if action == "renew":
                if hold.control_token is not None:
                    try:
                        self.require_control(hold.control_token)
                    except ControllerError:
                        hold.reason = hold.reason or "cancelled"
                        self._hold_condition.notify_all()
                if hold.phase == "holding" and not hold.reason and monotonic() < hold.expires:
                    hold.expires = monotonic() + HOLD_LEASE_SECONDS
                    self._hold_condition.notify_all()
                    return {"session": session, "phase": "holding", "sent": True}
                if not hold.reason and monotonic() >= hold.expires:
                    hold.reason = "expired"
                    self._hold_condition.notify_all()
                return hold.result or {"session": session, "phase": hold.reason or hold.phase}
            hold.reason = hold.reason or "released"
            self._hold_condition.notify_all()
            self._hold_condition.wait_for(lambda: hold.result is not None, timeout=HOLD_RELEASE_SECONDS)
            return hold.result or {"session": session, "phase": "released", "sent": hold.phase == "holding",
                                   "motionInhibited": self._motion_inhibited or hold.phase == "holding",
                                   "message": "Остановка запрошена. Проверьте положение на пульте."}

    def _prune_finished(self):
        now = monotonic()
        for session, (expires, _) in tuple(self._finished_holds.items()):
            if expires <= now:
                del self._finished_holds[session]

    def _remember_hold(self, session, result):
        self._finished_holds[session] = (monotonic() + PILOT_TOKEN_SECONDS, result)

    def _run_hold(self, hold, token, axis, direction):
        sent = False
        result = {"session": hold.session, "phase": "cancelled", "sent": False,
                  "message": "Кнопка отпущена до запуска."}
        try:
            with self._io(), self._client() as client:
                with self._hold_condition:
                    if hold.reason:
                        return
                reference = self._consume_position(token) if hold.control_token is None else None
                if axis in ("x", "y"):
                    before, baseline = self._preflight(client, reference)
                else:
                    before, baseline = self._preflight(client, reference, require_clearance=False)
                travel = self._hold_limit(before, axis, direction)
                if travel <= POSITION_TOLERANCE:
                    raise ControllerError("В этом направлении достигнута граница рабочего поля.")
                packet = f"cmd({JOG_CODES[(axis, direction)]},{travel})\x00".encode("ascii")
                try:
                    with self._hold_condition:
                        if hold.reason or monotonic() >= hold.expires:
                            return
                        if hold.control_token is not None:
                            self.require_control(hold.control_token)
                        self._observe_preflight(baseline)
                        self._require_idle_run()
                        client.sendto(packet, self.endpoint)
                        sent = True
                        hold.phase = "holding"
                        hold.expires = monotonic() + HOLD_LEASE_SECONDS
                        self._hold_condition.notify_all()
                        while not hold.reason:
                            remaining = hold.expires - monotonic()
                            if remaining <= 0:
                                hold.reason = "expired"
                                break
                            self._hold_condition.wait(timeout=remaining)
                finally:
                    if sent:
                        result = self._send_hold_stop(client, hold)
        except (ControllerError, OSError) as error:
            message = str(error) if isinstance(error, ControllerError) else "Обмен с контроллером прерван."
            result = {"session": hold.session, "phase": "failed", "sent": sent, "message": message}
        finally:
            with self._hold_condition:
                if sent and result.get("positionStable") is not True:
                    self._motion_inhibited = True
                    self._stop_can_recover = result.get("stopSent") is True
                    self._stop_recovery_sample = None
                    self._pilot = None
                result["motionInhibited"] = self._motion_inhibited
                hold.result = result
                hold.phase = result["phase"]
                self._remember_hold(hold.session, result)
                self._active_hold = None
                self._hold_condition.notify_all()

    def _send_hold_stop(self, client, hold):
        stop_sent = False
        for _ in range(3):
            try:
                client.sendto(JOG_STOP, self.endpoint)
                stop_sent = True
            except OSError:
                pass
            sleep(0.02)
        result = {"session": hold.session, "phase": hold.reason or "released", "sent": True,
                  "stopSent": stop_sent}
        if not stop_sent:
            return {**result, "phase": "failed",
                    "positionStable": False, "positionCheck": "stop-not-sent", "positionSamples": 0,
                    "message": "Не удалось отправить остановку. Используйте физический стоп."}
        result["positionStable"] = False
        previous = last_valid = None
        samples = 0
        changed = False
        deadline = monotonic() + HOLD_SETTLE_SECONDS
        while (remaining := deadline - monotonic()) > 0:
            try:
                last = self._position_fields(client, timeout=min(remaining, QUERY_TIMEOUT_SECONDS))
            except ControllerError:
                previous = None
            except OSError:
                break
            else:
                samples += 1
                result["position"] = coordinates(last)
                if previous is not None and positions_match(previous, last):
                    result["positionStable"] = True
                    if hold.reason == "released":
                        result.update(self._position_payload(last, issue_pilot=hold.control_token is None))
                    break
                changed = changed or (last_valid is not None and not positions_match(last_valid, last))
                previous = last
                last_valid = last
            if (remaining := deadline - monotonic()) > 0:
                sleep(min(HOLD_POSITION_INTERVAL_SECONDS, remaining))
        result["positionSamples"] = samples
        if not result["positionStable"]:
            if changed:
                result["positionCheck"] = "changed"
                message = "Остановка отправлена. Координаты продолжали меняться. Проверьте пульт."
            elif samples:
                result["positionCheck"] = "insufficient"
                message = "Остановка отправлена. Недостаточно показаний координат. Проверьте пульт."
            else:
                result["positionCheck"] = "unreachable"
                message = "Остановка отправлена. Контроллер не подтвердил координаты. Проверьте пульт."
        elif hold.reason == "expired":
            result["positionCheck"] = "stable"
            message = "Остановка отправлена после потери удержания. Координаты стабильны."
        else:
            result["positionCheck"] = "stable"
            message = "Остановка отправлена. Координаты после отпускания стабильны."
        return {**result, "message": message}

    def stop_active_hold(self):
        with self._hold_condition:
            hold = self._active_hold
            if hold is not None:
                hold.reason = hold.reason or "cancelled"
                self._hold_condition.notify_all()
                self._hold_condition.wait_for(lambda: hold.result is not None, timeout=HOLD_START_SECONDS + HOLD_RELEASE_SECONDS)

    def operate(self, action, token=None, ready=None, job_confirmed=None, recover_upload=None, control_token=None,
                job_receipt=None, job_verified=None, *, confirmed_stop=False, x=None, y=None):
        if action == "acknowledge-run":
            return self._acknowledge_run(control_token, job_verified)
        if action == "move-to":
            return self._move_to(x, y, control_token)
        if not isinstance(action, str) or action not in OPERATION_CODES:
            raise ControllerError("Неизвестная операция контроллера.")
        bounded_stop = action == "cancel-cut" and confirmed_stop
        if bounded_stop:
            control_token = None
        elif control_token is not None or action not in ("feed", "cut", "cancel-cut"):
            self.require_control(control_token)
        else:
            if action != "cancel-cut" and ready is not True:
                raise ControllerError("Подтвердите условия у станка перед запуском.")
            if action == "cut" and job_confirmed is not True:
                raise ControllerError("Сверьте загруженное задание на штатном пульте.")
        requires_receipt = control_token is not None and action in ("cut", "range")
        actuator_changes = ACTUATOR_COMMAND_EFFECTS.get(action)
        with self._hold_condition:
            self._require_run_action(action)
            if requires_receipt:
                self._require_job_receipt(control_token, job_receipt, action, job_verified)
            if self._active_hold is not None:
                raise ControllerError("Сначала отпустите кнопку ручного движения.")
        try:
            baseline = None
            manual_speed = None
            with self._io(), self._client() as client:
                with self._hold_condition:
                    self._require_run_action(action)
                    tracked_run = self._run.blocks_machine
                if action == "home":
                    status = self._query(client, b"sts\x00", STATUS_REPLY, "статуса")
                    if "fields" not in status:
                        raise ControllerError(status["message"])
                    baseline = status["fields"][5]
                elif action not in OPERATIONS_DURING_MOTION:
                    if action == "cut" and control_token is None and self.upload_uncertain and recover_upload is not True:
                        raise ControllerError("Предыдущая загрузка не подтверждена. Загрузите и сверьте задание через Xiao или повторите загрузку.")
                    reference = self._consume_position(token) if control_token is None else None
                    _, baseline = self._preflight(client, reference, require_clearance=action in OPERATIONS_REQUIRING_CLEARANCE)
                    if action in ("cut", "range"):
                        job = self._query(client, b"idx\x00", JOB_INDEX_REPLY, "индекса задания")
                        if not requires_receipt and ("fields" not in job or job["fields"][2] < 1):
                            raise ControllerError("Контроллер не подтвердил наличие загруженного задания.")
                else:
                    self._pilot = None
                    if tracked_run or bounded_stop:
                        try:
                            status = self._query(client, b"sts\x00", STATUS_REPLY, "статуса")
                        except OSError:
                            if action != "cancel-cut":
                                raise
                        else:
                            if "fields" in status:
                                baseline = status["fields"][5]
                    with self._hold_condition:
                        self._report_cut_event(baseline)
                        if bounded_stop:
                            if tracked_run and (baseline == 5 or baseline == 4 and not self._run.feed_active):
                                # A previous terminal event can remain in sts while Start is pending.
                                if self._run.state != "starting" or baseline != self._run.last_event:
                                    raise ControllerError("Контроллер уже сообщил об окончании или отмене раскроя.")
                            if not tracked_run:
                                if not self._recent_external_cut():
                                    raise ControllerError("Активный рез не подтверждён. Проверьте станок на штатном пульте.")
                                baseline = self._reported_cut_event
                    if action != "cancel-cut":
                        expected = 3 if action == "pause-cut" else 9
                        if baseline != expected:
                            raise ControllerError("Текущее состояние реза не подтверждено. Сверьте штатный пульт.")
                        job = self._query(client, b"idx\x00", JOB_INDEX_REPLY, "индекса задания")
                        if not self._matches_job_index(job.get("fields"), self._run.points):
                            raise ControllerError("Текущее задание не подтверждено. Сверьте штатный пульт.")
                message = f"{OPERATION_LABELS[action]} — команда отправлена."
                with self._hold_condition:
                    if action not in OPERATIONS_DURING_MOTION:
                        fields = job.get("fields") if action in ("cut", "range") else None
                        self._observe_preflight(baseline, fields)
                    self._require_run_action(action)
                    if control_token is not None:
                        self.require_control(control_token)
                    if self._active_hold is not None:
                        raise ControllerError("Сначала отпустите кнопку ручного движения.")
                    if requires_receipt:
                        receipt = self._require_job_receipt(control_token, job_receipt, action, job_verified)
                        if action in ("cut", "range"):
                            if job["busy"]:
                                raise ControllerError(job["message"])
                            if not self._matches_job_index(job.get("fields"), receipt.points):
                                self._clear_job_receipt(uncertain=True)
                                raise ControllerError("Задание на контроллере не совпадает с загрузкой. Загрузите чертёж заново.")
                            if action == "cut" and not self._matches_job_index(job.get("fields"), receipt.points, at_start=True):
                                self._clear_job_receipt(uncertain=True)
                                raise ControllerError("Контроллер уже прошёл часть задания. Сверьте раскрой на штатном пульте.")
                    if action in ("home", "set-zero"):
                        self._clear_job_receipt()
                    client.sendto(f"cmd({OPERATION_CODES[action]})\x00".encode("ascii"), self.endpoint)
                    if actuator_changes is not None:
                        self._actuators.update(actuator_changes)
                        self._actuators["revision"] += 1
                    actuators = self._actuators.copy()
                    if action == "cut":
                        self._run.start(receipt.points if requires_receipt else job["fields"][2], baseline, message,
                                        receipt.value if requires_receipt else None)
                        if not requires_receipt:
                            self._clear_job_receipt()
                            if self.upload_uncertain:
                                self._upload_state = "idle"
                    elif action in OPERATIONS_DURING_MOTION and tracked_run:
                        pending = {"pause-cut": "pausing", "resume-cut": "resuming", "cancel-cut": "cancelling"}
                        self._run.command(pending[action], baseline, message)
                        if action == "cancel-cut":
                            self._clear_job_receipt()
                    elif bounded_stop:
                        self._clear_job_receipt()
                        self._run.cancel_untracked(baseline, message)
                    elif action == "cancel-cut" or action in IDLE_CANCEL_OPERATIONS:
                        self._clear_job_receipt()
                        self._run.record(message)
                    elif action == "feed":
                        self._run.manual_feed_sent()
                    run = self._run.snapshot()
                    cut_motion = self._cut_motion_snapshot()
                if action == "manual-speed" and self.manual_speed_readback is not None:
                    firmware = self._read_firmware(client)
                    manual_speed = self.manual_speed_readback.read(client, self._exchange, firmware)
        except OSError as error:
            raise ControllerError("Не удалось отправить команду контроллеру.") from error
        result = {"operation": action, "sent": True, "message": message,
                  "uploadUncertain": self.upload_uncertain, "run": run, "actuators": actuators, **cut_motion}
        if actuator_changes is not None:
            result["actuatorChanges"] = dict(actuator_changes)
        if action == "cancel-cut" or action in IDLE_CANCEL_OPERATIONS:
            result["jobReceipt"] = None
        if action == "manual-speed":
            result["manualSpeed"] = manual_speed
        if baseline is not None:
            result["baselineEvent"] = {"code": baseline, "label": EVENT_LABELS.get(baseline)}
        return result

    def _move_to(self, x, y, control_token):
        self.require_control(control_token)
        target = self._target_ticks(x, "X", self.width), self._target_ticks(y, "Y", self.height)

        try:
            with self._io(), self._client() as client:
                with self._hold_condition:
                    self._require_idle_run()
                    if self._active_hold is not None:
                        raise ControllerError("Сначала отпустите кнопку ручного движения.")
                position, baseline = self._preflight(client, None, require_idle_controller=True,
                                                     settle_interval=HOLD_POSITION_INTERVAL_SECONDS)
                with self._hold_condition:
                    self._observe_preflight(baseline)
                    self._require_idle_run()
                    self.require_control(control_token)
                    if self._active_hold is not None:
                        raise ControllerError("Сначала отпустите кнопку ручного движения.")

                    sent = tuple(position[:2]) != target
                    if sent:
                        packet = f"cmd({COORDINATE_MOVE_CODE},{target[0]},{target[1]})\x00".encode("ascii")
                        client.sendto(packet, self.endpoint)
                    now = monotonic()
                    self._coordinate_move = CoordinateMove(target, now + COORDINATE_MOVE_TIMEOUT_SECONDS,
                                                           now, baseline, "moving" if sent else "reached")
                    return {"operation": "move-to", "sent": sent, "coordinateMove": self._coordinate_move.snapshot(),
                            "message": "Команда перемещения отправлена." if sent else "Головка уже в заданных координатах."}
        except OSError as error:
            raise ControllerError("Не удалось отправить команду контроллеру.") from error

    @staticmethod
    def _target_ticks(value, axis, limit):
        if type(value) in (int, float) and 0 <= value <= limit:
            ticks = round(value * TICKS_PER_MM)
            if ticks / TICKS_PER_MM <= limit:
                return ticks
        raise ControllerError(f"Координата {axis} должна быть от 0 до {limit:g} мм.")

    def _require_run_action(self, action):
        self._run.expire_command()
        if action not in OPERATIONS_DURING_MOTION:
            self._require_idle_run()
        elif action == "pause-cut" and self._run.state != "running":
            raise ControllerError("Пауза доступна после подтверждения начала реза.")
        elif action == "resume-cut" and self._run.state != "paused":
            raise ControllerError("Продолжение доступно после подтверждения паузы.")
        elif action == "cancel-cut" and self._run.state == "cancelling":
            raise ControllerError("Команда отмены уже отправлена. Проверьте штатный пульт.")
        if action == "resume-cut":
            self._require_motion_available()

    def _acknowledge_run(self, control_token, job_verified):
        self.require_control(control_token)
        if job_verified is not True:
            raise ControllerError("Подтвердите на штатном пульте, что раскрой и движение остановлены.")
        try:
            with self._io(), self._client() as client:
                with self._hold_condition:
                    if not self._run.blocks_machine or self._run.state not in ("unknown", "error"):
                        raise ControllerError("Сверка доступна при неподтверждённом состоянии раскроя.")
                self._preflight(client, None, require_clearance=False)
                with self._hold_condition:
                    self.require_control(control_token)
                    self._clear_job_receipt(uncertain=True)
                    self._run.acknowledge()
                    return {"operation": "acknowledge-run", "sent": False, "acknowledged": True,
                            "message": "Сверка состояния записана. Загрузите текущий чертёж перед новым запуском.",
                            "uploadUncertain": self.upload_uncertain, "run": self._run.snapshot(),
                            "actuators": self._actuators.copy()}
        except OSError as error:
            raise ControllerError("Не удалось проверить координаты контроллера. Повторите сверку на штатном пульте.") from error

    def upload_job(self, job, finalization, page_add, token=None, ready=None, auto_run_off=None, control_token=None,
                   *, filename=None):
        if control_token is not None:
            self.require_control(control_token, for_upload=True, auto_run_off=auto_run_off)
        elif ready is not True or auto_run_off is not True:
            raise ControllerError("Подтвердите готовность у станка и выключенный Auto Run на пульте.")
        with self._hold_condition:
            self._require_idle_run()
            if self._active_hold is not None:
                raise ControllerError("Сначала отпустите кнопку ручного движения.")
        try:
            with self._io():
                with self._hold_condition:
                    self._require_idle_run()
                    if control_token is not None:
                        self.require_control(control_token, for_upload=True, auto_run_off=auto_run_off)
                with self._client() as client:
                    return self._transfer_checked_job(client, job, finalization, page_add, token,
                                                      control_token, filename, auto_run_off)
        except OSError as error:
            raise ControllerError("Не удалось начать передачу задания. Проверьте связь с контроллером.") from error

    def _transfer_checked_job(self, client, job, finalization, page_add, token, control_token, filename, auto_run_off):
        if page_add is None or self.upload_page_add() != page_add:
            raise ControllerError("Профиль передачи Xiao изменился. Проверьте настройки и подготовьте задание заново.")
        reference = self._consume_position(token) if control_token is None else None
        _, baseline = self._preflight(client, reference)
        with self._hold_condition:
            self._observe_preflight(baseline)
            self._require_idle_run()
            if baseline == 7:
                raise ControllerError("Дождитесь окончания раскроя, передачи или подачи на штатном пульте.")
        board, busy = self._exchange(client, b"dog\x00", BOARD_INFO_REPLY, timeout=1.0)
        if busy or not board or int(board[1]) < FIRMWARE_WITH_FINAL_ACK:
            raise ControllerError("Не подтверждена версия платы для прямой передачи. Откройте файл через Xiao.")

        previous_limit = None
        if control_token is not None:
            before = self._query(client, b"idx\x00", JOB_INDEX_REPLY, "индекса задания перед загрузкой")
            fields = before.get("fields")
            if not fields or not -1 <= fields[0] <= fields[2] or fields[2] < 0:
                raise ControllerError("Не удалось проверить индекс задания до загрузки. Повторите попытку.")
            previous_limit = fields[2]

        if self.auto_run_readback is not None:
            readback = self.auto_run_readback.read(client, self._exchange, int(board[1]))
            if readback is False:
                raise ControllerError("На контроллере включён Auto Run. Выключите его на штатном пульте.")
            if readback is not True:
                raise ControllerError("Контроллер не подтвердил состояние Auto Run. Задание не передано.")

        with self._hold_condition:
            self._require_idle_run()
            if self._active_hold is not None:
                raise ControllerError("Сначала отпустите кнопку ручного движения.")
            if control_token is not None:
                self.require_control(control_token, for_upload=True, auto_run_off=auto_run_off)
            self._clear_job_receipt()
            self._upload_state = "transferring"
            self._run.begin_upload(filename)
        try:
            points = transfer_job(client, self.endpoint, job, finalization)
            if control_token is not None:
                result = self._confirm_uploaded_job(client, points, previous_limit, control_token)
            else:
                self._upload_state = "confirmed"
                result = {"uploaded": True, "points": points, "phase": "confirmed", "commitSent": True,
                          "uploadUncertain": False,
                          "message": "Контроллер подтвердил приём листа. Сверьте задание на пульте перед резом."}
        except JobTransferError as error:
            result = {"uploaded": False, "points": error.acknowledged_points, "phase": error.stage,
                      "commitSent": error.commit_sent, "uploadUncertain": True,
                      "message": f"{error} Текущее задание на пульте требует сверки."}
        finally:
            if self._upload_state == "transferring":
                self._upload_state = "uncertain"
        with self._hold_condition:
            self._run.finish_upload(result)
            result["run"] = self._run.snapshot()
            result["baselineEvent"] = {"code": baseline, "label": EVENT_LABELS.get(baseline)}
        return result

    def _confirm_uploaded_job(self, client, points, previous_limit, control_token):
        result = {"points": points, "phase": "verification", "commitSent": True,
                  "uploaded": False, "uploadUncertain": True}
        sleep(0.08)
        try:
            index = self._query(client, b"idx\x00", JOB_INDEX_REPLY, "индекса задания после загрузки")
        except OSError:
            return {**result, "message": "Плата приняла передачу, но прочитать индекс задания не удалось. Загрузите чертёж заново."}
        if not self._matches_job_index(index.get("fields"), points, at_start=True):
            return {**result, "message": "Плата приняла передачу, но индекс нового задания не подтверждён. Загрузите чертёж заново."}
        with self._hold_condition:
            try:
                self.require_control(control_token)
            except ControllerError:
                return {**result, "message": "Сеанс завершился до проверки загрузки. Включите управление и загрузите чертёж заново."}
            receipt = JobReceipt(secrets.token_urlsafe(24), control_token, points, previous_limit == points)
            self._job_receipt = receipt
            self._upload_state = "confirmed"
        return {**result, "uploaded": True, "phase": "confirmed", "uploadUncertain": False,
                "jobReceipt": receipt.value, "indexLimit": receipt.points,
                "verificationRequired": receipt.verification_required,
                "message": "Контроллер подтвердил приём листа. Сверьте задание на пульте перед резом."}

    def _position_fields(self, client, timeout=QUERY_TIMEOUT_SECONDS):
        result = self._query(client, b"pos\x00", POSITION_REPLY, "координат", timeout=timeout)
        if "fields" not in result:
            raise ControllerError(result["message"])
        return result["fields"]

    @contextmanager
    def _client(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            if os.name == "nt":
                client.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                client.bind(("0.0.0.0", self.endpoint[1]))
            except OSError as error:
                raise ControllerError(f"Локальный UDP-порт {self.endpoint[1]} недоступен. Возможно, его использует Xiao.") from error
            yield client

    def _read(self, packet, reply_pattern, label):
        try:
            with self._client() as client:
                return self._query(client, packet, reply_pattern, label)
        except ControllerError as error:
            message = str(error)
        except OSError:
            message = "Сетевой запрос к контроллеру не выполнен."
        ip, port = self.endpoint
        return {"ip": ip, "port": port, "responded": False, "busy": False, "message": message}

    def _query(self, client, packet, reply_pattern, label, timeout=QUERY_TIMEOUT_SECONDS):
        ip, port = self.endpoint
        result = {"ip": ip, "port": port, "responded": False, "busy": False}
        match, busy = self._exchange(client, packet, reply_pattern, timeout=timeout)
        if busy:
            return {**result, "responded": True, "busy": True, "message": "Контроллер ответил: занят."}
        if match:
            fields = [int(value) for value in match[1].split(b",")]
            return {**result, "responded": True, "fields": fields,
                    "message": f"Контроллер ответил на запрос {label}."}
        return {**result, "message": f"Контроллер не ответил на запрос {label}."}

    def _exchange(self, client, packet, reply_pattern, timeout=QUERY_TIMEOUT_SECONDS):
        client.sendto(packet, self.endpoint)
        deadline = monotonic() + timeout
        try:
            while (remaining := deadline - monotonic()) > 0:
                client.settimeout(remaining)
                response, source = client.recvfrom(1000)
                if source != self.endpoint:
                    continue
                response = response.partition(b"\x00")[0]
                if response == b"machine is busy":
                    return None, True
                match = reply_pattern.fullmatch(response)
                if match:
                    return match, False
        except TimeoutError:
            pass
        return None, False
