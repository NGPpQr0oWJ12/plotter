import json

from designer_cad import prepare_design
from nesting import identify_parts
from production_store import RecordNotFound, paths_by_part, quantity, same_part_geometry, verify_arrangement, width_mm


def _numbered(workspace):
    paths = workspace["paths"]
    if paths and any(not path.get("partNumber") for path in paths):
        paths = list(paths)
        for part in identify_parts(paths):
            notches = set(part["notchIndices"])
            for index in part["indices"]:
                paths[index] = {**paths[index], "partNumber": part["number"], "notch": index in notches}
    return {**workspace, "paths": paths}


def _reference_notches(entities):
    references = {entity["id"] for entity in entities if entity.get("reference")}
    symbols = [{key: value for key, value in entity.items() if key not in ("reference", "parentId")}
               for entity in entities if entity.get("notch") and
               (entity["id"] in references or entity.get("parentId") in references)]
    return prepare_design(symbols)["paths"] if symbols else []


def _part_sheet(number, paths, entry=None, *, source_geometry=False):
    title = next((path["partName"] for path in paths if path.get("partName")), None)
    sheet = {"number": number, "name": title or (entry and entry["name"]) or "Деталь", "paths": paths}
    if entry:
        for field in ("partLegend", "pattern"):
            if field in entry:
                sheet[field] = entry[field]
    if entry and source_geometry:
        entities = entry.get("entities", [])
        sheet["paths"] = [*entry["paths"], *_reference_notches(entities)]
        sheet["marks"] = [{"point": entity["point"]} for entity in entities if entity.get("type") == "point"
                          and entity.get("layer", "").startswith("Метка ")]
    return sheet


def registry_part_document(entry):
    if entry.get("kind") != "part":
        raise RecordNotFound("Деталь не найдена.")
    number = (entry.get("pattern") or {}).get("number")
    return {**_part_sheet(number, entry["paths"], entry, source_geometry=True), "name": entry["name"]}


def _arranged(workspace, width):
    try:
        if width is not None:
            verify_arrangement(workspace, width)
            return workspace
    except ValueError:
        pass
    return None


def kit_document(store, kit_id, amount=1, width=None):
    amount = quantity(amount)
    with store.connect() as db:
        kit = store.layout_kit(db, kit_id)
        records = {}
        paths = store.expand_parts(db, [{"id": kit["id"], "quantity": amount}], part_records=records)
        parts, templates = [], {}
        for number, contours in sorted(paths_by_part(paths).items()):
            part = records[number]
            if part["id"] not in templates:
                templates[part["id"]] = _part_sheet(number, contours, part, source_geometry=True)
            parts.append({**templates[part["id"]], "number": number})
        layout = None
        if width is not None:
            width = width_mm(width)
            slot = store.kit_layout_slot(db, kit_id, width, amount)
            if slot and slot["basis"] == store.kit_layout_basis(db, kit):
                layout = _numbered(json.loads(slot["data"]))
        return kit["name"], parts, {"quantity": amount, "widthMm": width if layout else None}, layout


def cut_document(store, cut_id, mode):
    with store.connect() as db:
        cut = store.cut_record(db, cut_id)
        snapshot = store.matching_workspace(db, cut, mode)
        order = snapshot["order"]
        quantity_per_order = store.order_header(db, cut["order_id"])["quantity"]
        records = {}
        try:
            originals = paths_by_part(store.expand_parts(db, cut["members"], quantity_per_order, part_records=records))
        except (ValueError, RecordNotFound):
            originals, records = {}, {}
        workspace = _numbered(snapshot["workspace"])
        parts = _workspace_sheets(store, db, workspace, originals, records)
        width = cut.get("widthMm") or workspace["optimization"].get("nesting", {}).get("widthMm")
        context = {**order, "fabric": cut["name"], "widthMm": width}
        return order["designation"] or order["modelName"] or cut["name"], parts, context, _arranged(workspace, width)


def _workspace_sheets(store, db, workspace, originals, records):
    by_name = {}
    parts = []
    for number, contours in sorted(paths_by_part(workspace["paths"]).items()):
        source = records.get(number)
        exact = bool(source and same_part_geometry(originals[number], contours))
        title = next((path["partName"] for path in contours if path.get("partName")), None)
        if not exact and title and (not source or title != source["name"]):
            if title not in by_name:
                candidates = []
                for (ident,) in db.execute("SELECT id FROM registry WHERE kind='part' AND name=?", (title,)):
                    labels = {}
                    try:
                        geometry = store.expand_parts(db, [{"id": ident, "quantity": 1}], part_records=labels)
                    except ValueError:
                        continue
                    candidates.append((geometry, labels[1]))
                by_name[title] = candidates
            matches = [record for geometry, record in by_name[title] if same_part_geometry(geometry, contours)]
            source = matches[0] if len(matches) == 1 else None
            exact = source is not None
        elif not exact and not title:
            source = None
        parts.append(_part_sheet(number, contours, source, source_geometry=exact))
    return parts


def standalone_document(store, workspace):
    if not workspace or not workspace["paths"]:
        raise ValueError("Добавьте детали в раскладку.")
    workspace = _numbered(workspace)
    with store.connect() as db:
        parts = _workspace_sheets(store, db, workspace, {}, {})
    width = workspace["optimization"].get("nesting", {}).get("widthMm")
    return "Самостоятельный раскрой", parts, {"widthMm": width}, _arranged(workspace, width)
