"""Check that a release can save existing Plotter data without dropping fields."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys


def check_sqlite(path, copy=None):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"{path.name}: SQLite integrity check failed")
        if copy is not None:
            with closing(sqlite3.connect(copy)) as target:
                db.backup(target)


def validate(application, state):
    sys.path.insert(0, str(application))
    from cut_journal import CutJournal
    from workspace import WorkspaceStore

    for source in (state / "workspace.json", state / "workspace.json.bak"):
        if not source.exists():
            continue
        contents = source.read_bytes()
        original = json.loads(contents)
        with TemporaryDirectory(prefix="plotter-state-check-") as scratch:
            path = Path(scratch) / "workspace.json"
            path.write_bytes(contents)
            store = WorkspaceStore(path)
            loaded = store.read()
            if loaded["recovered"] or loaded["revision"] != original["revision"]:
                raise ValueError(f"{source.name}: release cannot load this revision")
            store.write(loaded["revision"], loaded["workspace"])
            saved = json.loads(path.read_bytes())
            if saved["version"] != original["version"] or saved["workspace"] != original["workspace"]:
                raise ValueError(f"{source.name}: release changes saved workspace format or fields")

    journal = state / "cut-journal.json"
    if journal.exists() and CutJournal(journal).snapshot() != json.loads(journal.read_bytes()):
        raise ValueError("cut-journal.json: release cannot read every saved journal entry")

    production = state / "production.sqlite3"
    if production.exists():
        if (application / "production_store.py").is_file():
            from production_store import ProductionStore
            ProductionStore(production).check()
        else:
            check_sqlite(production)

    labels = state / "labels.sqlite3"
    if labels.exists():
        if (application / "label_queue.py").is_file():
            from label_queue import LabelQueue
            # Queue initialization can migrate records. Keep the recovery backup unchanged.
            with TemporaryDirectory(prefix="plotter-labels-check-") as scratch:
                path = Path(scratch) / labels.name
                check_sqlite(labels, copy=path)
                LabelQueue(path, load_bundle=None)
        else:
            # A previous release leaves the database untouched during rollback.
            check_sqlite(labels)


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from error
