"""Durable label settings and verified print jobs."""

from contextlib import closing, contextmanager
import json
import logging
import re
import sqlite3
from threading import Event, Lock, Thread
from uuid import uuid4

from workspace import WorkspaceConflict


LOGGER = logging.getLogger("plotter.labels")
UNKNOWN_SUBMISSION = "Windows не подтвердила отправку. Проверьте очередь и ленту."


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def part_ends(source, pages):
    """Last confirmed vertex of each instance, in execution order across the material."""
    from label_layout import label_part_keys
    ends, physical = {}, set()
    keys = label_part_keys(source)
    for page in pages:
        for path, end in zip(page["paths"], page["pathEndIndices"], strict=True):
            i = path["index"]
            original = source[i]
            number = original.get("partNumber")
            key = keys[i]
            if not original.get("notch"):
                physical.add(key)
            ends[key] = (key, str(original.get("partDisplayNumber") or number or i + 1),
                         original.get("partName") or original.get("layer", ""), page["number"], end)
    return [end for key, end in ends.items() if key in physical]


class LabelQueue:
    def __init__(self, path, load_bundle, *, local_spool=lambda: False, relay=None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.load_bundle = load_bundle
        self.local_spool, self.relay = local_spool, relay
        self.wake, self.stopped = Event(), Event()
        self.worker_lock = Lock()
        self.thread = None
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS templates(kind TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revision(n INTEGER NOT NULL);
                INSERT INTO revision SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM revision);
                CREATE TABLE IF NOT EXISTS settings(mode TEXT,cut TEXT,enabled INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(mode,cut));
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,mode TEXT NOT NULL,cut TEXT NOT NULL,
                    revision INTEGER NOT NULL,first_page INTEGER NOT NULL,bundle TEXT,finished INTEGER DEFAULT 0,
                    completed_ms INTEGER);
                CREATE INDEX IF NOT EXISTS runs_cut ON runs(mode,cut);
                CREATE TABLE IF NOT EXISTS pieces(run TEXT,key TEXT,number TEXT,name TEXT,page INTEGER,vertex INTEGER,
                    ready INTEGER DEFAULT 0,PRIMARY KEY(run,key));
                CREATE INDEX IF NOT EXISTS pieces_progress ON pieces(run,page,ready,vertex);
                CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,run TEXT NOT NULL,key TEXT NOT NULL,
                    token TEXT NOT NULL,status TEXT NOT NULL,printer TEXT NOT NULL,message TEXT DEFAULT '',
                    spool INTEGER,pdf BLOB,claim TEXT,claim_until INTEGER,UNIQUE(run,key,token));
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,id);
                CREATE TABLE IF NOT EXISTS station_label_printer(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS direct_label_prints(request_id TEXT PRIMARY KEY,scope TEXT NOT NULL,
                    status TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',spool INTEGER);
                CREATE TABLE IF NOT EXISTS auto_label_station(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_label_watches(token TEXT PRIMARY KEY,cut TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            run_columns = {column["name"] for column in db.execute("PRAGMA table_info(runs)")}
            if "completed_ms" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN completed_ms INTEGER")
            if "relay_key" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN relay_key TEXT")
            if "relay_station" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN relay_station TEXT")
            columns = {column["name"] for column in db.execute("PRAGMA table_info(jobs)")}
            for name, field in (("pdf", "BLOB"), ("auto_spool", "INTEGER NOT NULL DEFAULT 0"),
                                ("auto_page", "TEXT"), ("station", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {field}")
            db.execute("""UPDATE jobs SET status='legacy',message=? WHERE auto_spool=0 AND token='auto'
                          AND status IN ('queued','ready')""", ("Бирка прежнего PDF-режима. Доступна ручная печать.",))
            db.execute("INSERT OR IGNORE INTO auto_label_station VALUES(1,?)", (uuid4().hex,))
            self.station = db.execute("SELECT token FROM auto_label_station WHERE id=1").fetchone()[0]
            db.execute("UPDATE jobs SET status='unknown',message=? WHERE status='submitting'",
                       ("Отправка была прервана. Проверьте ленту перед повтором.",))
            db.execute("UPDATE direct_label_prints SET status='unknown',message=? WHERE status='submitting'",
                       ("Отправка была прервана. Проверьте очередь Windows и ленту.",))

    def resume_pending(self):
        with self.db() as db:
            pending = db.execute("SELECT 1 FROM jobs WHERE status='queued' OR auto_spool=1 AND status='ready' LIMIT 1").fetchone()
            if self.relay is not None:
                pending = pending or db.execute("SELECT 1 FROM auto_label_watches LIMIT 1").fetchone()
        if pending:
            self.kick()

    @contextmanager
    def db(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def templates(self, db=None):
        from label_layout import FIELDS_BY_KIND, defaults, editable_template
        if db is None:
            with self.db() as conn:
                conn.execute("BEGIN")
                return self.templates(conn)
        values = defaults()
        values.update({k: editable_template(k, json.loads(v)) for k, v in db.execute("SELECT kind,value FROM templates")})
        return {"templates": values, "fieldsByKind": FIELDS_BY_KIND,
                "revision": db.execute("SELECT n FROM revision").fetchone()[0]}

    def save_template(self, kind, value, revision):
        from label_layout import editable_template
        template = editable_template(kind, value)
        with self.db(write=True) as db:
            if type(revision) is not int or not db.execute("UPDATE revision SET n=n+1 WHERE n=?", (revision,)).rowcount:
                raise WorkspaceConflict("Шаблон уже изменён. Обновите конструктор.")
            db.execute("INSERT OR REPLACE INTO templates VALUES(?,?)", (kind, _json(template)))
            return self.templates(db)

    def begin(self, cut, mode, revision, first_page, ends):
        run = uuid4().hex
        with self.db(write=True) as db:
            db.execute("INSERT INTO runs(id,mode,cut,revision,first_page,relay_key) VALUES(?,?,?,?,?,?)",
                       (run, mode, cut, revision, first_page, uuid4().hex))
            db.executemany("INSERT INTO pieces(run,key,number,name,page,vertex) VALUES(?,?,?,?,?,?)",
                           [(run, *piece) for piece in ends])
        self.kick()
        return run

    def abandon(self, run):
        with self.db(write=True) as db:
            db.execute("UPDATE runs SET finished=2 WHERE id=? AND finished=0", (run,))

    @staticmethod
    def _settings(db, mode, cut):
        setting = db.execute("SELECT enabled FROM settings WHERE mode=? AND cut=?", (mode, cut)).fetchone()
        return setting is None or bool(setting[0])

    def configure(self, cut, mode, enabled):
        if type(enabled) is not bool:
            raise ValueError("Проверьте настройки печати.")
        with self.db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (mode, cut, int(enabled)))
        self.kick()

    def observe(self, run, page, index, *, complete=False, completed_ms=None):
        with self.db(write=True) as db:
            record = db.execute("SELECT mode,cut,first_page,finished FROM runs WHERE id=?", (run,)).fetchone()
            if not record or record["finished"]:
                return
            enabled = self._settings(db, record["mode"], record["cut"])
            keys = [r[0] for r in db.execute("""SELECT key FROM pieces WHERE run=? AND page=? AND vertex<=?
                                                AND ready=0 ORDER BY vertex""", (run, page, index))]
            db.execute("UPDATE pieces SET ready=1 WHERE run=? AND page=? AND vertex<=? AND ready=0", (run, page, index))
            if complete:
                missing = db.execute("SELECT 1 FROM pieces WHERE run=? AND ready=0 LIMIT 1", (run,)).fetchone()
                full = record["first_page"] == 1 and not missing and db.execute(
                    "SELECT 1 FROM pieces WHERE run=? LIMIT 1", (run,)).fetchone() is not None
                db.execute("UPDATE runs SET finished=?,completed_ms=? WHERE id=?",
                           (1 if full else 2, completed_ms if full else None, run))
                if full:
                    keys.append("master")
            if enabled:
                self._enqueue(db, run, keys, "auto")
        if enabled and keys:
            self.kick()

    @staticmethod
    def _enqueue(db, run, keys, token):
        db.executemany("INSERT OR IGNORE INTO jobs(run,key,token,status,printer,auto_spool) VALUES(?,?,?,'queued','',?)",
                       [(run, key, token, token == "auto") for key in keys])

    @staticmethod
    def _current(db, cut, mode):
        return db.execute("SELECT id,revision,finished,completed_ms FROM runs WHERE cut=? AND mode=? ORDER BY rowid DESC LIMIT 1",
                          (cut, mode)).fetchone()

    def manual(self, cut, mode, run, keys, request_id):
        if (not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys)
                or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100):
            raise ValueError("Выберите готовые детали для печати.")
        with self.db(write=True) as db:
            current = self._current(db, cut, mode)
            if not current or current["id"] != run:
                raise WorkspaceConflict("На столе уже другой раскрой.")
            ready = {r[0] for r in db.execute("SELECT key FROM pieces WHERE run=? AND ready=1", (run,))}
            if current["finished"] == 1:
                ready.add("master")
            if not set(keys) <= ready:
                raise ValueError("Для печати выбрана незавершённая деталь.")
            self._enqueue(db, run, keys, "manual:" + request_id)
        self.kick()

    @staticmethod
    def _relay_token(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

    def relay_key(self, cut, run):
        with self.db() as db:
            if not self._settings(db, "demo", cut):
                return None
            row = db.execute("SELECT relay_key FROM runs WHERE cut=? AND mode='demo' AND id=?", (cut, run)).fetchone()
            return row[0] if row else None

    def register_relay(self, cut, token):
        if self.relay is None or not self._relay_token(token):
            raise ValueError("Нет подтверждения передачи бирок Windows-помощнику.")
        with self.db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO auto_label_watches VALUES(?,?)", (token, cut))
        self.kick()

    def relay_source(self, cut, token, station, take=False, job_id=None, status=None):
        if not self._relay_token(token) or not self._relay_token(station):
            raise ValueError("Нет подтверждения для демораскроя.")
        with self.db(write=True) as db:
            record = db.execute("SELECT id,finished,relay_station FROM runs WHERE cut=? AND mode='demo' AND relay_key=?",
                                (cut, token)).fetchone()
            if not record or record["relay_station"] and record["relay_station"] != station:
                return {"job": None, "done": True}
            run = record["id"]
            if job_id is not None:
                if type(job_id) is not int or status not in ("submitted", "unknown", "error"):
                    raise ValueError("Проверьте результат автопечати.")
                message = UNKNOWN_SUBMISSION if status == "unknown" else ""
                db.execute("""UPDATE jobs SET status=?,message=? WHERE run=? AND id=? AND station=?
                              AND status='ready' AND auto_spool=1""", (status, message, run, job_id, station))
                return {"job": None}
            row = db.execute("""SELECT id,key,status,auto_page,station FROM jobs WHERE run=? AND auto_spool=1
                                 AND status IN ('queued','ready') ORDER BY id LIMIT 1""", (run,)).fetchone()
            if not row:
                return {"job": None, "done": bool(record["finished"])}
            if row["status"] != "ready" or (row["station"] != station and (row["station"] or take is not True)):
                return {"job": None}
            if not row["station"]:
                if not self._settings(db, "demo", cut):
                    return {"job": None}
                # Physical delivery cannot be safely reassigned after an uncertain result.
                db.execute("UPDATE runs SET relay_station=? WHERE id=? AND relay_station IS NULL", (station, run))
                db.execute("UPDATE jobs SET station=? WHERE id=?", (station, row["id"]))
            return {"job": {"id": row["id"], "key": row["key"], "page": json.loads(row["auto_page"]),
                            "enabled": self._settings(db, "demo", cut)}}

    def status(self, cut, mode):
        with self.db() as db:
            db.execute("BEGIN")
            current = self._current(db, cut, mode)
            run = current["id"] if current else None
            enabled = self._settings(db, mode, cut)
            pending = db.execute("""SELECT j.status,j.message FROM jobs j JOIN runs r ON j.run=r.id
                WHERE r.cut=? AND r.mode=? AND j.auto_spool=1 AND j.status IN ('queued','ready','submitting','unknown','error')
                ORDER BY CASE WHEN j.status='unknown' THEN 0 ELSE 1 END,j.id LIMIT 1""", (cut, mode)).fetchone()
            message = ""
            if pending and pending["status"] == "unknown":
                message = pending["message"] or UNKNOWN_SUBMISSION
            elif pending and self.local_spool():
                message = pending["message"] or ("Выберите принтер Windows для автопечати."
                                                 if not self._preferred_printer(db) and enabled else "")
            ready = [dict(r) for r in db.execute("SELECT key,number,name FROM pieces WHERE run=? AND ready=1 ORDER BY page,vertex", (run,))]
            jobs = [dict(r) for r in db.execute("""
                SELECT j.id,j.key,CASE WHEN j.key='master' THEN 'master' ELSE 'part' END AS kind,
                       COALESCE(p.number,'') AS number,COALESCE(p.name,'Мастер-бирка') AS name,
                       j.status,j.message,j.printer FROM jobs j LEFT JOIN pieces p ON p.run=j.run AND p.key=j.key
                 WHERE j.run=? ORDER BY j.id""", (run,))]
        return dict(enabled=enabled, ready=ready, autoPending=False, autoMessage=message,
                    masterReady=bool(current and current["finished"] == 1), jobs=jobs, runId=run)

    def manual_pages(self, cut, mode, key=None, revision=None):
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 64):
            raise ValueError("Выберите деталь для бирки.")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("Проверьте ревизию раскладки для бирок.")
        bundle = self.load_bundle(cut, mode, revision)
        with self.db() as db:
            run = self._current(db, cut, mode)
        completed_ms = (run["completed_ms"] if run and run["finished"] == 1
                        and run["revision"] == bundle["cutRevision"] else None)
        if key is not None and key != "master" and key not in bundle["parts"]:
            raise ValueError("Деталь отсутствует в текущем материале.")
        keys = (key,) if key is not None else (*bundle["parts"], "master")
        return [self._page(bundle, item, completed_ms) for item in keys]

    @staticmethod
    def _preferred_printer(db):
        row = db.execute("SELECT name FROM station_label_printer WHERE id=1").fetchone()
        return row[0] if row else ""

    def printer_settings(self):
        from label_printer import printers

        names = printers()
        with self.db() as db:
            return {"printers": names, "preferred": self._preferred_printer(db)}

    def select_printer(self, name):
        from label_printer import printers

        if not isinstance(name, str) or not 1 <= len(name) <= 256:
            raise ValueError("Выберите установленный принтер Windows.")
        names = printers()
        if name not in names:
            raise ValueError("Выбранный принтер отсутствует в списке Windows. Обновите список.")
        with self.db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO station_label_printer VALUES(1,?)", (name,))
        self.kick()
        return {"printers": names, "preferred": name}

    @staticmethod
    def _direct_print_result(request_id, status, message="", spool=None):
        result = {"requestId": request_id, "status": status, "message": message}
        if spool is not None:
            result["spoolId"] = spool
        return result

    def _check_direct_request(self, db, request_id, scope, printer):
        row = db.execute("SELECT scope,status,message,spool FROM direct_label_prints WHERE request_id=?",
                         (request_id,)).fetchone()
        if row:
            if row["scope"] != scope:
                raise WorkspaceConflict("Этот запрос уже использован для другой бирки или принтера.")
            return self._direct_print_result(request_id, row["status"], row["message"], row["spool"])
        if self._preferred_printer(db) != printer:
            raise WorkspaceConflict("Выбранный принтер изменился. Обновите выбор и подтвердите печать.")
        return None

    def print_part(self, cut, mode, revision, part_key, printer, request_id):
        from label_layout import render_label
        from label_printer import DriverFormatError, PrinterUnavailable, print_label

        if (mode not in ("operator", "demo") or type(revision) is not int or revision < 0
                or not isinstance(part_key, str) or not 1 <= len(part_key) <= 64 or part_key == "master"
                or not isinstance(printer, str) or not 1 <= len(printer) <= 256
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id)):
            raise ValueError("Проверьте деталь, ревизию и параметры печати.")
        scope = _json([cut, mode, revision, part_key, printer])
        with self.db() as db:
            existing = self._check_direct_request(db, request_id, scope, printer)
        if existing:
            return existing

        (template, fields), = self.manual_pages(cut, mode, part_key, revision)
        png, _ = render_label(template, fields)
        with self.db(write=True) as db:
            existing = self._check_direct_request(db, request_id, scope, printer)
            if existing:
                return existing
            # Persist before entering Win32. Unknown submissions never retry automatically.
            db.execute("INSERT INTO direct_label_prints(request_id,scope,status) VALUES(?,?,'submitting')",
                       (request_id, scope))

        spool = None
        try:
            spool = print_label(printer, png, template["widthMm"], template["heightMm"], self._print_title(fields, part_key))
            status, message = "submitted", "Бирка передана в очередь Windows."
        except (PrinterUnavailable, DriverFormatError) as error:
            status, message = "failed", str(error)
        except Exception:
            status, message = "unknown", UNKNOWN_SUBMISSION
        with self.db(write=True) as db:
            db.execute("UPDATE direct_label_prints SET status=?,message=?,spool=? WHERE request_id=?",
                       (status, message, spool, request_id))
        return self._direct_print_result(request_id, status, message, spool)

    @staticmethod
    def _print_title(fields, key):
        title = "Мастер-бирка" if key == "master" else f"Бирка №{fields.get('number') or key}"
        return f"{title} · заказ {fields['order']}" if fields.get("order") else title

    def pages(self, cut, mode, ids):
        if ids is not None and (not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids)):
            raise ValueError("Выберите бирки из ленты.")
        with self.db() as db:
            current = self._current(db, cut, mode)
            if not current:
                raise ValueError("Лента пока пуста.")
            run = current["id"]
            raw, completed_ms = db.execute("SELECT bundle,completed_ms FROM runs WHERE id=?", (run,)).fetchone()
            if not raw:
                raise ValueError("Данные для бирок ещё готовятся.")
            rows = db.execute("SELECT id,key FROM jobs WHERE run=? ORDER BY id", (run,)).fetchall()
        wanted = set(ids) if ids is not None else {r["id"] for r in rows}
        selected = [r for r in rows if r["id"] in wanted]
        if not selected or len(selected) != len(wanted):
            raise ValueError("Бирка отсутствует в текущем раскрое.")
        bundle = json.loads(raw)
        return [self._page(bundle, r["key"], completed_ms) for r in selected]

    @staticmethod
    def _page(bundle, key, completed_ms):
        if key == "master":
            from label_layout import master_label_fields
            return bundle["templates"]["master"], master_label_fields(bundle["master"], completed_ms)
        return bundle["templates"]["part"], bundle["parts"][key]

    def kick(self):
        self.wake.set()
        with self.worker_lock:
            if not self.stopped.is_set() and (self.thread is None or not self.thread.is_alive()):
                self.thread = Thread(target=self._work, name="plotter-labels", daemon=True)
                self.thread.start()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _work(self):
        attempted = set()
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                with self.db() as db:
                    runs = db.execute("""SELECT id,cut,mode,revision,
                        EXISTS(SELECT 1 FROM jobs WHERE run=runs.id AND status='queued') AS pending
                        FROM runs WHERE bundle IS NULL ORDER BY rowid DESC""").fetchall()
                for run in runs:
                    if self.stopped.is_set():
                        return
                    if run["id"] in attempted and not run["pending"]:
                        continue
                    try:
                        bundle = self.load_bundle(run["cut"], run["mode"], run["revision"])
                        with self.db(write=True) as db:
                            db.execute("UPDATE runs SET bundle=? WHERE id=?", (_json(bundle), run["id"]))
                            db.executemany("UPDATE pieces SET number=?,name=? WHERE run=? AND key=?",
                                           [(part["number"], part["name"], run["id"], key)
                                            for key, part in bundle["parts"].items()])
                    except (ValueError, OSError) as error:
                        attempted.add(run["id"])
                        with self.db() as db:
                            db.execute("UPDATE jobs SET message=? WHERE run=? AND status='queued'", (str(error), run["id"]))
                self._drain()
                if self.local_spool() and self.relay is not None:
                    self._relay_waiting()
            except Exception:
                LOGGER.exception("Ошибка обработки очереди бирок")
            self.wake.wait(5)

    def _drain(self):
        from label_layout import label_pdf

        bundles = {}
        with self.db() as db:
            rows = db.execute("""SELECT j.id,j.key,j.run,j.auto_spool,r.completed_ms FROM jobs j JOIN runs r ON r.id=j.run
                               WHERE j.status='queued' AND r.bundle IS NOT NULL ORDER BY j.id""").fetchall()
        for row in rows:
            if self.stopped.is_set():
                break
            run = row["run"]
            if run not in bundles:
                with self.db() as db:
                    bundles[run] = json.loads(db.execute("SELECT bundle FROM runs WHERE id=?", (run,)).fetchone()[0])
            try:
                page = self._page(bundles[run], row["key"], row["completed_ms"])
                pdf = None if row["auto_spool"] else label_pdf([page])
                auto_page = _json(page) if row["auto_spool"] else None
                status, message = "ready", ""
            except (KeyError, ValueError, OSError) as error:
                pdf, auto_page, status, message = None, None, "error", str(error)
            with self.db() as db:
                db.execute("UPDATE jobs SET status=?,message=?,pdf=?,auto_page=? WHERE id=? AND status='queued'",
                           (status, message, pdf, auto_page, row["id"]))
        if self.local_spool():
            self._spool_waiting()

    @classmethod
    def _submit_auto(cls, page, key, printer):
        from label_layout import render_label
        from label_printer import DriverFormatError, PrinterUnavailable, print_label

        template, fields = page
        try:
            png, _ = render_label(template, fields)
        except (ValueError, OSError) as error:
            return "error", str(error), None
        try:
            spool = print_label(printer, png, template["widthMm"], template["heightMm"], cls._print_title(fields, key))
            return "submitted", "", spool
        except PrinterUnavailable as error:
            return "ready", str(error), None
        except DriverFormatError as error:
            return "ready", str(error), None
        except Exception:
            return "unknown", UNKNOWN_SUBMISSION, None

    def _spool_waiting(self):
        with self.db() as db:
            rows = db.execute("""SELECT j.id,j.run,j.key,j.auto_page,r.cut,r.mode FROM jobs j JOIN runs r ON j.run=r.id
                WHERE j.auto_spool=1 AND j.status='ready' AND j.station IS NULL ORDER BY j.id""").fetchall()
        for row in rows:
            if self.stopped.is_set():
                return
            with self.db(write=True) as db:
                printer = self._preferred_printer(db)
                if not printer:
                    return
                if not self._settings(db, row["mode"], row["cut"]) or db.execute("""
                    SELECT 1 FROM jobs WHERE run=? AND auto_spool=1 AND id<?
                    AND status IN ('queued','ready','submitting') LIMIT 1""", (row["run"], row["id"])).fetchone():
                    continue
                reserved = db.execute("""UPDATE jobs SET printer=?,status='submitting',message='' WHERE id=?
                    AND status='ready' AND station IS NULL""", (printer, row["id"])).rowcount
            if not reserved:
                continue
            status, message, spool = self._submit_auto(json.loads(row["auto_page"]), row["key"], printer)
            with self.db(write=True) as db:
                db.execute("UPDATE jobs SET status=?,message=?,spool=? WHERE id=?",
                           (status, message, spool, row["id"]))
            if status == "ready":
                return

    def _relay_waiting(self):
        with self.db() as db:
            watches = db.execute("SELECT token,cut FROM auto_label_watches ORDER BY rowid").fetchall()
            preferred = self._preferred_printer(db)
        for token, cut in watches:
            while not self.stopped.is_set():
                request = {"cutId": cut, "relayKey": token, "station": self.station}
                try:
                    reply = self.relay({**request, "take": bool(preferred)})
                    job = reply.get("job")
                    if not job:
                        if reply.get("done"):
                            with self.db(write=True) as db:
                                db.execute("DELETE FROM auto_label_watches WHERE token=?", (token,))
                        break
                    status = self._spool_relay(token, job)
                    if status not in ("submitted", "unknown", "error"):
                        break
                    self.relay({**request, "jobId": job["id"], "status": status})
                except (ValueError, OSError):
                    break

    def _spool_relay(self, token, job):
        request_id = f"relay:{token}:{job['id']}"
        with self.db(write=True) as db:
            current = db.execute("SELECT status FROM direct_label_prints WHERE request_id=?", (request_id,)).fetchone()
            if current and current[0] != "ready":
                return current[0]
            printer = self._preferred_printer(db)
            if not printer or job.get("enabled") is not True:
                return "ready"
            db.execute("INSERT OR REPLACE INTO direct_label_prints(request_id,scope,status) VALUES(?,?, 'submitting')",
                       (request_id, token))
        status, message, spool = self._submit_auto(job["page"], job["key"], printer)
        with self.db(write=True) as db:
            db.execute("UPDATE direct_label_prints SET status=?,message=?,spool=? WHERE request_id=?",
                       (status, message, spool, request_id))
        return status
