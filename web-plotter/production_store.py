from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from cad import MAX_ENTITIES, MAX_POINTS, POINT_EPSILON_MM
from designer_cad import prepare_design, validate_design
from machine import PlotterError
from nesting import validate_placed_parts
from workspace import MAX_WORKSPACE_BYTES, WorkspaceConflict, validate_workspace


SCHEMA_VERSION = 1
MAX_QUANTITY = 50_000
MOSCOW_TIME = timezone(timedelta(hours=3))
KIT_LAYOUT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kit_layouts (
        kit_id TEXT NOT NULL REFERENCES registry(id) ON DELETE CASCADE,
        width_mm REAL NOT NULL, quantity INTEGER NOT NULL, revision INTEGER NOT NULL,
        basis TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kit_id, width_mm, quantity)
    )
"""


class RecordNotFound(PlotterError):
    pass


def uid():
    return uuid4().hex


def name(value, label="название"):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 255 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Укажите {label} (до 255 символов).")
    return value.strip()


def quantity(value):
    if type(value) is not int or not 1 <= value <= MAX_QUANTITY:
        raise ValueError(f"Количество должно быть целым от 1 до {MAX_QUANTITY}.")
    return value


def width_mm(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 1_000_000:
        raise ValueError("Укажите рабочую ширину полотна в миллиметрах.")
    return value


def encode(value):
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > MAX_WORKSPACE_BYTES:
        raise ValueError("Данные превышают допустимый размер.")
    return result


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError("Неверные данные записи.")
    return value


def key(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Неверный идентификатор записи.")
    return value


def check_revision(value, revision):
    if type(value) is not int or value != revision:
        raise WorkspaceConflict("Запись изменилась в другой вкладке. Заново откройте её.")


def order_date(value, label):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise ValueError(f"Укажите {label} в формате ГГГГ-ММ-ДД.")


def order_metadata(payload, previous=None):
    previous = previous or {}
    placed = (previous.get("placedOn") if previous else datetime.now(MOSCOW_TIME).date().isoformat())
    placed = order_date(payload.get("placedOn", placed), "дату постановки")
    due = order_date(payload.get("dueOn", previous.get("dueOn")), "срок выполнения")
    if placed and due and due < placed:
        raise ValueError("Срок выполнения не может быть раньше даты постановки.")
    notes = payload.get("notes", previous.get("notes", ""))
    if not isinstance(notes, str) or len(notes) > 2000 or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in notes):
        raise ValueError("Введите примечание длиной до 2000 символов.")
    return {"number": name(payload.get("number", previous.get("number")), "номер заказа"),
            "placedOn": placed, "dueOn": due, "notes": notes}


def row_data(row):
    return {**json.loads(row["data"]), **{k: row[k] for k in row.keys() if k != "data"}}


def check_composition(draft, cut):
    excluded = set(draft["excluded"])
    actual = {path.get("partNumber") for index, path in enumerate(draft["paths"])
              if index not in excluded and path["closed"]}
    if actual != set(range(1, cut["partCount"] + 1)):
        raise ValueError("В раскладке должны быть все детали из состава пункта.")


def cut_data(cut, **changes):
    fields = ("name", "widthMm", "members", "prepared", "partCount", "released")
    return {**{field: cut[field] for field in fields if field in cut}, **changes}


def new_workspace(paths, filename, width=None):
    return validate_workspace({
        "source": {"filename": filename, "warnings": []}, "paths": paths, "excluded": [],
        "view": {"zoom": 1, "focusX": 0, "focusY": 0, "grid": True, "cameraMode": "overview"},
        "optimization": {"route": True, "routeApplied": True, "join": False, **({"nesting": {
            "widthMm": width, "gapMm": 3, "marginMm": 0, "rotation": "quarter", "widthExplicit": True,
        }} if width is not None else {})},
    })


def check_kit_geometry(source, draft):
    expected, placed = defaultdict(list), defaultdict(list)
    for path in source:
        expected[path["partNumber"]].append(path)
    for path in draft["paths"]:
        placed[path.get("partNumber")].append(path)

    message = "Раскладка должна содержать все исходные резы комплекта. Меняйте только положение и угол деталей."
    if draft["excluded"] or placed.keys() != expected.keys():
        raise ValueError(message)
    for number, originals in expected.items():
        actual = placed[number]
        if len(actual) != len(originals):
            raise ValueError(message)
        angle = actual[0].get("partAngle", 0)
        cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        source_x, source_y = originals[0]["points"][0]
        placed_x, placed_y = actual[0]["points"][0]
        for before, after in zip(originals, actual):
            if (after.get("partAngle", 0) != angle or before["closed"] != after["closed"]
                    or before["layer"] != after["layer"] or bool(before.get("notch")) != bool(after.get("notch"))
                    or len(before["points"]) != len(after["points"])):
                raise ValueError(message)
            for (x, y), (u, v) in zip(before["points"], after["points"]):
                x, y = x - source_x, y - source_y
                rotated = (x * cosine - y * sine, x * sine + y * cosine)
                if math.dist(rotated, (u - placed_x, v - placed_y)) > 10 * POINT_EPSILON_MM:
                    raise ValueError(message)


class ProductionStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self, *, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self.initialize(db)
            elif version != SCHEMA_VERSION:
                raise ValueError("Версия базы реестра не поддерживается этой версией Plotter.")
            # This additive table leaves version 1 registries readable by older releases.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kit_layouts'").fetchone() is None:
                db.execute(KIT_LAYOUT_SCHEMA)
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def initialize(db):
        db.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS registry (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent TEXT REFERENCES registry(id),
                name TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS registry_parent ON registry(parent);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, published INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cuts (
                id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), position INTEGER NOT NULL,
                revision INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                UNIQUE (order_id, position)
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                cut_id TEXT NOT NULL REFERENCES cuts(id), mode TEXT NOT NULL, revision INTEGER NOT NULL,
                cut_revision INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (cut_id, mode)
            );
            PRAGMA user_version = {SCHEMA_VERSION};
            COMMIT;
        """)

    @staticmethod
    def registry_record(db, record_id):
        row = db.execute("SELECT * FROM registry WHERE id = ?", (key(record_id),)).fetchone()
        if row is None:
            raise RecordNotFound("Запись в реестре не найдена.")
        return row_data(row)

    def registry(self, record_id=None):
        with self.connect() as db:
            if record_id is not None:
                entry = self.registry_record(db, record_id)
                if entry["kind"] == "kit":
                    slots = db.execute("SELECT width_mm, quantity, revision, basis FROM kit_layouts "
                                       "WHERE kit_id=? ORDER BY width_mm, quantity", (entry["id"],)).fetchall()
                    basis = self.kit_layout_basis(db, entry) if slots else None
                    entry["layouts"] = [{"widthMm": slot["width_mm"], "quantity": slot["quantity"],
                                         "revision": slot["revision"], "valid": slot["basis"] == basis} for slot in slots]
                return entry
            rows = db.execute("SELECT id, kind, parent, name, revision, "
                              "CASE WHEN kind = 'folder' THEN data ELSE '{}' END AS data FROM registry")
            return {"entries": sorted(map(row_data, rows), key=lambda r: (r["name"].casefold(), r["id"]))}

    def save_registry(self, payload):
        record_id = payload.get("id")
        parent = payload.get("parent")
        with self.connect(write=True) as db:
            old = None
            if record_id is not None:
                old = self.registry_record(db, record_id)
                check_revision(payload.get("revision"), old["revision"])
            if payload.get("action") == "move":
                if old is None:
                    raise ValueError("Выберите запись для перемещения.")
                payload = {"kind": old["kind"], "name": old["name"], "model": old.get("model")}
            kind = payload.get("kind")
            if kind not in ("folder", "drawing", "part", "kit"):
                raise ValueError("Неверный тип записи реестра.")
            title = name(payload.get("name"))
            if old is not None:
                if old["kind"] != kind:
                    raise ValueError("Тип существующей записи изменить нельзя.")
            else:
                record_id = uid()
            ancestor = parent
            while ancestor is not None:
                if ancestor == record_id:
                    raise ValueError("Нельзя переместить папку внутрь неё самой.")
                folder = self.registry_record(db, ancestor)
                if folder["kind"] != "folder":
                    raise ValueError("Выберите папку реестра.")
                ancestor = folder["parent"]
            if kind == "folder":
                data = {"model": payload.get("model") is True}
            elif kind == "kit":
                data = {"members": self.members(db, payload.get("members", old and old["members"]), ("part",))}
            elif old is not None and "entities" not in payload:
                data = {field: old[field] for field in ("entities", "warnings", "paths") if field in old}
            else:
                entities = validate_design(payload.get("entities"))
                warnings = payload.get("warnings", [])
                if (not isinstance(warnings, list) or len(warnings) > 32
                        or any(not isinstance(w, str) or len(w) > 16_384 for w in warnings)):
                    raise ValueError("Неверные предупреждения чертежа.")
                data = {"entities": entities, "warnings": warnings}
                if kind == "part":
                    prepared = prepare_design(entities)
                    if not any(path["closed"] for path in prepared["paths"]):
                        raise ValueError("Для детали выделите замкнутую кромку и её внутренние контуры.")
                    data.update(paths=prepared["paths"], warnings=prepared["warnings"][:32])
            revision = 1 if old is None else old["revision"] + 1
            if old is None:
                db.execute("INSERT INTO registry VALUES (?, ?, ?, ?, ?, ?)",
                           (record_id, kind, parent, title, revision, encode(data)))
            else:
                db.execute("UPDATE registry SET name=?, parent=?, revision=?, data=? WHERE id=?",
                           (title, parent, revision, encode(data), record_id))
            return self.registry_record(db, record_id)

    def delete_registry(self, payload):
        record_id = key(payload.get("id"))
        with self.connect(write=True) as db:
            entry = self.registry_record(db, record_id)
            check_revision(payload.get("revision"), entry["revision"])
            if db.execute("SELECT 1 FROM registry WHERE parent=? LIMIT 1", (record_id,)).fetchone():
                raise WorkspaceConflict("Сначала переместите или удалите содержимое папки.")
            if entry["kind"] == "folder":
                rows = db.execute("SELECT data FROM orders")
                if any(json.loads(row["data"]).get("modelId") == record_id for row in rows):
                    raise WorkspaceConflict("Изделие используется в нарядах.")
            if entry["kind"] in ("part", "kit"):
                rows = db.execute("SELECT data FROM registry WHERE kind='kit' UNION ALL SELECT data FROM cuts")
                if any(member["id"] == record_id for row in rows for member in json.loads(row["data"]).get("members", ())):
                    raise WorkspaceConflict("Запись используется в комплектах или нарядах.")
            db.execute("DELETE FROM registry WHERE id=?", (record_id,))
            return {"id": record_id, "deleted": True}

    def members(self, db, values, allowed):
        if not isinstance(values, list) or not 0 < len(values) <= MAX_ENTITIES:
            raise ValueError("Выберите детали или комплекты и укажите количество.")
        members = {}
        for raw in values:
            member = object_value(raw)
            record_id = key(member.get("id"))
            row = db.execute("SELECT kind FROM registry WHERE id = ?", (record_id,)).fetchone()
            if row is None or row["kind"] not in allowed:
                raise ValueError("В составе есть запись неподходящего типа.")
            members[record_id] = quantity(members.get(record_id, 0) + quantity(member.get("quantity")))
        return [{"id": record_id, "quantity": amount} for record_id, amount in members.items()]

    def expand_parts(self, db, members, multiplier=1):
        paths, part_number, point_count = [], 0, 0
        for member in members:
            entry = self.registry_record(db, member["id"])
            repeats = quantity(member["quantity"] * multiplier)
            details = entry["members"] if entry["kind"] == "kit" else [{"id": entry["id"], "quantity": 1}]
            for detail in details:
                part = entry if entry["kind"] == "part" else self.registry_record(db, detail["id"])
                amount = quantity(detail["quantity"] * repeats)
                contours = part["paths"]
                point_count += amount * sum(len(path["points"]) for path in contours)
                if len(paths) + amount * len(contours) > MAX_ENTITIES or point_count > MAX_POINTS:
                    raise ValueError("Слишком много деталей в одном раскрое. Разделите раскладку.")
                min_x = min(x for path in contours for x, _ in path["points"])
                min_y = min(y for path in contours for _, y in path["points"])
                normalized = [{**path, "points": [[x - min_x, y - min_y] for x, y in path["points"]]}
                              for path in contours]
                for _ in range(amount):
                    part_number += 1
                    paths.extend({**path, "partNumber": part_number} for path in normalized)
        return paths

    def collect(self, values):
        with self.connect() as db:
            members = self.members(db, values, ("part", "kit"))
            return {"paths": self.expand_parts(db, members)}

    def kit_layout_basis(self, db, kit):
        digest = hashlib.sha256(encode(kit["members"]).encode("utf-8"))
        for member in kit["members"]:
            part = self.registry_record(db, member["id"])
            digest.update(encode(part["paths"]).encode("utf-8"))
        return digest.hexdigest()

    def layout_kit(self, db, record_id):
        kit = self.registry_record(db, record_id)
        if kit["kind"] != "kit":
            raise ValueError("Выберите комплект.")
        return kit

    @staticmethod
    def kit_layout_slot(db, record_id, width, amount):
        return db.execute("SELECT revision, basis, data FROM kit_layouts WHERE kit_id=? AND width_mm=? AND quantity=?",
                          (record_id, width, amount)).fetchone()

    @staticmethod
    def kit_layout_result(kit, width, amount, revision, workspace, valid):
        return {"id": kit["id"], "name": kit["name"], "widthMm": width, "quantity": amount,
                "revision": revision, "workspace": workspace, "saved": revision > 0, "valid": valid}

    @staticmethod
    def kit_layout_filename(kit, basis, amount):
        return f"kit-{kit['id']}-{basis}-{amount}.dxf"

    def kit_layout(self, record_id, width, amount):
        width, amount = width_mm(width), quantity(amount)
        with self.connect() as db:
            kit = self.layout_kit(db, record_id)
            basis = self.kit_layout_basis(db, kit)
            slot = self.kit_layout_slot(db, kit["id"], width, amount)
            valid = slot is not None and slot["basis"] == basis
            if valid:
                draft = json.loads(slot["data"])
            else:
                paths = self.expand_parts(db, [{"id": kit["id"], "quantity": amount}])
                draft = new_workspace(paths, self.kit_layout_filename(kit, basis, amount), width)
            return self.kit_layout_result(kit, width, amount, slot["revision"] if slot else 0, draft, valid)

    def save_kit_layout(self, payload, verify_layout):
        width, amount = width_mm(payload.get("widthMm")), quantity(payload.get("quantity"))
        with self.connect(write=True) as db:
            kit = self.layout_kit(db, payload.get("id"))
            slot = self.kit_layout_slot(db, kit["id"], width, amount)
            revision = slot["revision"] if slot else 0
            check_revision(payload.get("revision"), revision)
            basis = self.kit_layout_basis(db, kit)
            draft = validate_workspace(payload.get("workspace"))
            if not draft or draft["source"]["filename"] != self.kit_layout_filename(kit, basis, amount):
                raise WorkspaceConflict("Состав комплекта изменился. Заново откройте раскладку.")
            paths = self.expand_parts(db, [{"id": kit["id"], "quantity": amount}])
            check_kit_geometry(paths, draft)
            verify_layout(draft, width)
            revision += 1
            db.execute("INSERT INTO kit_layouts VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kit_id, width_mm, quantity) "
                       "DO UPDATE SET revision=excluded.revision, basis=excluded.basis, data=excluded.data",
                       (kit["id"], width, amount, revision, basis, encode(draft)))
            return self.kit_layout_result(kit, width, amount, revision, draft, True)

    def seed_kit_layout(self, db, members, multiplier, width, paths, filename, verify_layout):
        if len(members) != 1:
            return None
        member = members[0]
        kit = self.registry_record(db, member["id"])
        if kit["kind"] != "kit":
            return None
        slot = self.kit_layout_slot(db, kit["id"], width, quantity(member["quantity"] * multiplier))
        if not slot or slot["basis"] != self.kit_layout_basis(db, kit):
            return None
        try:
            draft = validate_workspace(json.loads(slot["data"]))
            if not draft:
                return None
            check_kit_geometry(paths, draft)
            verify_layout(draft, width)
        except ValueError:
            return None
        return {**draft, "source": {**draft["source"], "filename": filename}}

    @staticmethod
    def order_header(db, order_id):
        row = db.execute("SELECT * FROM orders WHERE id = ?", (key(order_id),)).fetchone()
        order = row_data(row) if row is not None else None
        if order is None or order.get("deleted"):
            raise RecordNotFound("Наряд не найден.")
        return {"placedOn": None, "dueOn": None, "notes": "", **order}

    @staticmethod
    def cut_record(db, cut_id):
        row = db.execute("SELECT * FROM cuts WHERE id = ?", (key(cut_id),)).fetchone()
        if row is None:
            raise RecordNotFound("Раскладка не найдена.")
        ProductionStore.order_header(db, row["order_id"])
        return {"released": True, **row_data(row)}

    @staticmethod
    def cuts(db, order_id):
        return [{"released": True, **row_data(row)} for row in db.execute(
            "SELECT * FROM cuts WHERE order_id = ? ORDER BY position", (order_id,))]

    def orders(self, order_id=None, *, published_only=False):
        with self.connect() as db:
            if order_id is not None:
                header = self.order_header(db, order_id)
                if published_only and not header["published"]:
                    raise RecordNotFound("Наряд ещё не отправлен оператору.")
                return {**header, "cuts": self.cuts(db, order_id)}
            entries = (row_data(row) for row in db.execute(
                "SELECT * FROM orders WHERE published >= ? ORDER BY rowid DESC", (int(published_only),)))
            orders = {order["id"]: {"placedOn": None, "dueOn": None, "notes": "", **order,
                                   "materials": [], "total": 0, "done": 0, "active": 0}
                      for order in entries if not order.get("deleted")}
            for row in db.execute("SELECT cuts.* FROM cuts JOIN orders ON orders.id=cuts.order_id "
                                  "WHERE orders.published >= ? ORDER BY cuts.order_id, cuts.position", (int(published_only),)):
                order = orders.get(row["order_id"])
                if order is None:
                    continue
                cut = {"released": bool(order["published"]), **row_data(row)}
                order["materials"].append({field: cut[field] for field in ("id", "name", "widthMm", "status", "prepared", "released")})
                order["total"] += 1
                order["done"] += cut["status"] == "done"
                order["active"] += cut["status"] == "active"
            return {"orders": list(orders.values())}

    def save_order(self, payload):
        with self.connect(write=True) as db:
            order_id = payload.get("id")
            old = self.order_header(db, order_id) if order_id is not None else None
            if old:
                check_revision(payload.get("revision"), old["revision"])
                if old["published"]:
                    raise WorkspaceConflict("Наряд уже отправлен.")
            model = self.registry_record(db, payload.get("modelId"))
            if model["kind"] != "folder" or not model.get("model"):
                raise ValueError("Выберите папку, отмеченную как изделие.")
            amount = quantity(payload.get("quantity"))
            if old and (old["quantity"] != amount or old["modelId"] != model["id"]):
                cuts = self.cuts(db, order_id)
                if cuts and old["modelId"] != model["id"]:
                    raise ValueError("Чтобы изменить изделие, сначала удалите пункты раскроя этого наряда.")
                for cut in cuts:
                    paths = self.expand_parts(db, cut["members"], amount)
                    cut_id = cut["id"]
                    workspace = new_workspace(paths, f"cut-{cut_id}.dxf", cut["widthMm"])
                    self.put_workspace(db, cut_id, "design", workspace, self.find_workspace(db, cut_id, "design"))
                    db.execute("UPDATE cuts SET revision=revision+1, data=? WHERE id=?",
                               (encode(cut_data(cut, prepared=False, released=False, partCount=paths[-1]["partNumber"])), cut_id))
                self.clear_operator_workspaces(db, order_id)
            data = {**order_metadata(payload, old), "modelId": model["id"],
                    "modelName": model["name"], "quantity": amount}
            if old is None:
                order_id = uid()
                db.execute("INSERT INTO orders VALUES (?, 1, 0, ?)", (order_id, encode(data)))
            else:
                db.execute("UPDATE orders SET revision=revision+1, data=? WHERE id=?", (encode(data), order_id))
            return {**self.order_header(db, order_id), "cuts": self.cuts(db, order_id)}

    def update_order_metadata(self, payload):
        order_id = key(payload.get("id"))
        with self.connect(write=True) as db:
            order = self.order_header(db, order_id)
            check_revision(payload.get("revision"), order["revision"])
            data = order_metadata(payload, order)
            if data != {field: order[field] for field in data}:
                raw = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()["data"]
                db.execute("UPDATE orders SET revision=revision+1, data=? WHERE id=?",
                           (encode({**json.loads(raw), **data}), order_id))
            return {**self.order_header(db, order_id), "cuts": self.cuts(db, order_id)}

    def delete_order(self, payload):
        order_id = key(payload.get("id"))
        with self.connect(write=True) as db:
            order = self.order_header(db, order_id)
            check_revision(payload.get("revision"), order["revision"])
            active = db.execute("SELECT 1 FROM cuts WHERE order_id=? AND status='active' LIMIT 1",
                                (order_id,)).fetchone()
            if active is not None:
                raise WorkspaceConflict("Завершите открытый раскрой перед удалением наряда.")
            raw = db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()["data"]
            data = json.loads(raw)
            data["deleted"] = True
            db.execute("UPDATE orders SET revision=revision+1, data=? WHERE id=?", (encode(data), order_id))
            return {"id": order_id, "deleted": True}

    def save_cut(self, payload, verify_layout=None):
        with self.connect(write=True) as db:
            order = self.order_header(db, payload.get("orderId"))
            check_revision(payload.get("orderRevision"), order["revision"])
            cut_id = payload.get("id")
            old = self.cut_record(db, cut_id) if cut_id is not None else None
            if old is not None and old["order_id"] != order["id"]:
                raise ValueError("Эта раскладка относится к другому наряду.")
            if old is not None and old["status"] != "pending":
                raise WorkspaceConflict("После начала раскроя нельзя изменять или удалять его пункт.")
            if payload.get("action") == "remove-cut":
                if old is None:
                    raise ValueError("Выберите пункт раскроя.")
                db.execute("DELETE FROM workspaces WHERE cut_id=?", (cut_id,))
                db.execute("DELETE FROM cuts WHERE id=?", (cut_id,))
            else:
                members = self.members(db, payload.get("members"), ("part", "kit"))
                data = {"name": name(payload.get("name"), "название ткани"),
                        "widthMm": width_mm(payload.get("widthMm")), "members": members}
                previous = self.find_workspace(db, cut_id, "design") if old else None
                same_layout = old is not None and previous and all(old[field] == data[field] for field in ("widthMm", "members"))
                if same_layout:
                    if old["name"] != data["name"]:
                        db.execute("UPDATE cuts SET revision=revision+1, data=? WHERE id=?",
                                   (encode(cut_data(old, name=data["name"])), cut_id))
                else:
                    paths = self.expand_parts(db, members, order["quantity"])
                    data.update(prepared=False, released=False, partCount=paths[-1]["partNumber"])
                    workspace = None
                    if old is None:
                        cut_id = uid()
                        workspace = self.seed_kit_layout(db, members, order["quantity"], data["widthMm"], paths,
                                                         f"cut-{cut_id}.dxf", verify_layout or verify_arrangement)
                        data["prepared"] = workspace is not None
                        position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM cuts WHERE order_id=?",
                                              (order["id"],)).fetchone()[0]
                        db.execute("INSERT INTO cuts VALUES (?, ?, ?, 1, 'pending', ?)",
                                   (cut_id, order["id"], position, encode(data)))
                    else:
                        db.execute("UPDATE cuts SET revision=revision+1, data=? WHERE id=?",
                                   (encode(data), cut_id))
                        db.execute("DELETE FROM workspaces WHERE cut_id=? AND mode IN ('operator','demo')", (cut_id,))
                    if workspace is None:
                        workspace = new_workspace(paths, f"cut-{cut_id}.dxf", data["widthMm"])
                    self.put_workspace(db, cut_id, "design", workspace, previous)
            db.execute("UPDATE orders SET revision=revision+1 WHERE id=?", (order["id"],))
            return {**self.order_header(db, order["id"]), "cuts": self.cuts(db, order["id"]), "cutId": cut_id}

    @staticmethod
    def find_workspace(db, cut_id, mode):
        row = db.execute("SELECT revision, cut_revision, data FROM workspaces WHERE cut_id=? AND mode=?",
                         (cut_id, mode)).fetchone()
        if row is None:
            return None
        return {"revision": row["revision"], "cutRevision": row["cut_revision"],
                "workspace": json.loads(row["data"])}

    @staticmethod
    def clear_operator_workspaces(db, order_id):
        db.execute("DELETE FROM workspaces WHERE mode IN ('operator','demo') AND cut_id IN "
                   "(SELECT id FROM cuts WHERE order_id=?)", (order_id,))

    def put_workspace(self, db, cut_id, mode, workspace, previous):
        content = encode(workspace)
        revision = previous["revision"] + 1 if previous else 1
        # Retain the cutting revision for view-only updates.
        def geometry(draft):
            return {field: value for field, value in draft.items() if field != "view"}
        cut_revision = (previous["cutRevision"] + (geometry(previous["workspace"]) != geometry(workspace))
                        if previous else 1)
        db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?) ON CONFLICT(cut_id, mode) DO UPDATE "
                   "SET revision=excluded.revision, cut_revision=excluded.cut_revision, data=excluded.data",
                   (cut_id, mode, revision, cut_revision, content))
        return self.find_workspace(db, cut_id, mode)

    def matching_workspace(self, db, cut, mode):
        if mode not in ("design", "preview", "operator", "demo"):
            raise ValueError("Неизвестное рабочее место.")
        order = self.order_header(db, cut["order_id"])
        if mode == "design" and order["published"] and (cut["released"] or cut["status"] != "pending"):
            raise WorkspaceConflict("Наряд уже отправлен оператору.")
        if mode in ("operator", "demo"):
            if not order["published"]:
                raise WorkspaceConflict("Наряд ещё не отправлен оператору.")
            if not cut["released"]:
                raise WorkspaceConflict("Конструктор обновляет раскладку. Дождитесь её отправки.")
        base = self.find_workspace(db, cut["id"], "design")
        if mode in ("design", "preview"):
            return base
        work = self.find_workspace(db, cut["id"], mode)
        return work or {"revision": 0, "cutRevision": base["cutRevision"], "workspace": base["workspace"]}

    def workspace(self, cut_id, mode):
        with self.connect() as db:
            cut = self.cut_record(db, cut_id)
            return {**self.matching_workspace(db, cut, mode), "status": cut["status"], "name": cut["name"]}

    def save_workspace(self, payload, mode, verify_layout):
        with self.connect(write=True) as db:
            cut = self.cut_record(db, payload.get("cutId"))
            current = self.matching_workspace(db, cut, mode)
            check_revision(payload.get("revision"), current["revision"])
            if mode == "preview" or (mode == "operator" and cut["status"] == "done"):
                raise WorkspaceConflict("Этот раскрой открыт для просмотра.")
            draft = validate_workspace(payload.get("workspace"))
            if not draft or draft["source"]["filename"] != current["workspace"]["source"]["filename"]:
                raise ValueError("Сохраните раскладку для этого пункта наряда.")
            if mode == "design" and draft["optimization"].get("nesting", {}).get("widthMm") != cut["widthMm"]:
                raise ValueError("Ширина должна совпадать с указанной в пункте наряда.")
            if mode == "operator" and cut["status"] == "pending":
                raise WorkspaceConflict("Сначала откройте раскрой из наряда.")
            if mode == "design":
                try:
                    check_composition(draft, cut)
                    verify_layout(draft, cut["widthMm"])
                    prepared = True
                except (ValueError, PlotterError):
                    prepared = False
                if cut["prepared"] != prepared:
                    db.execute("UPDATE cuts SET data=? WHERE id=?", (encode(cut_data(cut, prepared=prepared)), cut["id"]))
                db.execute("DELETE FROM workspaces WHERE cut_id=? AND mode IN ('operator','demo')", (cut["id"],))
            previous = self.find_workspace(db, cut["id"], mode)
            if not previous:
                previous = current
            return self.put_workspace(db, cut["id"], mode, draft, previous)

    def verify_cut(self, db, cut, verify_layout):
        draft = self.find_workspace(db, cut["id"], "design")["workspace"]
        try:
            check_composition(draft, cut)
            verify_layout(draft, cut["widthMm"])
        except (ValueError, PlotterError) as error:
            raise ValueError(f'{cut["name"]}: {error}') from error
        db.execute("UPDATE cuts SET data=? WHERE id=?", (encode(cut_data(cut, prepared=True, released=True)), cut["id"]))

    def publish_order(self, payload, verify_layout):
        with self.connect(write=True) as db:
            order = self.order_header(db, payload.get("id"))
            if order["published"]:
                return {**order, "cuts": self.cuts(db, order["id"])}
            check_revision(payload.get("revision"), order["revision"])
            cuts = self.cuts(db, order["id"])
            if not cuts:
                raise ValueError("Добавьте хотя бы один пункт раскроя.")
            for cut in cuts:
                self.verify_cut(db, cut, verify_layout)
            db.execute("UPDATE orders SET published=1, revision=revision+1 WHERE id=?", (order["id"],))
            return {**self.order_header(db, order["id"]), "cuts": self.cuts(db, order["id"])}

    def publish_cut(self, payload, verify_layout):
        with self.connect(write=True) as db:
            order = self.order_header(db, payload.get("orderId"))
            check_revision(payload.get("orderRevision"), order["revision"])
            cut = self.cut_record(db, payload.get("id"))
            if cut["order_id"] != order["id"]:
                raise ValueError("Эта раскладка относится к другому наряду.")
            if not order["published"] or cut["released"] or cut["status"] != "pending":
                raise WorkspaceConflict("Этот пункт не ожидает отправки оператору.")
            self.verify_cut(db, cut, verify_layout)
            db.execute("UPDATE orders SET revision=revision+1 WHERE id=?", (order["id"],))
            return {**self.order_header(db, order["id"]), "cuts": self.cuts(db, order["id"])}

    def reopen_order(self, payload):
        order_id = key(payload.get("id"))
        with self.connect(write=True) as db:
            order = self.order_header(db, order_id)
            check_revision(payload.get("revision"), order["revision"])
            if not order["published"]:
                raise WorkspaceConflict("Наряд уже открыт для редактирования.")
            cuts = self.cuts(db, order_id)
            if any(cut["status"] != "pending" for cut in cuts):
                raise WorkspaceConflict("Наряд нельзя вернуть в черновики после начала раскроя.")
            self.clear_operator_workspaces(db, order_id)
            db.execute("UPDATE orders SET published=0, revision=revision+1 WHERE id=?", (order_id,))
            return {**self.order_header(db, order_id), "cuts": cuts}

    def progress(self, cut_id, action, mode, revision=None):
        with self.connect(write=True) as db:
            cut = self.cut_record(db, cut_id)
            current = self.matching_workspace(db, cut, mode)
            status = cut["status"]
            if mode == "operator":
                if action == "start" and status == "pending":
                    status = "active"
                if action == "finish" and status != "done":
                    if status != "active":
                        raise WorkspaceConflict("Сначала приступите к раскрою.")
                    check_revision(revision, current["revision"])
                    status = "done"
                if status != cut["status"]:
                    db.execute("UPDATE cuts SET status=? WHERE id=?", (status, cut_id))
            elif action != "start" or mode != "demo":
                raise WorkspaceConflict("В демо-режиме ход наряда не сохраняется.")
            return {**current, "status": status, "name": cut["name"],
                    "opened": cut["status"] == "pending" and status == "active"}

    def release_cut(self, cut_id, revision):
        with self.connect(write=True) as db:
            cut = self.cut_record(db, cut_id)
            current = self.matching_workspace(db, cut, "operator")
            check_revision(revision, current["revision"])
            if cut["status"] != "active":
                raise WorkspaceConflict("Этот раскрой сейчас не открыт на плоттере.")
            current = self.put_workspace(db, cut_id, "operator", current["workspace"], current)
            db.execute("UPDATE cuts SET status='pending' WHERE id=?", (cut_id,))
            return {**current, "status": "pending", "name": cut["name"]}

    def backup(self, destination):
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as source, \
                closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def check(self):
        with self.connect() as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("База реестра содержит повреждённые записи.")


def verify_arrangement(draft, width):
    excluded = set(draft["excluded"])
    paths = [path for index, path in enumerate(draft["paths"]) if index not in excluded]
    if not paths:
        raise ValueError("Добавьте детали в раскладку.")
    settings = draft["optimization"].get("nesting", {})
    if settings.get("widthMm") != width:
        raise ValueError("Установите рабочую ширину полотна.")
    validate_placed_parts(paths, width=width, margin=settings["marginMm"], gap=settings["gapMm"])
