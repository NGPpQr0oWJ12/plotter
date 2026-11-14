from collections import deque
import json
import logging
from pathlib import Path

from run_monitor import timestamp
from workspace import replace_file


MAX_EVENTS = 100
MAX_BYTES = 256 * 1024
LOGGER = logging.getLogger(__name__)


class CutJournal:
    """Persist operator-visible cut events without restoring machine control state."""

    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        self.events = deque(maxlen=MAX_EVENTS)
        if self.path is None:
            return
        try:
            with self.path.open("rb") as file:
                contents = file.read(MAX_BYTES + 1)
            if len(contents) > MAX_BYTES:
                raise ValueError("Cut journal is too large")
            entries = json.loads(contents)
            if not isinstance(entries, list):
                raise ValueError("Invalid cut journal")
            self.events.extend(entry for entry in entries[-MAX_EVENTS:] if self._valid_entry(entry))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            LOGGER.warning("Не удалось прочитать журнал раскроя.")

    @staticmethod
    def _valid_entry(entry):
        return (isinstance(entry, dict) and type(entry.get("at")) is int
                and 0 <= entry["at"] <= 8_640_000_000_000_000
                and type(entry.get("page")) is int and 1 <= entry["page"] <= 128
                and all(isinstance(entry.get(key), str) and len(entry[key]) <= 255
                        for key in ("filename", "kind", "message")))

    def record(self, filename, page, kind, message):
        event = {"at": timestamp(), "filename": filename, "page": page, "kind": kind, "message": message}
        self.events.append(event)
        if self.path is None:
            return event
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            contents = json.dumps(list(self.events), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            replace_file(self.path, contents)
        except OSError:
            LOGGER.warning("Не удалось сохранить журнал раскроя.")
        return event

    def snapshot(self):
        return list(self.events)
