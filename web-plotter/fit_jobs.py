"""Manage a local, cancellable upholstery fit calculation."""

import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from uuid import uuid4


class FitJobs:
    def __init__(self):
        self.lock = Lock()
        self.job = None

    def start(self, model, state):
        with self.lock:
            if self.job and self._status()["status"] == "running":
                raise ValueError("Расчёт посадки уже запущен.")
            self._clear()
            with_resources = TemporaryDirectory(prefix="plotter-fit-")
            try:
                directory = Path(with_resources.name)
                (directory / "model.gz").write_bytes(model)
                (directory / "markup.json").write_text(json.dumps(state, allow_nan=False), encoding="utf-8")
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(directory)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception:
                with_resources.cleanup()
                raise
            identifier = uuid4().hex
            self.job = identifier, process, with_resources, monotonic()
            return {"id": identifier}

    def status(self, identifier):
        with self.lock:
            self._check(identifier)
            return self._status()

    def cancel(self, identifier):
        with self.lock:
            self._check(identifier)
            self._clear()
            return {"status": "cancelled"}

    def close(self):
        with self.lock:
            self._clear()

    def _check(self, identifier):
        if not self.job or identifier != self.job[0]:
            raise ValueError("Расчёт посадки не найден.")

    def _status(self):
        _, process, resources, started = self.job
        if process.poll() is None:
            if monotonic() - started <= 300:
                return {"status": "running"}
            self._stop(process)
            return {"status": "error", "message": "Превышено время расчёта посадки."}
        output = Path(resources.name) / "result.json"
        if process.returncode or not output.is_file():
            return {"status": "error", "message": "Процесс расчёта посадки завершился без результата."}
        return json.loads(output.read_text(encoding="utf-8"))

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
            process.wait()

    def _clear(self):
        if self.job:
            _, process, resources, _ = self.job
            self._stop(process)
            resources.cleanup()
            self.job = None


def run_worker(directory):
    from upholstery import UpholsteryError
    from upholstery_fit import simulate

    model = json.loads(gzip.decompress((directory / "model.gz").read_bytes()))
    state = json.loads((directory / "markup.json").read_text(encoding="utf-8"))
    try:
        if state.get("modelEdges"):
            from model_surface import build_surface_model

            model = build_surface_model(model, state["modelEdges"])
        payload = {"status": "done", "result": simulate(model, state)}
    except (UpholsteryError, ValueError) as error:
        payload = {"status": "error", "message": str(error)}
    (directory / "result.json").write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    run_worker(Path(sys.argv[1]))
