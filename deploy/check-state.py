"""Check that a release can save existing Plotter data without dropping fields."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys


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
            # A previous release leaves the new database untouched during rollback.
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(production.as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("production.sqlite3: invalid registry database")


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit(str(error)) from error
