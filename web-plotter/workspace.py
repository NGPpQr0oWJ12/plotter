import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from cad import MAX_ABS_COORDINATE_MM, MAX_ENTITIES, MAX_POINTS


VERSION = 1
MAX_REVISION = (1 << 53) - 1  # Exactly representable by the browser.
# Includes 50,000 contour layer names and 250,000 points at the CAD limits.
MAX_WORKSPACE_BYTES = 96 * 1024 * 1024
PART_LABEL_FIELDS = ("partName", "productName")
WORKSPACE_REVISION_CONFLICT = "workspace_revision_conflict"
WORKSPACE_REVISION_MESSAGE = "Рабочая область изменилась в другой вкладке. Обновите страницу."


class WorkspaceConflict(ValueError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def validate_workspace(value):
    return _workspace(value)


def _revision(value):
    return type(value) is int and 0 <= value <= MAX_REVISION


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _text(value, limit):
    return (
        isinstance(value, str)
        and len(value) <= limit
        and all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)
    )


def part_path_labels(path):
    labels = {}
    for field in PART_LABEL_FIELDS:
        if field not in path:
            continue
        label = path[field]
        if (path.get("partNumber") is None or not _text(label, 255) or not label.strip()
                or any(ord(character) < 32 for character in label)):
            raise ValueError("Неверное название детали или изделия в раскладке.")
        labels[field] = label
    return labels


def _workspace(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Неверный формат рабочей области.")

    source = value.get("source")
    if not isinstance(source, dict):
        raise ValueError("Нет сведений о файле рабочей области.")
    filename = source.get("filename")
    warnings = source.get("warnings")
    if (not _text(filename, 255) or not filename.lower().endswith((".dxf", ".dwg"))
            or any(character in "/\\" or ord(character) < 32 for character in filename)):
        raise ValueError("Неверное имя файла рабочей области.")
    if (not isinstance(warnings, list) or len(warnings) > 32
            or any(not _text(warning, 16_384) for warning in warnings)):
        raise ValueError("Неверный список предупреждений рабочей области.")

    paths = drawing_paths(value.get("paths"))
    excluded = excluded_paths(value.get("excluded"), len(paths))

    view = value.get("view")
    if not isinstance(view, dict):
        raise ValueError("Нет настроек вида рабочей области.")
    zoom = view.get("zoom")
    focus_x, focus_y, grid = view.get("focusX"), view.get("focusY"), view.get("grid")
    if (not _number(zoom) or not 0 < zoom <= 25 or not _number(focus_x)
            or not _number(focus_y) or type(grid) is not bool):
        raise ValueError("Неверные настройки вида рабочей области.")
    view_settings = {"zoom": zoom, "focusX": focus_x, "focusY": focus_y, "grid": grid}
    if "cameraMode" in view:
        if view["cameraMode"] not in ("work", "overview", "manual"):
            raise ValueError("Неверный режим вида рабочей области.")
        view_settings["cameraMode"] = view["cameraMode"]
    if "section" in view:
        if type(view["section"]) is not int or view["section"] < 1:
            raise ValueError("Неверный проход для просмотра.")
        view_settings["section"] = view["section"]

    optimization = value.get("optimization")
    if (not isinstance(optimization, dict) or type(optimization.get("route")) is not bool
            or type(optimization.get("join")) is not bool):
        raise ValueError("Неверные настройки оптимизации рабочей области.")

    settings = {"route": optimization["route"], "join": optimization["join"]}
    if "routeApplied" in optimization:
        route_applied = optimization["routeApplied"]
        if type(route_applied) is not bool:
            raise ValueError("Неверная настройка маршрута рабочей области.")
        settings["routeApplied"] = route_applied
    if "nesting" in optimization:
        nesting = optimization["nesting"]
        if not isinstance(nesting, dict):
            raise ValueError("Неверные настройки автораскладки.")
        width, gap, margin = nesting.get("widthMm"), nesting.get("gapMm"), nesting.get("marginMm")
        rotation = nesting.get("rotation")
        if ((width is not None and (not _number(width) or not 1 <= width <= 1_000_000))
                or not _number(gap) or not 0 <= gap <= 100
                or not _number(margin) or not 0 <= margin <= 100
                or rotation not in ("none", "any", "half", "quarter")):
            raise ValueError("Неверные настройки автораскладки.")
        settings["nesting"] = {"widthMm": width, "gapMm": gap, "marginMm": margin, "rotation": rotation}
        if "lengthMm" in nesting:
            length = nesting["lengthMm"]
            if length is not None and (not _number(length) or not 1 <= length <= MAX_ABS_COORDINATE_MM):
                raise ValueError("Неверная длина полотна для автораскладки.")
            settings["nesting"]["lengthMm"] = length
        if "widthExplicit" in nesting:
            width_explicit = nesting.get("widthExplicit")
            if type(width_explicit) is not bool:
                raise ValueError("Неверный признак выбранной ширины полотна.")
            settings["nesting"]["widthExplicit"] = width_explicit

    return {
        "source": {"filename": filename, "warnings": warnings},
        "paths": paths,
        "excluded": excluded,
        "view": view_settings,
        "optimization": settings,
    }


def drawing_paths(paths):
    if not isinstance(paths, list) or len(paths) > MAX_ENTITIES:
        raise ValueError("Неверное число контуров рабочей области.")
    clean_paths = []
    point_count = 0
    part_rotations = {}
    part_labels = {}
    for path in paths:
        if not isinstance(path, dict):
            raise ValueError("Неверный контур рабочей области.")
        points, layer, closed = path.get("points"), path.get("layer"), path.get("closed")
        if type(closed) is not bool or not _text(layer, 255):
            raise ValueError("Неверные свойства контура рабочей области.")
        if not isinstance(points, list) or len(points) < (3 if closed else 2):
            raise ValueError("Не хватает точек контура рабочей области.")
        point_count += len(points)
        if point_count > MAX_POINTS:
            raise ValueError("В рабочей области слишком много точек.")
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or not _number(point[0]) or not _number(point[1])):
                raise ValueError("Неверные координаты контура рабочей области.")
        clean_path = {"points": points, "layer": layer, "closed": closed}
        if "notch" in path:
            if type(path["notch"]) is not bool:
                raise ValueError("Неверный признак насечки рабочей области.")
            clean_path["notch"] = path["notch"]
        part_number = path.get("partNumber")
        if "partNumber" in path:
            if not _revision(part_number) or part_number == 0:
                raise ValueError("Неверный номер детали рабочей области.")
            clean_path["partNumber"] = part_number
        labels = part_path_labels(path)
        for field, label in labels.items():
            if part_labels.setdefault((part_number, field), label) != label:
                raise ValueError("У контуров одной детали разные названия.")
        clean_path.update(labels)
        if "partAngle" in path:
            angle = path["partAngle"]
            if type(angle) not in (int, float) or not 0 <= angle < 360:
                raise ValueError("Неверный угол детали рабочей области.")
            clean_path["partAngle"] = angle
        if "partRotation" in path:
            rotation = path["partRotation"]
            if part_number is None or rotation not in ("none", "any", "quarter"):
                raise ValueError("Неверная настройка поворота детали рабочей области.")
            mode = "any" if rotation == "quarter" else rotation
            if part_rotations.setdefault(part_number, mode) != mode:
                raise ValueError("У контуров одной детали разные настройки поворота.")
            clean_path["partRotation"] = rotation
        clean_paths.append(clean_path)

    return clean_paths


def excluded_paths(excluded, path_count):
    if (not isinstance(excluded, list) or len(excluded) > path_count
            or any(type(index) is not int or not 0 <= index < path_count for index in excluded)
            or len(set(excluded)) != len(excluded)):
        raise ValueError("Неверный список исключённых контуров.")
    return excluded


def _load(path):
    try:
        with path.open("rb") as file:
            contents = file.read(MAX_WORKSPACE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(contents) > MAX_WORKSPACE_BYTES:
        raise ValueError("Файл рабочей области слишком велик.")
    data = json.loads(contents)
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != VERSION or not _revision(data.get("revision"))
            or data["revision"] == 0 or "workspace" not in data):
        raise ValueError("Неизвестный формат файла рабочей области.")
    return data["revision"], _workspace(data["workspace"]), contents


def replace_file(path, contents):
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class WorkspaceStore:
    def __init__(self, path, *, shared=False):
        self.path = Path(path)
        self.backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.lock = Lock()
        self.shared = shared

    @contextmanager
    def _locked(self):
        with self.lock:
            if not self.shared:
                yield
                return
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield

    def _current(self):
        primary_error = None
        try:
            primary = _load(self.path)
            if primary is not None:
                revision, workspace, contents = primary
                return revision, workspace, contents, False
        except (ValueError, RecursionError) as error:
            primary_error = error
        try:
            backup = _load(self.backup)
            if backup is not None:
                revision, workspace, _ = backup
                return revision, workspace, None, True
        except (ValueError, RecursionError) as error:
            raise ValueError("Не удалось прочитать рабочую область и её резервную копию.") from error
        if primary_error is not None:
            raise ValueError("Не удалось прочитать рабочую область. Резервная копия отсутствует.") from primary_error
        return 0, None, None, False

    def read(self):
        with self._locked():
            revision, workspace, _, recovered = self._current()
        return {"revision": revision, "workspace": workspace, "recovered": recovered}

    def write(self, expected_revision, workspace):
        if not _revision(expected_revision):
            raise ValueError("Неверная редакция рабочей области.")
        workspace = _workspace(workspace)
        with self._locked():
            revision, _, previous, _ = self._current()
            if revision != expected_revision:
                raise WorkspaceConflict(WORKSPACE_REVISION_MESSAGE, code=WORKSPACE_REVISION_CONFLICT)
            if revision == MAX_REVISION:
                raise ValueError("Достигнут предел редакций рабочей области.")
            revision += 1
            contents = json.dumps(
                {"version": VERSION, "revision": revision, "workspace": workspace},
                ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            if len(contents) > MAX_WORKSPACE_BYTES:
                raise ValueError("Файл рабочей области слишком велик.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if previous is not None:
                replace_file(self.backup, previous)
            replace_file(self.path, contents)
        return {"revision": revision, "workspace": workspace, "recovered": False}
