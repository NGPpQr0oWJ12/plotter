"""Local import boundary and drawing state for SketchUp upholstery."""

import json
import math
import re
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

MAX_SKP_BYTES = 96 * 1024 * 1024
MAX_MODEL_BYTES = 128 * 1024 * 1024
MAX_MARKUP_BYTES = 2 * 1024 * 1024
HEX_COLOR = re.compile(r"#[a-fA-F0-9]{6}")


def import_model(data, filename):
    if not data or len(data) > MAX_SKP_BYTES:
        raise ValueError("Выберите файл SketchUp размером до 96 МБ.")
    if (not filename.lower().endswith(".skp") or len(filename) > 255
            or any(c in "/\\" or ord(c) < 32 for c in filename)):
        raise ValueError("Выберите файл SketchUp (.skp).")
    worker = Path(__file__).with_name("skp_reader.py")
    with TemporaryDirectory(prefix="plotter-skp-") as directory:
        path = Path(directory) / "model.skp"
        path.write_bytes(data)
        with path.with_suffix(".json").open("w+b") as output:
            try:
                run = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(worker), str(path)],
                                     stdout=output, stderr=subprocess.PIPE, timeout=90,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as error:
                raise ValueError("Чтение модели заняло слишком много времени.") from error
            if run.returncode:
                detail = run.stderr.decode("utf-8", errors="replace").strip()
                if detail and len(detail) <= 320 and "\n" not in detail and "\\" not in detail:
                    raise ValueError(detail)
                raise ValueError("Не удалось прочитать файл SketchUp. Проверьте файл и установку SketchUp.")
            if output.tell() > MAX_MODEL_BYTES:
                raise ValueError("Сетка модели слишком велика. Упростите модель в SketchUp.")
            output.seek(0)
            try:
                model = json.load(output)
            except (ValueError, UnicodeError) as error:
                raise ValueError("Не удалось прочитать геометрию из SketchUp.") from error
    if not isinstance(model, dict) or not any(node["triangles"] for node in model.get("nodes", [])):
        raise ValueError("В модели SketchUp нет поверхностей для развёртки.")
    model["name"] = filename
    model["nodes"][0]["name"] = filename
    return model


def _layer_text(value):
    return isinstance(value, str) and bool(value.strip()) and all(ord(char) >= 32 for char in value)


def _validate_fabric_layers(layers):
    if not isinstance(layers, list):
        raise ValueError("Некорректный список слоёв ткани.")
    identifiers = set()
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("Некорректный слой ткани.")
        identifier = layer.get("id")
        if not _layer_text(identifier) or len(identifier) > 128 or identifier in identifiers:
            raise ValueError("Идентификаторы слоёв ткани должны быть уникальными и непустыми.")
        identifiers.add(identifier)
        name, color = layer.get("name"), layer.get("color")
        if not _layer_text(name) or len(name) > 120:
            raise ValueError("Укажите название слоя ткани длиной до 120 символов.")
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise ValueError("Укажите цвет слоя ткани в формате #RRGGBB.")
        surfaces = layer.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces:
            raise ValueError("Выберите поверхности для слоя ткани.")
        for surface in surfaces:
            if not isinstance(surface, dict) or not _layer_text(surface.get("nodeId")):
                raise ValueError("Некорректный компонент поверхности слоя ткани.")
            faces = surface.get("faces")
            if not isinstance(faces, list) or not faces or not all(_layer_text(face) for face in faces):
                raise ValueError("Некорректный список граней поверхности слоя ткани.")


def _validate_surface_fabrics(assignments):
    if not isinstance(assignments, list):
        raise ValueError("Некорректный список назначений ткани.")
    assigned = set()
    for item in assignments:
        if not isinstance(item, dict) or set(item) != {"nodeId", "faceId", "name", "color"}:
            raise ValueError("Для назначения ткани укажите объект, грань, название и цвет.")
        node_id, face_id = item["nodeId"], item["faceId"]
        if not _layer_text(node_id) or not _layer_text(face_id):
            raise ValueError("Укажите объект и грань для назначения ткани.")
        if (node_id, face_id) in assigned:
            raise ValueError("Для одной грани можно назначить только одну ткань.")
        assigned.add((node_id, face_id))
        if not _layer_text(item["name"]) or len(item["name"]) > 80:
            raise ValueError("Укажите название ткани длиной до 80 символов.")
        if not isinstance(item["color"], str) or not HEX_COLOR.fullmatch(item["color"]):
            raise ValueError("Укажите цвет ткани в формате #RRGGBB.")


def _validate_drafting(marks):
    if not isinstance(marks, list) or len(marks) > 1000:
        raise ValueError("Разметка модели поддерживает до 1000 отрезков и направляющих.")
    identifiers = set()
    for mark in marks:
        if not isinstance(mark, dict) or set(mark) != {"id", "kind", "from", "to", "nodeIds"}:
            raise ValueError("Некорректный отрезок или направляющая в модели.")
        identifier = mark["id"]
        if (not _layer_text(identifier) or len(identifier) > 128 or identifier in identifiers
                or mark["kind"] not in ("line", "guide")):
            raise ValueError("Некорректный идентификатор или тип линии в модели.")
        identifiers.add(identifier)
        nodes = mark["nodeIds"]
        if (not isinstance(nodes, list) or len(nodes) > 2
                or any(not _layer_text(node) or len(node) > 1024 for node in nodes) or len(nodes) != len(set(nodes))):
            raise ValueError("Некорректная привязка линии к компонентам модели.")
        start, end = mark["from"], mark["to"]
        if any(not isinstance(point, list) or len(point) != 3
               or any(type(v) not in (int, float) or not -1e9 <= v <= 1e9 for v in point)
               for point in (start, end)) or math.dist(start, end) <= 0.001:
            raise ValueError("Укажите две разные пространственные точки линии в модели.")


def _validate_edge_notches(notches):
    if not isinstance(notches, list) or len(notches) > 100:
        raise ValueError("На детали можно поставить до 100 насечек.")
    identifiers = set()
    for notch in notches:
        if not isinstance(notch, dict) or set(notch) != {"id", "edgeId", "position"}:
            raise ValueError("Некорректная насечка на кромке детали.")
        identifier, edge_id, position = notch["id"], notch["edgeId"], notch["position"]
        if (not _layer_text(identifier) or len(identifier) > 128 or identifier in identifiers
                or not _layer_text(edge_id) or len(edge_id) > 128
                or type(position) not in (int, float) or not 0 < position < 1):
            raise ValueError("Укажите позицию насечки внутри кромки детали.")
        identifiers.add(identifier)


def check_surface_edit(state, before, after):
    from model_surface import surface_node_id, validate_model_edges

    validate_model_edges(before)
    earlier = {edge["id"]: edge for edge in before}
    current = {edge["id"]: edge for edge in after}
    affected = {(edge["nodeId"], edge["faceId"]) for identifier in earlier.keys() | current.keys()
                if earlier.get(identifier) != current.get(identifier)
                for edge in (earlier.get(identifier), current.get(identifier)) if edge is not None}
    if not affected:
        return
    edited_nodes = {surface_node_id(node, face) for node, face in affected}
    used = {record.get("nodeId") for field in ("panels", "seams") for record in state.get(field, [])}
    fabrics = [part for layer in state.get("fabricLayers", []) if "fabric:" + layer["id"] in used
               for part in layer["surfaces"]]
    for node, face in affected:
        if (node in used or surface_node_id(node, face) in used
                or any(part["nodeId"] in edited_nodes or part["nodeId"] == node and face in part["faces"] for part in fabrics)
                or any(item["nodeId"] == node and item["faceId"] == face for item in state.get("surfaceFabrics", []))):
            raise ValueError("На этой грани уже есть ткань или шов. Чтобы изменить её рёбра, сначала снимите разметку ткани.")


def markup(value):
    if not isinstance(value, dict) or set(value) - {
        "panels", "seams", "pairs", "joins", "material", "componentNames", "fabricLayers", "autoNotches", "surfaceFabrics",
        "fit", "transferSettings", "nextPanelNumber", "cadLinks", "hiddenNodes", "drafting", "modelEdges",
    }:
        raise ValueError("Некорректная разметка модели.")
    if "autoNotches" in value and not isinstance(value["autoNotches"], bool):
        raise ValueError("Некорректная настройка контрольных насечек.")
    hidden = value.get("hiddenNodes", [])
    if not isinstance(hidden, list) or len(hidden) > 5000 or any(not _layer_text(node) for node in hidden):
        raise ValueError("Некорректный список скрытых объектов.")
    links = value.get("cadLinks", {})
    if not isinstance(links, dict) or len(links) > 5000:
        raise ValueError("Некорректный список связей с чертежом.")
    for identifier, link in links.items():
        if (not _layer_text(identifier) or len(identifier) > 128 or not isinstance(link, dict)
                or not isinstance(link.get("hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", link["hash"])
                or not isinstance(link.get("frame"), list) or len(link["frame"]) != 3
                or any(type(n) not in (int, float) for n in link["frame"])):
            raise ValueError("Некорректная привязка лекала к чертежу.")
    for field, limit in (("panels", 120), ("seams", 400), ("pairs", 400), ("joins", 400)):
        members = value.get(field, [])
        if not isinstance(members, list) or len(members) > limit or any(not isinstance(x, dict) for x in members):
            raise ValueError("Слишком много деталей или швов в разметке модели.")
    for join in value.get("joins", []):
        marks = join.get("marks", [])
        if not _layer_text(join.get("id")) or len(join["id"]) > 128:
            raise ValueError("Некорректная общая кромка деталей.")
        if not isinstance(marks, list) or len(marks) > 100 or any(
            type(mark) not in (int, float) or not 0 < mark < 1 for mark in marks
        ):
            raise ValueError("Укажите позиции насечек внутри шва.")
    panels_by_id = {panel["id"]: panel for panel in value.get("panels", []) if isinstance(panel.get("id"), str)}
    placements = set()
    for contour in value.get("seams", []):
        if "placementFor" not in contour:
            continue
        panel_id = contour["placementFor"]
        panel = panels_by_id.get(panel_id) if isinstance(panel_id, str) else None
        if (panel is None or panel_id in placements or contour.get("nodeId") != panel.get("nodeId")
                or contour.get("kind") != "seam" or contour.get("closed") is not True or "seed" not in panel):
            raise ValueError("Уложенной детали нужен один замкнутый контур на её поверхности.")
        placements.add(panel_id)
    for panel in value.get("panels", []):
        _validate_edge_notches(panel.get("notches", []))
        if "number" in panel and (not isinstance(panel["number"], str) or not re.fullmatch(r"Д[0-9]{2,7}", panel["number"])):
            raise ValueError("Некорректный номер детали.")
        if "color" in panel and (not isinstance(panel["color"], str) or not HEX_COLOR.fullmatch(panel["color"])):
            raise ValueError("Укажите цвет детали в формате #RRGGBB.")
        if "fabricName" in panel and (not _layer_text(panel["fabricName"]) or len(panel["fabricName"]) > 80):
            raise ValueError("Укажите название ткани длиной до 80 символов.")
        if (panel.get("direction", "free") not in ("free", "grain", "nap")
                or panel.get("side", "") not in ("", "left", "right")
                or type(panel.get("quantity", 1)) is not int or not 1 <= panel.get("quantity", 1) <= 999):
            raise ValueError("Проверьте сторону, направление ткани и количество деталей.")
    if value.get("material") is not None and not isinstance(value["material"], dict):
        raise ValueError("Некорректные параметры ткани.")
    for field in ("fit", "transferSettings"):
        if field in value and not isinstance(value[field], dict):
            raise ValueError("Некорректные настройки лекал.")
    counter = value.get("nextPanelNumber", 1)
    if type(counter) is not int or not 1 <= counter <= 9999999:
        raise ValueError("Некорректный счётчик деталей.")
    names = value.get("componentNames", {})
    if (not isinstance(names, dict) or len(names) > 5000 or
            any(not isinstance(component, str) or not component or len(component) > 255 or not isinstance(name, str)
                or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name)
                for component, name in names.items())):
        raise ValueError("Некорректные названия компонентов.")
    _validate_fabric_layers(value.get("fabricLayers", []))
    _validate_surface_fabrics(value.get("surfaceFabrics", []))
    _validate_drafting(value.get("drafting", []))
    if "modelEdges" in value:
        from model_surface import validate_model_edges

        validate_model_edges(value["modelEdges"])
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, RecursionError) as error:
        raise ValueError("Некорректные числа в разметке модели.") from error
    if len(data.encode("utf-8")) > MAX_MARKUP_BYTES:
        raise ValueError("Разметка модели слишком велика.")
    return data


def transfer_manifest(state, result):
    patterns = {}
    used = set()
    for index, panel in enumerate(state.get("panels", []), 1):
        number = panel.get("number") or f"Д{index:02d}"
        while number in used:
            index += 1
            number = f"Д{index:02d}"
        used.add(number)
        identifier = panel["id"]
        patterns[identifier] = {"id": identifier, "number": number, "name": panel.get("name") or number,
                                **{key: panel[key] for key in ("side", "quantity", "direction", "fabricName") if key in panel},
                                "sewing": []}

    def add(label, ids, marks, method="stitch"):
        if len(ids) != 2 or any(identifier not in patterns for identifier in ids):
            return
        labels = [mark["label"] for mark in marks]
        for current, other in (ids, ids[::-1]):
            entry = {"name": label, "other": patterns[other]["number"], "marks": labels,
                     "markIds": [mark["id"] for mark in marks], "method": method}
            if entry not in patterns[current]["sewing"]:
                patterns[current]["sewing"].append(entry)

    computed = {pair["id"]: pair for pair in result["pairs"]}
    for index, pair in enumerate(state.get("pairs", []), 1):
        if pair["id"] in computed:
            add(pair.get("name") or f"Ш{index:02d}", [pair["a"]["panelId"], pair["b"]["panelId"]],
                computed[pair["id"]]["marks"], pair.get("method", "stitch"))
    names = {record["id"]: record.get("name") for record in state.get("joins", [])}
    for index, join in enumerate(result["confirmedJoins"], 1):
        add(names.get(join["id"]) or f"Общая кромка {index}", join["panelIds"], join["marks"])
    return patterns
