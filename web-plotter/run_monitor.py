from collections import deque
from time import monotonic, time


COMMAND_TIMEOUT = 7.0
FRESHNESS_TIMEOUT = 5.0
PENDING_STATES = frozenset(("starting", "pausing", "resuming", "cancelling"))
ACTIVE_STATES = PENDING_STATES | {"running", "paused", "feeding", "unknown", "error"}
FAULT_EVENTS = frozenset((101, 104, 109, 110, 111, 112, 115, 126, 127, 129, 130, 134, 135, 136, 140))


def timestamp():
    return int(time() * 1000)


class RunMonitor:
    """Track a local run from observed controller events. The caller serializes access."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.state = "idle"
        self.filename = None
        self.points = None
        self.started_at = None
        self.started_from_panel = False
        self.started_receipt = None
        self.ended_at = None
        self.updated_at = None
        self.last_seen = None
        self.last_event = None
        self.index = None
        self.completed_index = 0
        self.index_regressed = False
        self.fresh = False
        self.deadline = None
        self.completed_pages = 0
        self.feed_active = False
        self.followup_feed_deadline = None
        self.events = deque(maxlen=30)

    @property
    def blocks_machine(self):
        return self.started_at is not None and self.state in ACTIVE_STATES

    def record(self, message):
        self.events.append({"at": timestamp(), "message": message})

    def snapshot(self):
        self.expire_command()
        fresh = bool(self.fresh and self.last_seen is not None
                     and monotonic() - self.last_seen <= FRESHNESS_TIMEOUT)
        result = {"state": self.state, "filename": self.filename,
                  "page": 1 if self.points is not None else None,
                  "pages": 1 if self.points is not None else 0,
                  "indexLimit": self.points,
                  "completedPages": self.completed_pages, "startedAt": self.started_at,
                  "startedFromPanel": self.started_from_panel,
                  "startedReceipt": self.started_receipt,
                  "endedAt": self.ended_at, "updatedAt": self.updated_at, "fresh": fresh,
                  "completedIndex": self.completed_index,
                  "events": list(self.events)}
        if fresh and self.index is not None:
            result["index"] = self.index
        return result

    def begin_upload(self, filename):
        self._reset()
        self.filename = filename
        self.state = "transferring"
        self.record("Начата передача задания на контроллер.")

    def finish_upload(self, result):
        if result["uploaded"]:
            self.points = result["points"]
            self.state = "ready"
        else:
            self.state = "error"
        self.record(result["message"])

    def invalidate_ready(self):
        if self.state == "ready":
            self.state = "idle"
            self.record("Перед запуском загрузите и проверьте текущий чертёж заново.")

    def start(self, points, baseline, message, receipt=None):
        self.points = points
        self.started_at = timestamp()
        self.started_from_panel = False
        self.started_receipt = receipt
        self.ended_at = None
        self.completed_pages = 0
        self.completed_index = 0
        self.index_regressed = False
        self.feed_active = False
        self.followup_feed_deadline = None
        self.command("starting", baseline, message)

    def observe_panel_start(self, event, label, receipt):
        self.started_at = timestamp()
        self.started_from_panel = True
        self.started_receipt = receipt
        self.state = "running" if event == 3 else "paused"
        self.last_event = event
        self.record(label)

    def cancel_untracked(self, baseline, message):
        events = self.events
        self._reset()
        self.events = events
        self.started_at = timestamp()
        self.feed_active = baseline == 50
        self.command("cancelling", baseline, message)

    def command(self, state, baseline, message):
        self.state = state
        self.last_event = baseline
        self.index = None
        self.deadline = monotonic() + COMMAND_TIMEOUT
        self.record(message)

    def expire_command(self):
        if self.deadline is not None and monotonic() >= self.deadline:
            self.unknown("Контроллер не подтвердил действие. Сверьте состояние на штатном пульте.")

    def unknown(self, message):
        if self.state != "unknown":
            self.record(message)
        self.state = "unknown"
        self.deadline = None
        self.index = None

    def acknowledge(self):
        self.state = "idle"
        self.ended_at = timestamp()
        self.deadline = None
        self.feed_active = False
        self.record("Оператор подтвердил остановку на штатном пульте.")

    def manual_feed_sent(self):
        self.followup_feed_deadline = None

    def observe(self, event, label, fields, busy):
        self.fresh = event is not None and not busy
        self.index = None
        self.expire_command()
        if not self.fresh:
            self.followup_feed_deadline = None
        if event is None:
            if not busy and self.blocks_machine:
                self.unknown("Связь с контроллером потеряна. Сверьте состояние на штатном пульте.")
            return

        now = monotonic()
        self.last_seen = now
        self.updated_at = timestamp()
        if self.points is not None and fields:
            index, _, limit = fields[:3]
            if limit == self.points and 0 <= index <= limit:
                self.index = index

        if event == 4 and self.points is not None and (not self.fresh or not fields):
            return
        changed = self.last_event is not None and event != self.last_event
        followup_feed = (self.state == "completed" and self.last_event == 4
                         and self.followup_feed_deadline is not None and now <= self.followup_feed_deadline)
        self.last_event = event
        if event != 4:
            self.followup_feed_deadline = None
        if changed and event == 50 and (self.blocks_machine or followup_feed):
            self.record(label or "Начата подача ткани.")
            if self.state != "cancelling":
                self.state = "feeding"
                self.ended_at = None
                self.deadline = None
            self.feed_active = True
            return
        if not self.blocks_machine:
            return
        index_changed = self.points is not None and fields and self.index is None
        if self.fresh and index_changed and event in (3, 9):
            self.index_regressed = True
        if (self.fresh and self.index is not None and event in (3, 9)
                and self.state in ("starting", "running", "pausing", "paused", "resuming")):
            # The reported index can be the current target. Only earlier points count until completion.
            completed = max(0, self.index - 1)
            self.index_regressed |= completed < self.completed_index
            if not self.index_regressed:
                self.completed_index = completed
        if changed:
            self.record(label or f"Событие контроллера {event}.")
            if event == 4 and not index_changed:
                self.completed_pages = int(self.points is not None)
                if self.points is not None:
                    self.completed_index = self.points
                if not self.feed_active:
                    self.finish("completed")
                    if self.fresh:
                        self.followup_feed_deadline = now + FRESHNESS_TIMEOUT
                return
            if event == 5:
                if self.feed_active:
                    self.unknown("Контроллер сообщил об отмене реза. Проверьте остановку подачи на штатном пульте.")
                else:
                    self.finish("cancelled")
                return
            if event in FAULT_EVENTS:
                self.state = "error"
                self.deadline = None
                return
            if event == 49:
                self.feed_active = False
                if self.state == "feeding":
                    if self.completed_pages == 1 and not index_changed:
                        self.finish("completed")
                    else:
                        self.unknown("Подача завершена, состояние раскроя требует сверки на штатном пульте.")
                return
        if index_changed:
            self.unknown("Индекс задания изменился. Сверьте задание на штатном пульте.")
            return
        if changed and event == 3 and self.state in ("starting", "resuming", "paused"):
            self.state = "running"
            self.deadline = None
        elif changed and event == 9 and self.state in ("running", "pausing", "starting", "resuming"):
            self.state = "paused"
            self.deadline = None

    def finish(self, state):
        self.state = state
        self.ended_at = self.updated_at
        self.deadline = None
