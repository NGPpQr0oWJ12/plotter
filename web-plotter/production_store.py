from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import sqlite3
from sys import float_info
from uuid import uuid4
from zlib import error as CompressionError

from cad import MAX_ABS_COORDINATE_MM, MAX_ENTITIES, MAX_POINTS, POINT_EPSILON_MM
from designer_cad import center_part_geometry, prepare_design, validate_design
from machine import PlotterError
from nesting import validate_placed_parts
from order_metrics import cut_metrics, plan_snapshot, summarize_cuts, utc_now
from registry_archive import MAX_REGISTRY_JSON
from sketchup_design import MAX_MODEL_BYTES, markup
from workspace import (MAX_REVISION, MAX_WORKSPACE_BYTES, PART_LABEL_FIELDS, WORKSPACE_REVISION_CONFLICT,
                       WorkspaceConflict, drawing_paths, validate_workspace)


SCHEMA_VERSION = 1
MAX_QUANTITY = 50_000
REGISTRY_BACKUP_VERSION = 1
MOSCOW_TIME = timezone(timedelta(hours=3))
RECORD_REVISION_MESSAGE = "Запись изменилась в другой вкладке. Заново откройте её."
KIT_LAYOUT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kit_layouts (
        kit_id TEXT NOT NULL REFERENCES registry(id) ON DELETE CASCADE,
        width_mm REAL NOT NULL, quantity INTEGER NOT NULL, revision INTEGER NOT NULL,
        basis TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kit_id, width_mm, quantity)
    )
"""
CUT_STATISTICS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS cut_statistics (
        cut_id TEXT PRIMARY KEY REFERENCES cuts(id) ON DELETE CASCADE,
        workspace_mode TEXT, cut_revision INTEGER, width_mm REAL, prepared INTEGER,
        metrics TEXT, started_at TEXT, finished_at TEXT
    )
"""
CUTS_WITH_STATISTICS = """
    SELECT cuts.*, stats.metrics AS _metrics, stats.started_at AS _startedAt, stats.finished_at AS _finishedAt
    FROM cuts LEFT JOIN cut_statistics AS stats ON stats.cut_id=cuts.id
"""
MODEL_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sketchup_models (
        id TEXT PRIMARY KEY, expires INTEGER NOT NULL, data BLOB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS drawing_models (
        drawing_id TEXT PRIMARY KEY REFERENCES registry(id) ON DELETE CASCADE,
        model_id TEXT NOT NULL REFERENCES sketchup_models(id), markup TEXT NOT NULL
    )""",
)


class RecordNotFound(PlotterError):
    pass


def uid():
    return uuid4().hex


def name(value, label="название"):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 255 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Укажите {label} (до 255 символов).")
    return value.strip()


def part_legend(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"developer", "comments", "date"}:
        raise ValueError("Некорректная легенда детали.")
    developer, comments = value.get("developer", ""), value.get("comments", "")
    if not isinstance(developer, str) or len(developer) > 255 or any(ord(ch) < 32 for ch in developer):
        raise ValueError("Укажите разработчика длиной до 255 символов.")
    if (not isinstance(comments, str) or len(comments) > 2000
            or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in comments)):
        raise ValueError("Особые отметки должны содержать до 2000 символов.")
    return {"developer": developer.strip(), "date": datetime.now(MOSCOW_TIME).strftime("%m.%Y"), "comments": comments}


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


def pattern_record(value):
    value = object_value(value)
    direction, side = value.get("direction", "free"), value.get("side", "")
    if direction not in ("free", "grain", "nap") or side not in ("", "left", "right"):
        raise ValueError("Проверьте сторону детали и направление ткани.")
    sewing = value.get("sewing", [])
    if not isinstance(sewing, list) or len(sewing) > 800:
        raise ValueError("В карте швов слишком много записей.")
    edges = []
    for edge in sewing:
        edge = object_value(edge)
        marks = edge.get("marks", [])
        mark_ids = edge.get("markIds", [])
        if not isinstance(marks, list) or len(marks) > 100:
            raise ValueError("Проверьте обозначения насечек.")
        if not isinstance(mark_ids, list) or mark_ids and len(mark_ids) != len(marks):
            raise ValueError("Проверьте соответствие номеров насечек.")
        method = edge.get("method", "stitch")
        if method not in ("stitch", "closure"):
            raise ValueError("Некорректный способ соединения кромок.")
        edges.append({"name": name(edge.get("name"), "название шва"),
                      "other": name(edge.get("other"), "номер сопряжённой детали"),
                      "marks": [name(mark, "обозначение насечки") for mark in marks],
                      "markIds": [name(mark, "идентификатор насечки") for mark in mark_ids], "method": method})
    signature = value.get("seamSignature")
    if signature is not None and (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)):
        raise ValueError("Некорректная версия линии стачивания.")
    return {"id": name(value.get("id"), "идентификатор лекала"),
            "number": name(value.get("number"), "номер детали"),
            "name": name(value.get("name"), "название детали"),
            "fabricName": name(value["fabricName"], "название ткани") if value.get("fabricName") else "",
            "direction": direction, "side": side, "quantity": quantity(value.get("quantity", 1)), "sewing": edges,
            **({"seamSignature": signature} if signature else {}),
            **({"checkSewing": True} if value.get("checkSewing") is True else {})}


def key(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Неверный идентификатор записи.")
    return value


def check_revision(value, revision, *, mismatch_code=None):
    if type(value) is not int or value != revision:
        code = mismatch_code if type(value) is int and 0 <= value <= MAX_REVISION else None
        raise WorkspaceConflict(RECORD_REVISION_MESSAGE, code=code)


def backup_revision(value, previous=None):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ValueError("Некорректная версия записи в резервной копии реестра.")
    if previous is not None:
        return backup_revision(max(value, previous) + 1)
    return value


def backup_object(value, fields):
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError("Некорректная структура резервной копии реестра.")
    return value


def backup_list(value):
    if not isinstance(value, list) or len(value) > MAX_ENTITIES:
        raise ValueError("Превышен предел количества записей резервной копии реестра.")
    return value


def tree_order(records):
    children = defaultdict(list)
    for identifier, record in records.items():
        parent = record.get("parent")
        if parent is not None and (not isinstance(parent, str) or parent not in records):
            raise ValueError("Резервная копия содержит ссылку на отсутствующего родителя.")
        children[parent].append(identifier)
    ordered = children[None].copy()
    for identifier in ordered:
        ordered.extend(children[identifier])
    if len(ordered) != len(records):
        raise ValueError("Резервная копия содержит циклическую иерархию.")
    return ordered


def validate_registry_entry(record):
    kind = record["kind"]
    data = object_value(record["data"])
    if kind == "folder":
        if data.keys() - {"model"} or type(data.get("model", False)) is not bool:
            raise ValueError("Некорректная папка в резервной копии реестра.")
    elif kind == "kit":
        backup_object(data, {"members"})
        members = backup_list(data["members"])
        identifiers = set()
        for member in members:
            backup_object(member, {"id", "quantity"})
            identifiers.add(key(member["id"]))
            quantity(member["quantity"])
        if not members or len(identifiers) != len(members):
            raise ValueError("Некорректный состав комплекта в резервной копии реестра.")
    elif kind in ("drawing", "part"):
        allowed = {"entities", "warnings"} | ({"patterns"} if kind == "drawing" else {"paths", "pattern", "partLegend"})
        if data.keys() - allowed:
            raise ValueError("Некорректные свойства чертежа или детали в резервной копии реестра.")
        entities = validate_design(data.get("entities"))
        warnings = data.get("warnings", [])
        if (not isinstance(warnings, list) or len(warnings) > 32
                or any(not isinstance(warning, str) or len(warning) > 16_384 for warning in warnings)):
            raise ValueError("Некорректные предупреждения чертежа в резервной копии реестра.")
        if kind == "drawing" and "patterns" in data:
            groups = {entity["groupPath"][1] for entity in entities
                      if len(entity.get("groupPath", [])) >= 2 and entity["groupPath"][0] == "Лекала 3D"}
            for identifier, pattern in object_value(data["patterns"]).items():
                if identifier not in groups:
                    raise ValueError("Лекало ссылается на отсутствующую группу чертежа.")
                pattern_record(pattern)
        if kind == "part":
            paths = drawing_paths(data.get("paths"))
            if (not any(path["closed"] for path in paths)
                    or any(abs(value) > MAX_ABS_COORDINATE_MM for path in paths for point in path["points"] for value in point)):
                raise ValueError("Некорректные контуры детали в резервной копии реестра.")
            if "pattern" in data and pattern_record(data["pattern"])["direction"] != "free":
                if not any(entity["layer"] == "Долевая" and entity.get("reference") and entity["type"] == "line" for entity in entities):
                    raise ValueError("Для направленной ткани нужна линия долевой.")
            if "partLegend" in data:
                legend = backup_object(data["partLegend"], {"developer", "date", "comments"})
                if not isinstance(legend["date"], str) or not re.fullmatch(r"(?:0[1-9]|1[0-2])\.[0-9]{4}", legend["date"]):
                    raise ValueError("Некорректная дата легенды в резервной копии реестра.")
                part_legend(legend)
    else:
        raise ValueError("Неизвестный тип записи в резервной копии реестра.")


def backup_model(data):
    try:
        blob = b64decode(data, validate=True)
        with gzip.GzipFile(fileobj=BytesIO(blob)) as stream:
            raw = stream.read(MAX_MODEL_BYTES + 1)
        if len(raw) > MAX_MODEL_BYTES:
            raise ValueError("Модель SketchUp превышает допустимый размер.")
        model = object_value(json.loads(raw))
    except (Base64Error, OSError, EOFError, CompressionError, UnicodeError, RecursionError, ValueError) as error:
        raise ValueError("Повреждённая модель SketchUp в резервной копии реестра.") from error

    def coordinates(value, count):
        return isinstance(value, list) and len(value) == count and all(
            type(axis) in (int, float) and -float_info.max <= axis <= float_info.max for axis in value)

    def indices(value, count, size):
        return isinstance(value, list) and len(value) == count and all(type(index) is int and 0 <= index < size for index in value)

    def text(value):
        return isinstance(value, str) and bool(value)

    bounds = model.get("bounds")
    if (not text(model.get("name")) or not isinstance(bounds, list) or len(bounds) != 2
            or not all(coordinates(point, 3) for point in bounds) or any(low > high for low, high in zip(*bounds))):
        raise ValueError("Некорректная модель SketchUp в резервной копии реестра.")
    nodes = {}
    for node in backup_list(model.get("nodes")):
        node = object_value(node)
        identifier = node.get("id")
        vertices, triangles, faces, edges = (node.get(field) for field in ("vertices", "triangles", "faces", "edges"))
        if (not text(identifier) or identifier in nodes or not text(node.get("name"))
                or type(node.get("visible", True)) is not bool or not all(isinstance(items, list) for items in (vertices, triangles, faces, edges))
                or len(triangles) != len(faces) or not all(coordinates(point, 3) for point in vertices) or not all(map(text, faces))
                or "transform" in node and not coordinates(node["transform"], 16)
                or any(not indices(item, 3, len(vertices)) for item in triangles)
                or any(not indices(item, 2, len(vertices)) for item in edges)):
            raise ValueError("Некорректная геометрия SketchUp в резервной копии реестра.")
        if "faceLoops" in node:
            for loops in object_value(node["faceLoops"]).values():
                if not isinstance(loops, list) or any(not isinstance(loop, list) or len(loop) < 3
                        or not indices(loop, len(loop), len(vertices)) for loop in loops):
                    raise ValueError("Некорректный контур грани SketchUp в резервной копии реестра.")
        if "faceFlags" in node:
            for flags in object_value(node["faceFlags"]).values():
                if type(object_value(flags).get("visible", True)) is not bool:
                    raise ValueError("Некорректная видимость грани SketchUp в резервной копии реестра.")
        if "edgeFlags" in node and (not isinstance(node["edgeFlags"], list) or len(node["edgeFlags"]) != len(edges)
                                    or any(any(type(object_value(flag).get(field, False)) is not bool
                                               for field in ("visible", "soft", "smooth")) for flag in node["edgeFlags"])):
            raise ValueError("Некорректные свойства рёбер SketchUp в резервной копии реестра.")
        if (identifier == "root") != (node.get("parent") is None):
            raise ValueError("Некорректный корень модели SketchUp в резервной копии реестра.")
        nodes[identifier] = node
    tree_order(nodes)
    if not any(node["triangles"] for node in nodes.values()):
        raise ValueError("В модели SketchUp из резервной копии отсутствуют поверхности.")
    return blob


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
    designation = payload.get("designation", previous.get("designation", ""))
    if not isinstance(designation, str) or len(designation) > 255 or any(ord(ch) < 32 for ch in designation):
        raise ValueError("Введите обозначение изделия длиной до 255 символов.")
    return {"number": name(payload.get("number", previous.get("number")), "номер заказа"),
            "placedOn": placed, "dueOn": due, "notes": notes, "designation": designation.strip()}


def row_data(row):
    return {**json.loads(row["data"]), **{k: row[k] for k in row.keys() if k != "data"}}


def cut_with_statistics(row, *, released=True):
    cut = {"released": released, **row_data(row)}
    metrics = cut.pop("_metrics")
    cut["stats"] = cut_metrics(json.loads(metrics) if metrics else None, cut["status"],
                               cut.pop("_startedAt"), cut.pop("_finishedAt"))
    return cut


def check_composition(draft, cut):
    excluded = set(draft["excluded"])
    actual = {path.get("partNumber") for index, path in enumerate(draft["paths"])
              if index not in excluded and path["closed"]}
    if actual != set(range(1, cut["partCount"] + 1)):
        raise ValueError("В раскладке должны быть все детали из состава пункта.")


def cut_data(cut, **changes):
    fields = ("name", "widthMm", "members", "prepared", "partCount", "released", "operatorAssignedWidth")
    return {**{field: cut[field] for field in fields if field in cut}, **changes}


def new_workspace(paths, filename, width=None):
    return validate_workspace({
        "source": {"filename": filename, "warnings": []}, "paths": paths, "excluded": [],
        "view": {"zoom": 1, "focusX": 0, "focusY": 0, "grid": True, "cameraMode": "overview"},
        "optimization": {"route": True, "routeApplied": True, "join": False, **({"nesting": {
            "widthMm": width, "gapMm": 3, "marginMm": 0, "rotation": "quarter", "widthExplicit": True,
        }} if width is not None else {})},
    })


def paths_by_part(paths):
    groups = defaultdict(list)
    for path in paths:
        groups[path.get("partNumber")].append(path)
    return groups


def same_part_geometry(originals, actual):
    if len(actual) != len(originals):
        return False
    angle = actual[0].get("partAngle", 0)
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    source_x, source_y = originals[0]["points"][0]
    placed_x, placed_y = actual[0]["points"][0]
    for before, after in zip(originals, actual):
        if (after.get("partAngle", 0) != angle or before["closed"] != after["closed"]
                or before["layer"] != after["layer"] or bool(before.get("notch")) != bool(after.get("notch"))
                or len(before["points"]) != len(after["points"])):
            return False
        for (x, y), (u, v) in zip(before["points"], after["points"]):
            x, y = x - source_x, y - source_y
            rotated = (x * cosine - y * sine, x * sine + y * cosine)
            if math.dist(rotated, (u - placed_x, v - placed_y)) > 10 * POINT_EPSILON_MM:
                return False
    return True


def restore_part_labels(workspace, source, *, check_geometry=False):
    originals = paths_by_part(source)
    labels = {}
    for number, actual in paths_by_part(workspace["paths"]).items():
        if number is None:
            continue
        present = {field: path[field] for path in actual for field in PART_LABEL_FIELDS if field in path}
        original = originals.get(number)
        if (("partName" not in present or "partDisplayNumber" not in present) and original
                and (not check_geometry or same_part_geometry(original, actual))):
            present = {**{field: path[field] for path in original for field in PART_LABEL_FIELDS if field in path},
                       **present}
        labels[number] = present
    return {**workspace, "paths": [{**labels.get(path.get("partNumber"), {}), **path} for path in workspace["paths"]]}


def missing_part_labels(workspace):
    return any("partNumber" in path and ("partName" not in path or "partDisplayNumber" not in path)
               for path in workspace["paths"])


def check_kit_geometry(source, draft):
    expected, placed = paths_by_part(source), paths_by_part(draft["paths"])

    message = "Раскладка должна содержать все исходные резы комплекта. Меняйте только положение и угол деталей."
    nap_angle = 180 if draft["optimization"].get("napReversed") else 0
    if draft["excluded"] or placed.keys() != expected.keys():
        raise ValueError(message)
    for number, originals in expected.items():
        actual = placed[number]
        if len(actual) != len(originals):
            raise ValueError(message)
        angle = actual[0].get("partAngle", 0)
        if originals[0].get("partRotation") == "none" and abs(math.remainder(angle - nap_angle, 360)) > .001:
            raise ValueError("Кромка направленной ткани должна идти вдоль рулона.")
        if not same_part_geometry(originals, actual):
            raise ValueError(message)


class ProductionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.layout_metrics = None

    def configure_metrics(self, callback):
        self.layout_metrics = callback

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
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cut_statistics'").fetchone() is None:
                db.execute(CUT_STATISTICS_SCHEMA)
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

    @staticmethod
    def model_attachment(db, drawing_id):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='drawing_models'").fetchone() is None:
            return {}
        row = db.execute("SELECT model_id, markup FROM drawing_models WHERE drawing_id=?", (drawing_id,)).fetchone()
        return {"model3d": {"id": row["model_id"], "state": json.loads(row["markup"])}} if row else {}

    def stage_model(self, model):
        data = json.dumps(model, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        model_id = uid()
        with self.connect(write=True) as db:
            for statement in MODEL_SCHEMA:
                db.execute(statement)
            db.execute("DELETE FROM sketchup_models WHERE expires < unixepoch() AND NOT EXISTS "
                       "(SELECT 1 FROM drawing_models WHERE model_id=sketchup_models.id)")
            db.execute("INSERT INTO sketchup_models VALUES (?, unixepoch() + 86400, ?)",
                       (model_id, gzip.compress(data, compresslevel=3)))
        return model_id

    def model(self, model_id):
        model_id = key(model_id)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='sketchup_models'").fetchone():
                row = db.execute("SELECT data FROM sketchup_models WHERE id=? AND "
                                 "(expires >= unixepoch() OR EXISTS "
                                 "(SELECT 1 FROM drawing_models WHERE model_id=?))", (model_id, model_id)).fetchone()
                if row:
                    return bytes(row["data"])
        raise RecordNotFound("Модель не найдена. Импортируйте файл SketchUp ещё раз.")

    def export_registry(self):
        with self.connect() as db:
            entries = [{**dict(row), "data": json.loads(row["data"])}
                       for row in db.execute("SELECT * FROM registry ORDER BY id")]
            layouts = []
            bases = {}
            for slot in db.execute("SELECT * FROM kit_layouts ORDER BY kit_id, width_mm, quantity"):
                kit_id = slot["kit_id"]
                if kit_id not in bases:
                    bases[kit_id] = self.kit_layout_basis(db, self.layout_kit(db, kit_id))
                layouts.append({"kitId": kit_id, "widthMm": slot["width_mm"], "quantity": slot["quantity"],
                                "revision": slot["revision"], "basis": slot["basis"], "valid": slot["basis"] == bases[kit_id],
                                "workspace": json.loads(slot["data"])})
            models, links = [], []
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='drawing_models'").fetchone():
                links = [{"drawingId": row["drawing_id"], "modelId": row["model_id"], "markup": json.loads(row["markup"])}
                         for row in db.execute("SELECT * FROM drawing_models ORDER BY drawing_id")]
                models = [{"id": row["id"], "data": b64encode(row["data"]).decode("ascii")}
                          for row in db.execute("SELECT id, data FROM sketchup_models WHERE EXISTS "
                                                "(SELECT 1 FROM drawing_models WHERE model_id=sketchup_models.id) ORDER BY id")]
            return {"version": REGISTRY_BACKUP_VERSION, "entries": entries, "kitLayouts": layouts,
                    "models": models, "drawingModels": links}

    @staticmethod
    def check_registry_restore_references(db, records, revisions):
        for row in db.execute("SELECT data FROM orders"):
            model_id = json.loads(row["data"]).get("modelId")
            if model_id is None:
                continue
            entry = records.get(model_id)
            if not entry or entry["kind"] != "folder" or entry["data"].get("model") is not True:
                raise WorkspaceConflict("Резервная копия не содержит изделие из существующего наряда.")
        for row in db.execute("SELECT data FROM cuts"):
            for member in json.loads(row["data"]).get("members", []):
                identifier = member["id"]
                entry = records.get(identifier)
                previous = revisions.get(identifier)
                if not entry or entry["kind"] not in ("part", "kit") or (previous and entry["kind"] != previous["kind"]):
                    raise WorkspaceConflict("Резервная копия изменяет тип или удаляет деталь или комплект из существующего раскроя.")

    def restore_registry(self, snapshot):
        backup_object(snapshot, {"version", "entries", "kitLayouts", "models", "drawingModels"})
        if type(snapshot["version"]) is not int or snapshot["version"] != REGISTRY_BACKUP_VERSION:
            raise ValueError("Версия резервной копии реестра не поддерживается.")
        total_bytes = 0

        def account(size):
            nonlocal total_bytes
            total_bytes += size
            if total_bytes > MAX_REGISTRY_JSON:
                raise ValueError("Резервная копия реестра превышает допустимый размер.")

        records, contents = {}, {}
        for record in backup_list(snapshot["entries"]):
            backup_object(record, {"id", "kind", "parent", "name", "revision", "data"})
            identifier = key(record["id"])
            if identifier in records:
                raise ValueError("Резервная копия содержит повторяющийся идентификатор записи.")
            backup_revision(record["revision"])
            record = {**record, "name": name(record["name"])}
            validate_registry_entry(record)
            data = encode(record["data"])
            account(len(data.encode("utf-8")) + len(record["name"].encode("utf-8")) + 128)
            records[identifier], contents[identifier] = record, data
        ordered = tree_order(records)
        for record in records.values():
            parent = record["parent"]
            if parent is not None and records[parent]["kind"] != "folder":
                raise ValueError("Родителем записи реестра должна быть папка.")
            if record["kind"] == "kit" and any(records.get(member["id"], {}).get("kind") != "part"
                                                for member in record["data"]["members"]):
                raise ValueError("Комплект ссылается на отсутствующую деталь резервной копии.")

        slots = {}
        for slot in backup_list(snapshot["kitLayouts"]):
            backup_object(slot, {"kitId", "widthMm", "quantity", "revision", "basis", "valid", "workspace"})
            kit_id, amount = key(slot["kitId"]), quantity(slot["quantity"])
            width = slot["widthMm"]
            if type(width) not in (int, float) or not 1 <= width <= 1_000_000:
                raise ValueError("Некорректная ширина раскладки комплекта в резервной копии.")
            selector = kit_id, width, amount
            if (selector in slots or records.get(kit_id, {}).get("kind") != "kit" or type(slot["valid"]) is not bool
                    or not isinstance(slot["basis"], str) or not re.fullmatch(r"[0-9a-f]{64}", slot["basis"])):
                raise ValueError("Некорректная запись раскладки комплекта в резервной копии.")
            backup_revision(slot["revision"])
            draft = validate_workspace(slot["workspace"])
            filename = self.kit_layout_filename(records[kit_id], slot["basis"], amount)
            if (not draft or draft["source"]["filename"] != filename
                    or draft["optimization"].get("nesting", {}).get("widthMm") != width):
                raise ValueError("Некорректная раскладка комплекта в резервной копии.")
            account(len(encode(draft).encode("utf-8")) + 200)
            slots[selector] = {**slot, "workspace": draft}

        models, links = {}, {}
        for model in backup_list(snapshot["models"]):
            backup_object(model, {"id", "data"})
            identifier = key(model["id"])
            if identifier in models or not isinstance(model["data"], str):
                raise ValueError("Некорректная запись модели SketchUp в резервной копии.")
            account(len(model["data"]) + 64)
            models[identifier] = backup_model(model["data"])
        for link in backup_list(snapshot["drawingModels"]):
            backup_object(link, {"drawingId", "modelId", "markup"})
            drawing_id, model_id = key(link["drawingId"]), key(link["modelId"])
            if drawing_id in links or records.get(drawing_id, {}).get("kind") != "drawing" or model_id not in models:
                raise ValueError("Некорректная связь чертежа с моделью SketchUp в резервной копии.")
            state = markup(link["markup"])
            account(len(state.encode("utf-8")) + 120)
            links[drawing_id] = (model_id, state)
        if set(models) != {model_id for model_id, _ in links.values()}:
            raise ValueError("Резервная копия содержит модель SketchUp без чертежа.")

        with self.connect(write=True) as db:
            revisions = {row["id"]: row for row in db.execute("SELECT id, kind, revision FROM registry")}
            self.check_registry_restore_references(db, records, revisions)
            slot_revisions = {(row["kit_id"], row["width_mm"], row["quantity"]): row["revision"]
                              for row in db.execute("SELECT kit_id, width_mm, quantity, revision FROM kit_layouts")}
            for statement in MODEL_SCHEMA:
                db.execute(statement)
            attached = {row["model_id"] for row in db.execute("SELECT DISTINCT model_id FROM drawing_models")}
            for identifier, blob in models.items():
                existing = db.execute("SELECT data FROM sketchup_models WHERE id=?", (identifier,)).fetchone()
                if existing and identifier not in attached and blob != existing["data"]:
                    raise WorkspaceConflict("Модель из копии совпадает по идентификатору с временной моделью в текущем реестре.")
            db.execute("DELETE FROM drawing_models")
            db.execute("DELETE FROM kit_layouts")
            for identifier in ordered:
                record = records[identifier]
                old = revisions.get(identifier)
                db.execute("INSERT INTO registry VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                           "kind=excluded.kind, parent=excluded.parent, name=excluded.name, "
                           "revision=excluded.revision, data=excluded.data",
                           (identifier, record["kind"], record["parent"], record["name"],
                            backup_revision(record["revision"], old["revision"] if old else None), contents[identifier]))
            db.execute("PRAGMA defer_foreign_keys=ON")
            db.executemany("DELETE FROM registry WHERE id=?", ((identifier,) for identifier in revisions.keys() - records.keys()))
            for selector, slot in slots.items():
                kit_id, width, amount = selector
                kit = self.layout_kit(db, kit_id)
                current_basis = self.kit_layout_basis(db, kit)
                draft, basis = slot["workspace"], slot["basis"]
                if slot["valid"]:
                    paths = self.expand_parts(db, [{"id": kit_id, "quantity": amount}])
                    check_kit_geometry(paths, draft)
                    verify_arrangement(draft, width)
                    basis = current_basis
                    draft["source"]["filename"] = self.kit_layout_filename(kit, basis, amount)
                elif basis == current_basis:
                    raise ValueError("Статус сохранённой раскладки комплекта в резервной копии некорректен.")
                db.execute("INSERT INTO kit_layouts VALUES (?, ?, ?, ?, ?, ?)",
                           (*selector, backup_revision(slot["revision"], slot_revisions.get(selector)), basis, encode(draft)))
            db.executemany("DELETE FROM sketchup_models WHERE id=?", ((identifier,) for identifier in attached - models.keys()))
            for identifier, blob in models.items():
                db.execute("INSERT INTO sketchup_models VALUES (?, 0, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (identifier, blob))
            db.executemany("INSERT INTO drawing_models VALUES (?, ?, ?)",
                           ((drawing_id, model_id, state) for drawing_id, (model_id, state) in links.items()))
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise WorkspaceConflict("Резервная копия оставляет несвязанные записи в текущем реестре.")
        return {"entries": len(records)}

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
                elif entry["kind"] == "drawing":
                    entry.update(self.model_attachment(db, entry["id"]))
                return entry
            rows = db.execute("SELECT id, kind, parent, name, revision, CASE WHEN kind = 'folder' THEN data "
                              "WHEN kind = 'part' AND json_type(data, '$.pattern') = 'object' THEN "
                              "json_object('patternId', json_extract(data, '$.pattern.id'), "
                              "'patternQuantity', json_extract(data, '$.pattern.quantity')) "
                              "ELSE '{}' END AS data FROM registry")
            return {"entries": sorted(map(row_data, rows), key=lambda r: (r["name"].casefold(), r["id"]))}

    def save_registry(self, payload, *, allow_3d=False):
        record_id = payload.get("id")
        parent = payload.get("parent")
        moving = payload.get("action") == "move"
        with self.connect(write=True) as db:
            old = None
            if record_id is not None:
                old = self.registry_record(db, record_id)
                check_revision(payload.get("revision"), old["revision"])
            if moving:
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
                data = {field: old[field] for field in ("entities", "warnings", "paths", "pattern", "patterns") if field in old}
            else:
                entities = validate_design(payload.get("entities"))
                warnings = payload.get("warnings", [])
                if (not isinstance(warnings, list) or len(warnings) > 32
                        or any(not isinstance(w, str) or len(w) > 16_384 for w in warnings)):
                    raise ValueError("Неверные предупреждения чертежа.")
                data = {"entities": entities, "warnings": warnings}
                if kind == "drawing" and "patterns" in payload:
                    patterns = object_value(payload["patterns"])
                    groups = {item["groupPath"][1] for item in entities if len(item.get("groupPath", [])) >= 2
                              and item["groupPath"][0] == "Лекала 3D"}
                    data["patterns"] = {identifier: pattern_record(record) for identifier, record in patterns.items()
                                        if identifier in groups}
                if kind == "part":
                    pattern = payload.get("pattern", old.get("pattern") if old else None)
                    if pattern:
                        data["pattern"] = pattern_record(pattern)
                        if data["pattern"]["direction"] != "free":
                            if not any(item["layer"] == "Долевая" and item.get("reference") and item["type"] == "line"
                                       for item in entities):
                                raise ValueError("Для направленной ткани нужна линия долевой.")
                    prepared = prepare_design(entities)
                    if not any(path["closed"] for path in prepared["paths"]):
                        raise ValueError("Для детали выделите замкнутую кромку и её внутренние контуры.")
                    data.update(paths=prepared["paths"], warnings=prepared["warnings"][:32])
            if kind == "part":
                old_legend = old.get("partLegend") if old else None
                legend = old_legend if moving else part_legend(payload.get("partLegend", old_legend))
                if legend is not None:
                    data["partLegend"] = legend
                if not moving:
                    if "entities" not in payload:
                        prepared = prepare_design(data["entities"])
                    data["entities"], data["paths"] = center_part_geometry(data["entities"], prepared)
            revision = 1 if old is None else old["revision"] + 1
            if old is None:
                db.execute("INSERT INTO registry VALUES (?, ?, ?, ?, ?, ?)",
                           (record_id, kind, parent, title, revision, encode(data)))
            else:
                db.execute("UPDATE registry SET name=?, parent=?, revision=?, data=? WHERE id=?",
                           (title, parent, revision, encode(data), record_id))
            if (kind == "part" and old and old["paths"] != data["paths"]
                    and same_part_geometry(old["paths"], data["paths"])):
                self.preserve_centered_kit_layouts(db, old)
            if "model3d" in payload:
                if not allow_3d or kind != "drawing":
                    raise ValueError("Модель можно сохранить в локальном чертеже.")
                attachment = object_value(payload["model3d"])
                model_id = key(attachment.get("id"))
                state = markup(attachment.get("state"))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='sketchup_models'").fetchone() is None:
                    raise RecordNotFound("Сначала импортируйте модель SketchUp.")
                row = db.execute("SELECT 1 FROM sketchup_models WHERE id=? AND "
                                 "(expires >= unixepoch() OR EXISTS "
                                 "(SELECT 1 FROM drawing_models WHERE model_id=?))", (model_id, model_id)).fetchone()
                if row is None:
                    raise RecordNotFound("Модель не найдена. Импортируйте файл SketchUp ещё раз.")
                db.execute("INSERT INTO drawing_models VALUES (?, ?, ?) ON CONFLICT(drawing_id) DO UPDATE "
                           "SET model_id=excluded.model_id, markup=excluded.markup", (record_id, model_id, state))
            return {**self.registry_record(db, record_id), **self.model_attachment(db, record_id)}

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

    def expand_parts(self, db, members, multiplier=1, *, part_records=None):
        paths, part_number, point_count = [], 0, 0
        products = {None: None}
        numbers_by_part = defaultdict(list)

        def product_name(parent):
            ancestors = []
            while parent not in products:
                folder = self.registry_record(db, parent)
                if folder.get("model") is True:
                    products[parent] = folder["name"]
                    break
                ancestors.append(parent)
                parent = folder["parent"]
            title = products[parent]
            products.update(dict.fromkeys(ancestors, title))
            return title

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
                directed = part.get("pattern", {}).get("direction", "free") != "free"
                if directed:
                    grain = next(item for item in part["entities"] if item["layer"] == "Долевая"
                                 and item.get("reference") and item["type"] == "line")
                    x1, y1 = grain["start"]
                    x2, y2 = grain["end"]
                    length = math.hypot(x2 - x1, y2 - y1)
                    if length <= POINT_EPSILON_MM:
                        raise ValueError("Линия долевой слишком короткая.")
                    cosine, sine = (x2 - x1) / length, (y2 - y1) / length
                    contours = [{**path, "points": [[x * cosine + y * sine, y * cosine - x * sine]
                                                    for x, y in path["points"]]} for path in contours]
                min_x = min(x for path in contours for x, _ in path["points"])
                min_y = min(y for path in contours for _, y in path["points"])
                labels = {"partName": part["name"]}
                product = product_name(part["parent"])
                if product:
                    labels["productName"] = product
                normalized = [{**path, "points": [[x - min_x, y - min_y] for x, y in path["points"]],
                               **labels, **({"partRotation": "none"} if directed else {})} for path in contours]
                for _ in range(amount):
                    part_number += 1
                    numbers_by_part[part["id"]].append(part_number)
                    if part_records is not None:
                        part_records[part_number] = part
                    paths.extend({**path, "partNumber": part_number} for path in normalized)
        display_numbers = {number: f"{numbers[-1]}/{copy}" if len(numbers) > 1 else str(number)
                           for numbers in numbers_by_part.values() for copy, number in enumerate(numbers, 1)}
        for path in paths:
            path["partDisplayNumber"] = display_numbers[path["partNumber"]]
        return paths

    def collect(self, values):
        with self.connect() as db:
            members = self.members(db, values, ("part", "kit"))
            return {"paths": self.expand_parts(db, members)}

    def kit_layout_basis(self, db, kit, previous_part=None):
        digest = hashlib.sha256(encode(kit["members"]).encode("utf-8"))
        for member in kit["members"]:
            part = previous_part if previous_part and previous_part["id"] == member["id"] else self.registry_record(db, member["id"])
            digest.update(encode(part["paths"]).encode("utf-8"))
        return digest.hexdigest()

    def preserve_centered_kit_layouts(self, db, previous_part):
        kits = db.execute("SELECT DISTINCT registry.* FROM registry JOIN kit_layouts ON kit_layouts.kit_id=registry.id, "
                          "json_each(registry.data, '$.members') AS member "
                          "WHERE registry.kind='kit' AND json_extract(member.value, '$.id')=?", (previous_part["id"],)).fetchall()
        for row in kits:
            kit = row_data(row)
            old_basis = self.kit_layout_basis(db, kit, previous_part)
            basis = self.kit_layout_basis(db, kit)
            slots = db.execute("SELECT width_mm, quantity, data FROM kit_layouts WHERE kit_id=? AND basis=?",
                               (kit["id"], old_basis)).fetchall()
            for slot in slots:
                amount = slot["quantity"]
                try:
                    draft = validate_workspace(json.loads(slot["data"]))
                    if not draft:
                        continue
                    paths = self.expand_parts(db, [{"id": kit["id"], "quantity": amount}])
                    check_kit_geometry(paths, draft)
                except (ValueError, PlotterError):
                    continue
                draft["source"]["filename"] = self.kit_layout_filename(kit, basis, amount)
                db.execute("UPDATE kit_layouts SET basis=?, data=? WHERE kit_id=? AND width_mm=? AND quantity=?",
                           (basis, encode(draft), kit["id"], slot["width_mm"], amount))

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
                if missing_part_labels(draft):
                    paths = self.expand_parts(db, [{"id": kit["id"], "quantity": amount}])
                    draft = restore_part_labels(draft, paths)
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
            draft = restore_part_labels(draft, paths)
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
        amount = quantity(member["quantity"] * multiplier)
        slots = db.execute("SELECT width_mm, basis, data FROM kit_layouts WHERE kit_id=? AND quantity=? "
                           "AND (? IS NULL OR width_mm=?) ORDER BY rowid DESC", (kit["id"], amount, width, width))
        basis = None
        for slot in slots:
            if basis is None:
                basis = self.kit_layout_basis(db, kit)
            if slot["basis"] != basis:
                continue
            try:
                draft = validate_workspace(json.loads(slot["data"]))
                if not draft or draft["optimization"].get("nesting", {}).get("widthMm") != slot["width_mm"]:
                    continue
                check_kit_geometry(paths, draft)
                verify_layout(draft, slot["width_mm"])
            except ValueError:
                continue
            draft = restore_part_labels(draft, paths)
            return {**draft, "source": {**draft["source"], "filename": filename}}
        return None

    @staticmethod
    def order_details(db, order):
        details = {"placedOn": None, "dueOn": None, "notes": "", "modelId": None, "modelName": None, "quantity": 1,
                   "designation": "", **order}
        if not details["modelName"] and isinstance(details["modelId"], str):
            model = db.execute("SELECT name FROM registry WHERE id=? AND kind='folder' AND json_extract(data, '$.model')=1",
                               (details["modelId"],)).fetchone()
            if model:
                details["modelName"] = model["name"]
        return details

    @staticmethod
    def order_header(db, order_id):
        row = db.execute("SELECT * FROM orders WHERE id = ?", (key(order_id),)).fetchone()
        order = row_data(row) if row is not None else None
        if order is None or order.get("deleted"):
            raise RecordNotFound("Наряд не найден.")
        return ProductionStore.order_details(db, order)

    @staticmethod
    def cut_record(db, cut_id):
        row = db.execute("SELECT * FROM cuts WHERE id = ?", (key(cut_id),)).fetchone()
        if row is None:
            raise RecordNotFound("Раскладка не найдена.")
        ProductionStore.order_header(db, row["order_id"])
        return {"released": True, **row_data(row)}

    @staticmethod
    def cuts(db, order_id):
        return [cut_with_statistics(row) for row in db.execute(
            CUTS_WITH_STATISTICS + " WHERE cuts.order_id=? ORDER BY cuts.position", (order_id,))]

    def order_with_cuts(self, db, header):
        cuts = self.cuts(db, header["id"])
        return {**header, "cuts": cuts, "stats": summarize_cuts(cuts)}

    def orders(self, order_id=None, *, published_only=False):
        with self.connect() as db:
            if order_id is not None:
                header = self.order_header(db, order_id)
                if published_only and not header["published"]:
                    raise RecordNotFound("Наряд ещё не отправлен оператору.")
                return self.order_with_cuts(db, header)
            entries = (row_data(row) for row in db.execute(
                "SELECT * FROM orders WHERE published >= ? ORDER BY rowid DESC", (int(published_only),)))
            orders = {order["id"]: {**self.order_details(db, order),
                                   "materials": [], "total": 0, "done": 0, "active": 0}
                      for order in entries if not order.get("deleted")}
            for row in db.execute(CUTS_WITH_STATISTICS + " JOIN orders ON orders.id=cuts.order_id "
                                  "WHERE orders.published >= ? ORDER BY cuts.order_id, cuts.position", (int(published_only),)):
                order = orders.get(row["order_id"])
                if order is None:
                    continue
                cut = cut_with_statistics(row, released=bool(order["published"]))
                order["materials"].append({field: cut[field] for field in
                                           ("id", "name", "widthMm", "status", "prepared", "released", "stats")})
                order["total"] += 1
                order["done"] += cut["status"] == "done"
                order["active"] += cut["status"] == "active"
            for order in orders.values():
                order["stats"] = summarize_cuts(order["materials"])
            return {"orders": list(orders.values())}

    def save_order(self, payload):
        with self.connect(write=True) as db:
            order_id = payload.get("id")
            old = self.order_header(db, order_id) if order_id is not None else None
            if old:
                check_revision(payload.get("revision"), old["revision"])
                if old["published"]:
                    raise WorkspaceConflict("Наряд уже отправлен.")
            previous = old or {}
            model_id = payload.get("modelId", previous.get("modelId"))
            model_name = previous.get("modelName") if "modelId" not in payload else None
            if "modelId" in payload and model_id is not None:
                model = self.registry_record(db, model_id)
                if model["kind"] != "folder" or not model.get("model"):
                    raise ValueError("Выберите папку, отмеченную как изделие.")
                model_name = model["name"]
            amount = quantity(payload.get("quantity", previous.get("quantity", 1)))
            if old and (old["quantity"] != amount or old["modelId"] != model_id):
                cuts = self.cuts(db, order_id)
                if cuts and old["modelId"] != model_id:
                    raise ValueError("Чтобы изменить изделие, сначала удалите пункты раскроя этого наряда.")
                for cut in cuts:
                    paths = self.expand_parts(db, cut["members"], amount)
                    cut_id = cut["id"]
                    workspace = new_workspace(paths, f"cut-{cut_id}.dxf", cut["widthMm"])
                    record = self.put_workspace(db, cut_id, "design", workspace, self.find_workspace(db, cut_id, "design"))
                    db.execute("UPDATE cuts SET revision=revision+1, data=? WHERE id=?",
                               (encode(cut_data(cut, prepared=False, released=False, partCount=paths[-1]["partNumber"])), cut_id))
                    self.refresh_cut_statistics(db, {**cut, "prepared": False}, "design", record)
                self.clear_operator_workspaces(db, order_id)
            data = {**order_metadata(payload, old), "modelId": model_id, "modelName": model_name, "quantity": amount}
            if old is None:
                order_id = uid()
                db.execute("INSERT INTO orders VALUES (?, 1, 0, ?)", (order_id, encode(data)))
            else:
                db.execute("UPDATE orders SET revision=revision+1, data=? WHERE id=?", (encode(data), order_id))
            return self.order_with_cuts(db, self.order_header(db, order_id))

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
            return self.order_with_cuts(db, self.order_header(db, order_id))

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
                before = old or {}
                members = self.members(db, payload.get("members", before.get("members")), ("part", "kit"))
                requested = payload.get("widthMm")
                width = before.get("widthMm") if requested is None else width_mm(requested)
                operator_width = before.get("operatorAssignedWidth")
                if operator_width and requested is None and members != before["members"]:
                    width = None
                data = {"name": name(payload.get("name", before.get("name")), "название ткани"),
                        "widthMm": width, "members": members}
                previous = self.find_workspace(db, cut_id, "design") if old else None
                same_layout = (previous and not (operator_width and requested is not None)
                               and all(before[field] == data[field] for field in ("widthMm", "members")))
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
                        if workspace is not None:
                            data.update(widthMm=workspace["optimization"]["nesting"]["widthMm"], prepared=True)
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
                    record = self.put_workspace(db, cut_id, "design", workspace, previous)
                    self.refresh_cut_statistics(db, {"id": cut_id, **data}, "design", record)
            db.execute("UPDATE orders SET revision=revision+1 WHERE id=?", (order["id"],))
            return {**self.order_with_cuts(db, self.order_header(db, order["id"])), "cutId": cut_id}

    @staticmethod
    def find_workspace(db, cut_id, mode):
        row = db.execute("SELECT revision, cut_revision, data FROM workspaces WHERE cut_id=? AND mode=?",
                         (cut_id, mode)).fetchone()
        if row is None:
            return None
        return {"revision": row["revision"], "cutRevision": row["cut_revision"],
                "workspace": json.loads(row["data"])}

    @staticmethod
    def statistics_key(db, cut_id):
        row = db.execute("SELECT workspace_mode, cut_revision, width_mm, prepared FROM cut_statistics WHERE cut_id=?",
                         (cut_id,)).fetchone()
        return tuple(row) if row is not None else None

    @staticmethod
    def current_statistics_key(db, cut):
        row = db.execute("SELECT mode, cut_revision FROM workspaces WHERE cut_id=? AND mode IN ('design','operator') "
                         "ORDER BY mode='operator' DESC LIMIT 1", (cut["id"],)).fetchone()
        return (*row, cut["widthMm"], row["mode"] != "design" or bool(cut.get("prepared"))) if row is not None else None

    @staticmethod
    def put_cut_statistics(db, cut_id, key, metrics):
        db.execute("INSERT INTO cut_statistics (cut_id,workspace_mode,cut_revision,width_mm,prepared,metrics) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(cut_id) DO UPDATE SET workspace_mode=excluded.workspace_mode, "
                   "cut_revision=excluded.cut_revision, width_mm=excluded.width_mm, prepared=excluded.prepared, "
                   "metrics=excluded.metrics",
                   (cut_id, *key, encode(metrics) if metrics is not None else None))

    def calculate_cut_statistics(self, cut, mode, draft, source, verified=None):
        width = cut["widthMm"]
        if (width is None or (mode == "design" and not cut.get("prepared"))
                or draft["optimization"].get("nesting", {}).get("widthMm") != width):
            return None
        if not isinstance(verified, dict):
            try:
                check_composition(draft, cut)
                verified = self.layout_metrics(draft, width)
            except (ValueError, PlotterError):
                return None
        return plan_snapshot(verified, source)

    def refresh_cut_statistics(self, db, cut, mode, record, verified=None):
        key = (mode, record["cutRevision"], cut["widthMm"], mode != "design" or bool(cut.get("prepared")))
        previous = self.statistics_key(db, cut["id"])
        if previous == key:
            return
        if self.layout_metrics is None:
            if previous is not None:
                self.put_cut_statistics(db, cut["id"], (None, None, None, None), None)
            return
        metrics = self.calculate_cut_statistics(cut, mode, record["workspace"], mode, verified)
        self.put_cut_statistics(db, cut["id"], key, metrics)

    def backfill_metrics(self):
        if self.layout_metrics is None or not self.path.is_file():
            return
        with self.connect() as db:
            cut_ids = [row["id"] for row in db.execute("SELECT cuts.id FROM cuts JOIN orders ON orders.id=cuts.order_id "
                                                       "WHERE COALESCE(json_extract(orders.data,'$.deleted'),0)=0")]
        for cut_id in cut_ids:
            try:
                with self.connect() as db:
                    cut = self.cut_record(db, cut_id)
                    key = self.current_statistics_key(db, cut)
                    if key is None or self.statistics_key(db, cut_id) == key:
                        continue
                    draft = self.find_workspace(db, cut_id, key[0])["workspace"]
                metrics = self.calculate_cut_statistics(cut, key[0], draft, "saved")
                with self.connect(write=True) as db:
                    if (self.current_statistics_key(db, self.cut_record(db, cut_id)) == key
                            and self.statistics_key(db, cut_id) != key):
                        self.put_cut_statistics(db, cut_id, key, metrics)
            except RecordNotFound:
                continue

    @staticmethod
    def clear_operator_workspaces(db, order_id):
        db.execute("DELETE FROM workspaces WHERE mode IN ('operator','demo') AND cut_id IN "
                   "(SELECT id FROM cuts WHERE order_id=?)", (order_id,))

    def put_workspace(self, db, cut_id, mode, workspace, previous):
        content = encode(workspace)
        revision = previous["revision"] + 1 if previous else 1
        # View settings and labels do not change the cutting job.
        def geometry(draft):
            return {**{field: value for field, value in draft.items() if field not in ("view", "paths")},
                    "paths": [{field: value for field, value in path.items() if field not in PART_LABEL_FIELDS}
                              for path in draft["paths"]]}
        cut_revision = (previous["cutRevision"] + (geometry(previous["workspace"]) != geometry(workspace))
                        if previous else 1)
        db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?) ON CONFLICT(cut_id, mode) DO UPDATE "
                   "SET revision=excluded.revision, cut_revision=excluded.cut_revision, data=excluded.data",
                   (cut_id, mode, revision, cut_revision, content))
        return self.find_workspace(db, cut_id, mode)

    def restore_workspace_part_labels(self, db, cut, order, record):
        draft = record["workspace"]
        if not missing_part_labels(draft):
            return record
        try:
            source = self.expand_parts(db, cut["members"], order["quantity"])
        except (ValueError, RecordNotFound):
            return record
        return {**record, "workspace": restore_part_labels(draft, source, check_geometry=True)}

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
            record = base
        else:
            record = self.find_workspace(db, cut["id"], mode) or {"revision": 0, "cutRevision": base["cutRevision"],
                                                                  "workspace": base["workspace"]}
        record = self.restore_workspace_part_labels(db, cut, order, record)
        return {**record, "order": {field: order[field] for field in ("id", "number", "designation", "modelName")}}

    def workspace(self, cut_id, mode):
        with self.connect() as db:
            cut = self.cut_record(db, cut_id)
            return {**self.matching_workspace(db, cut, mode), "status": cut["status"],
                    "name": cut["name"], "widthMm": cut["widthMm"]}

    def save_workspace(self, payload, mode, verify_layout):
        with self.connect(write=True) as db:
            cut = self.cut_record(db, payload.get("cutId"))
            current = self.matching_workspace(db, cut, mode)
            check_revision(payload.get("revision"), current["revision"], mismatch_code=WORKSPACE_REVISION_CONFLICT)
            if mode == "preview" or (mode == "operator" and cut["status"] == "done"):
                raise WorkspaceConflict("Этот раскрой открыт для просмотра.")
            draft = validate_workspace(payload.get("workspace"))
            if not draft or draft["source"]["filename"] != current["workspace"]["source"]["filename"]:
                raise ValueError("Сохраните раскладку для этого пункта наряда.")
            if missing_part_labels(draft):
                draft = restore_part_labels(draft, current["workspace"]["paths"])
            width = cut["widthMm"]
            layout_width = draft["optimization"].get("nesting", {}).get("widthMm")
            if mode == "design" and width is not None and layout_width != width:
                raise ValueError("Ширина должна совпадать с указанной в пункте наряда.")
            if mode == "operator" and cut["status"] == "pending":
                raise WorkspaceConflict("Сначала откройте раскрой из наряда.")
            prepared = cut.get("prepared")
            verified = None
            if mode == "design" or (mode == "operator" and width is None):
                prepared = False
                if layout_width is not None:
                    try:
                        check_composition(draft, cut)
                        verified = verify_layout(draft, layout_width)
                        prepared = True
                    except (ValueError, PlotterError):
                        pass
                if prepared and width is None:
                    width = layout_width
                if width != cut["widthMm"] or (mode == "design" and cut["prepared"] != prepared):
                    data = cut_data(cut, widthMm=width, prepared=prepared)
                    if mode == "operator":
                        data["operatorAssignedWidth"] = True
                    db.execute("UPDATE cuts SET data=? WHERE id=?", (encode(data), cut["id"]))
            if mode == "design":
                db.execute("DELETE FROM workspaces WHERE cut_id=? AND mode IN ('operator','demo')", (cut["id"],))
            previous = self.find_workspace(db, cut["id"], mode)
            if not previous:
                previous = current
            record = self.put_workspace(db, cut["id"], mode, draft, previous)
            if mode != "demo":
                self.refresh_cut_statistics(db, {**cut, "widthMm": width, "prepared": prepared}, mode, record, verified)
            return {**record, "widthMm": width}

    def verify_cut(self, db, cut, verify_layout):
        record = self.find_workspace(db, cut["id"], "design")
        draft = record["workspace"]
        width = cut["widthMm"]
        verified = None
        try:
            check_composition(draft, cut)
            if width is not None:
                verified = verify_layout(draft, width)
        except (ValueError, PlotterError) as error:
            raise ValueError(f'{cut["name"]}: {error}') from error
        data = cut_data(cut, prepared=width is not None, released=True)
        db.execute("UPDATE cuts SET data=? WHERE id=?", (encode(data), cut["id"]))
        self.refresh_cut_statistics(db, {**cut, **data}, "design", record, verified)

    def publish_order(self, payload, verify_layout):
        with self.connect(write=True) as db:
            order = self.order_header(db, payload.get("id"))
            if order["published"]:
                return self.order_with_cuts(db, order)
            check_revision(payload.get("revision"), order["revision"])
            cuts = self.cuts(db, order["id"])
            if not cuts:
                raise ValueError("Добавьте хотя бы один пункт раскроя.")
            for cut in cuts:
                self.verify_cut(db, cut, verify_layout)
            db.execute("UPDATE orders SET published=1, revision=revision+1 WHERE id=?", (order["id"],))
            return self.order_with_cuts(db, self.order_header(db, order["id"]))

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
            return self.order_with_cuts(db, self.order_header(db, order["id"]))

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
            for cut in cuts:
                if cut.get("operatorAssignedWidth"):
                    data = cut_data(cut, widthMm=None, prepared=False)
                    data.pop("operatorAssignedWidth")
                    db.execute("UPDATE cuts SET data=? WHERE id=?", (encode(data), cut["id"]))
            self.clear_operator_workspaces(db, order_id)
            for cut in self.cuts(db, order_id):
                record = self.find_workspace(db, cut["id"], "design")
                self.refresh_cut_statistics(db, cut, "design", record)
            db.execute("UPDATE orders SET published=0, revision=revision+1 WHERE id=?", (order_id,))
            return self.order_with_cuts(db, self.order_header(db, order_id))

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
                    if status == "active":
                        db.execute("INSERT INTO cut_statistics (cut_id,started_at) VALUES (?,?) "
                                   "ON CONFLICT(cut_id) DO UPDATE SET started_at=excluded.started_at, finished_at=NULL",
                                   (cut_id, utc_now()))
                    else:
                        db.execute("INSERT INTO cut_statistics (cut_id,finished_at) VALUES (?,?) "
                                   "ON CONFLICT(cut_id) DO UPDATE SET finished_at=excluded.finished_at", (cut_id, utc_now()))
            elif action != "start" or mode != "demo":
                raise WorkspaceConflict("В демо-режиме ход наряда не сохраняется.")
            return {**current, "status": status, "name": cut["name"], "widthMm": cut["widthMm"],
                    "opened": cut["status"] == "pending" and status == "active"}

    def release_cut(self, cut_id, revision):
        with self.connect(write=True) as db:
            cut = self.cut_record(db, cut_id)
            current = self.matching_workspace(db, cut, "operator")
            check_revision(revision, current["revision"])
            if cut["status"] != "active":
                raise WorkspaceConflict("Этот раскрой сейчас не открыт на плоттере.")
            current = self.put_workspace(db, cut_id, "operator", current["workspace"], current)
            self.refresh_cut_statistics(db, cut, "operator", current)
            db.execute("UPDATE cuts SET status='pending' WHERE id=?", (cut_id,))
            db.execute("UPDATE cut_statistics SET started_at=NULL, finished_at=NULL WHERE cut_id=?", (cut_id,))
            return {**current, "status": "pending", "name": cut["name"], "widthMm": cut["widthMm"]}

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
