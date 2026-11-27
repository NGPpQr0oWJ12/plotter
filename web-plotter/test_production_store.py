from base64 import b64encode
from copy import deepcopy
from datetime import datetime
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from production_store import MOSCOW_TIME, ProductionStore, RecordNotFound, check_kit_geometry, verify_arrangement
from workspace import WorkspaceConflict


class ProductionTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store = ProductionStore(Path(root.name) / "production.sqlite3")
        self.model = self.store.save_registry({"kind": "folder", "model": True, "name": "Диван"})
        self.folder = self.store.save_registry({"kind": "folder", "parent": self.model["id"], "name": "Обивка"})
        self.part = self.store.save_registry({"kind": "part", "parent": self.folder["id"], "name": "Сиденье",
                                               "entities": [{"id": "closed", "layer": "0", "type": "polyline",
                                                             "points": [[0, 0], [40, 0], [40, 30], [0, 30]], "closed": True}]})
        self.kit = self.store.save_registry({"kind": "kit", "name": "Сиденья", "members": [self.member(self.part, 2)]})
        self.order = self.store.save_order({"number": "260550", "modelId": self.model["id"], "quantity": 2})

    @staticmethod
    def member(record, quantity=1):
        return {"id": record["id"], "quantity": quantity}

    def add_cut(self, name):
        self.order = self.store.save_cut({"action": "save-cut", "orderId": self.order["id"],
                                          "orderRevision": self.order["revision"], "name": name, "widthMm": 1500,
                                          "members": [self.member(self.kit)]})
        return self.order["cutId"]

    def arrange(self, cut_id):
        record = self.store.workspace(cut_id, "design")
        draft = record["workspace"]
        for path in draft["paths"]:
            path["points"] = [[x + 50 * path["partNumber"], y] for x, y in path["points"]]
        self.store.save_workspace({"cutId": cut_id, **record}, "design", verify_arrangement)

    def save_backup_layout(self, amount=1):
        draft = self.store.kit_layout(self.kit["id"], 1500, amount)
        for path in draft["workspace"]["paths"]:
            path["points"] = [[x + 50 * path["partNumber"], y] for x, y in path["points"]]
        return self.store.save_kit_layout(draft, verify_arrangement)

    def backup_model(self):
        return {"name": "Обивка.skp", "bounds": [[0, 0, 0], [20, 20, 0]], "nodes": [{
            "id": "root", "parent": None, "name": "Обивка", "visible": True,
            "vertices": [[0, 0, 0], [20, 0, 0], [0, 20, 0]], "triangles": [[0, 1, 2]],
            "faces": ["face"], "edges": [[0, 1], [1, 2], [2, 0]],
        }]}

    def test_export_restore_roundtrip_keeps_kit_and_attached_models_only(self):
        self.add_cut("Заказ не переносится")
        saved_layout = self.save_backup_layout()
        model = self.backup_model()
        model_id = self.store.stage_model(model)
        self.store.stage_model(model)
        markup = {"panels": [{"id": "front", "nodeId": "root", "faces": ["face"]}]}
        drawings = [self.store.save_registry({"kind": "drawing", "name": title, "parent": self.folder["id"],
                    "entities": self.part["entities"], "model3d": {"id": model_id, "state": markup}}, allow_3d=True)
                    for title in ("Лицевая сторона", "Изнанка")]
        snapshot = json.loads(json.dumps(self.store.export_registry(), ensure_ascii=False))
        self.assertEqual(set(snapshot), {"version", "entries", "kitLayouts", "models", "drawingModels"})
        self.assertEqual([item["id"] for item in snapshot["models"]], [model_id])
        target = ProductionStore(self.store.path.with_name("restored.sqlite3"))
        staged = target.stage_model(model)
        self.assertEqual(target.restore_registry(snapshot), {"entries": 6})
        self.assertEqual(target.export_registry(), snapshot)
        self.assertEqual(target.kit_layout(self.kit["id"], 1500, 1), saved_layout)
        self.assertEqual(target.orders(), {"orders": []})
        self.assertEqual(target.model(model_id), self.store.model(model_id))
        self.assertEqual(json.loads(gzip.decompress(target.model(staged))), model)
        for drawing in drawings:
            self.assertEqual(target.registry(drawing["id"]), drawing)
        target.check()
        damaged = deepcopy(snapshot)
        model["nodes"][0]["triangles"][0][-1] = 20
        damaged["models"][0]["data"] = b64encode(gzip.compress(json.dumps(model).encode())).decode()
        with self.assertRaisesRegex(ValueError, "геометрия SketchUp"):
            target.restore_registry(damaged)
        self.assertEqual(target.export_registry(), snapshot)

    def test_restore_keeps_stale_kit_layout_and_accepts_reordered_json_for_valid_layout(self):
        saved = self.save_backup_layout()
        target = ProductionStore(self.store.path.with_name("restored.sqlite3"))
        reordered = json.loads(json.dumps(self.store.export_registry(), sort_keys=True))
        target.restore_registry(reordered)
        restored = target.kit_layout(self.kit["id"], 1500, 1)
        self.assertTrue(restored["valid"])
        self.assertEqual(restored["workspace"]["paths"], saved["workspace"]["paths"])
        wider = {**self.part["entities"][0], "points": [[0, 0], [48, 0], [48, 30], [0, 30]]}
        self.store.save_registry({**self.part, "entities": [wider]})
        stale_snapshot = self.store.export_registry()
        self.assertFalse(stale_snapshot["kitLayouts"][0]["valid"])
        target.restore_registry(stale_snapshot)
        target_slot = target.export_registry()["kitLayouts"][0]
        self.assertEqual(target_slot["workspace"], stale_snapshot["kitLayouts"][0]["workspace"])
        self.assertEqual(target_slot["basis"], stale_snapshot["kitLayouts"][0]["basis"])
        self.assertFalse(target_slot["valid"])

    def test_restore_advances_existing_entries_and_slots_without_touching_cut_workspace(self):
        cut = self.add_cut("Велюр")
        layout = self.save_backup_layout()
        archived = self.store.export_registry()
        current_part = self.store.save_registry({**self.part, "name": "Переименовано"})
        latest_layout = self.store.save_kit_layout(layout, verify_arrangement)
        workspace, orders = self.store.workspace(cut, "design"), self.store.orders()
        self.store.save_registry({"kind": "folder", "name": "Лишняя папка"})
        self.assertEqual(self.store.restore_registry(archived), {"entries": 4})
        self.assertEqual(self.store.registry(self.part["id"])["revision"], current_part["revision"] + 1)
        restored = self.store.kit_layout(self.kit["id"], 1500, 1)
        self.assertTrue(restored["valid"])
        self.assertEqual(restored["revision"], latest_layout["revision"] + 1)
        self.assertEqual(restored["workspace"], layout["workspace"])
        self.assertEqual(self.store.workspace(cut, "design"), workspace)
        self.assertEqual(self.store.orders(), orders)
        with self.assertRaises(WorkspaceConflict):
            self.store.save_registry({**current_part, "name": "Устаревшая вкладка"})
        with self.assertRaises(WorkspaceConflict):
            self.store.save_kit_layout(latest_layout, verify_arrangement)

    def test_restore_rejects_cycles_or_orphaned_cuts_and_rolls_back_bad_kit_geometry(self):
        cut = self.add_cut("Велюр")
        self.save_backup_layout()
        original = self.store.export_registry()
        workspace = self.store.workspace(cut, "design")
        cycle = deepcopy(original)
        next(record for record in cycle["entries"] if record["id"] == self.model["id"])["parent"] = self.folder["id"]
        without_model = deepcopy(original)
        next(record for record in without_model["entries"] if record["id"] == self.model["id"])["data"]["model"] = False
        without_kit = {**original, "entries": [record for record in original["entries"] if record["id"] != self.kit["id"]],
                       "kitLayouts": []}
        changed_kind = deepcopy(without_kit)
        changed_kind["entries"].append({**next(record for record in original["entries"] if record["id"] == self.part["id"]),
                                         "id": self.kit["id"]})
        bad_geometry = deepcopy(original)
        bad_geometry["kitLayouts"][0]["workspace"]["paths"][0]["points"][1][0] += 1
        for snapshot, error in ((cycle, "циклическую"), (without_model, "существующего наряда"), (without_kit, "существующего раскроя"),
                                (changed_kind, "существующего раскроя"), (bad_geometry, "все исходные резы")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.store.restore_registry(snapshot)
            self.assertEqual(self.store.export_registry(), original)
            self.assertEqual(self.store.workspace(cut, "design"), workspace)

    def test_folder_color_and_registry_timestamps_follow_edit_move_and_restore(self):
        created = "2026-11-27T10:00:00.000Z"
        edited_at = "2026-11-27T10:01:00.000Z"
        moved_at = "2026-11-27T10:02:00.000Z"
        with patch("production_store.utc_now", side_effect=(created, edited_at, moved_at)):
            saved = self.store.save_registry({"kind": "folder", "name": "Новая папка", "color": "blue",
                                               "createdAt": "2000-01-01T00:00:00.000Z"})
            edited = self.store.save_registry({"id": saved["id"], "revision": saved["revision"],
                                                "kind": "folder", "name": "Переименовано"})
            moved = self.store.save_registry({"action": "move", "id": edited["id"], "revision": edited["revision"],
                                               "parent": self.folder["id"]})
        self.assertEqual((saved["createdAt"], saved["updatedAt"]), (created, created))
        self.assertEqual((edited["color"], edited["createdAt"], edited["updatedAt"]), ("blue", created, edited_at))
        self.assertEqual((moved["color"], moved["createdAt"], moved["updatedAt"]), ("blue", created, moved_at))
        target = ProductionStore(self.store.path.with_name("restored.sqlite3"))
        target.restore_registry(self.store.export_registry())
        self.assertEqual(target.registry(moved["id"]), moved)
        listed = {entry["id"]: entry for entry in target.registry()["entries"]}
        self.assertEqual(listed[moved["id"]], moved)
        for record in (self.part, self.kit):
            self.assertEqual(listed[record["id"]]["createdAt"], record["createdAt"])
            self.assertEqual(listed[record["id"]]["updatedAt"], record["updatedAt"])
        with self.assertRaisesRegex(ValueError, "цвет папки"):
            target.save_registry({**moved, "color": "invalid"})
        self.assertEqual(target.registry(moved["id"]), moved)

    def test_legacy_registry_dates_remain_unknown_until_observed(self):
        legacy = self.store.export_registry()
        identifier = self.model["id"]
        entry, = (record for record in legacy["entries"] if record["id"] == identifier)
        del entry["data"]["createdAt"], entry["data"]["updatedAt"]
        target = ProductionStore(self.store.path.with_name("restored.sqlite3"))
        target.restore_registry(legacy)
        old = target.registry(identifier)
        self.assertIsNone(old["createdAt"])
        self.assertIsNone(old["updatedAt"])
        now = "2026-11-27T10:00:00.000Z"
        with patch("production_store.utc_now", return_value=now):
            saved = target.save_registry({**old, "name": "Лекала"})
        self.assertIsNone(saved["createdAt"])
        self.assertEqual(saved["updatedAt"], now)
        self.store.restore_registry(legacy)
        self.assertEqual(self.store.registry(identifier)["createdAt"], self.model["createdAt"])

    def test_registry_trash_hides_folder_descendants_and_keeps_linked_kit_usable(self):
        expected_paths = self.store.collect([self.member(self.kit)])
        moved = self.store.trash_registry(self.folder)
        active = {entry["id"] for entry in self.store.registry()["entries"]}
        self.assertFalse({self.folder["id"], self.part["id"]} & active)
        self.assertEqual(self.store.collect([self.member(self.kit)]), expected_paths)
        trashed, = self.store.registry_trash()["entries"]
        self.assertEqual((trashed["id"], trashed["revision"], trashed["parent"], trashed["parentName"], trashed["trashedAt"]),
                         (moved["id"], moved["revision"], self.model["id"], self.model["name"], moved["trashedAt"]))
        with self.assertRaises(WorkspaceConflict):
            self.store.restore_trashed_registry(self.folder)
        with self.assertRaisesRegex(WorkspaceConflict, "корзины"):
            self.store.save_registry({**self.part, "name": "Скрытая деталь"})
        with self.assertRaisesRegex(WorkspaceConflict, "корзины"):
            self.store.save_registry({"kind": "folder", "name": "Новая", "parent": self.folder["id"]})
        restored = self.store.restore_trashed_registry(trashed)
        self.assertEqual((restored["parent"], restored["createdAt"]), (self.model["id"], self.folder["createdAt"]))
        self.assertNotIn("trashedAt", restored)
        self.assertEqual(self.store.registry_trash(), {"entries": []})
        self.assertIn(self.part["id"], {entry["id"] for entry in self.store.registry()["entries"]})

    def test_nested_trash_survives_archive_and_restores_child_to_desktop(self):
        child = self.store.trash_registry(self.folder)
        parent = self.store.trash_registry(self.model)
        target = ProductionStore(self.store.path.with_name("restored.sqlite3"))
        target.restore_registry(self.store.export_registry())
        self.assertEqual({item["id"]: item["trashedAt"] for item in target.registry_trash()["entries"]},
                         {child["id"]: child["trashedAt"], parent["id"]: parent["trashedAt"]})
        restored = target.restore_trashed_registry(child)
        self.assertIsNone(restored["parent"])
        self.assertEqual(target.registry(self.part["id"])["parent"], child["id"])
        remaining, = target.registry_trash()["entries"]
        self.assertEqual(remaining["id"], parent["id"])

    def test_purge_folder_keeps_parts_referenced_by_another_kit(self):
        paths = self.store.collect([self.member(self.kit)])
        expendable = self.store.save_registry({"kind": "folder", "name": "Без ссылок", "parent": self.folder["id"]})
        folder = self.store.trash_registry(self.folder)
        self.assertEqual(self.store.purge_registry_trash(folder), {"id": folder["id"], "deleted": True})
        self.assertEqual(self.store.registry_trash(), {"entries": []})
        self.assertEqual({entry["id"] for entry in self.store.registry()["entries"]},
                         {self.model["id"], self.kit["id"]})
        self.assertEqual(self.store.registry(self.kit["id"])["members"], self.kit["members"])
        self.assertEqual(self.store.collect([self.member(self.kit)]), paths)
        with self.assertRaises(RecordNotFound):
            self.store.registry(expendable["id"])
        archived_part = self.store.registry(self.part["id"])
        with self.assertRaises(WorkspaceConflict):
            self.store.trash_registry(archived_part)
        with self.assertRaises(WorkspaceConflict):
            self.store.move_registry_selection({"entries": [archived_part], "parent": None})
        with self.assertRaises(WorkspaceConflict):
            self.store.move_registry_selection({"entries": [self.kit], "parent": folder["id"]})
        self.store.check()

    def test_empty_trash_keeps_order_model_and_cut_kit_with_its_parts(self):
        kit = self.store.save_registry({**self.kit, "action": "move", "parent": self.folder["id"]})
        cut_id = self.add_cut("Обивка")
        before = self.store.workspace(cut_id, "design")
        paths = self.store.collect([self.member(kit)])
        spare = self.store.save_registry({"kind": "folder", "name": "Свободная"})
        child = self.store.save_registry({"kind": "folder", "name": "Дочерняя", "parent": spare["id"]})
        self.store.trash_registry(spare)
        self.store.trash_registry(self.folder)
        self.store.trash_registry(self.model)
        self.assertEqual(self.store.empty_registry_trash(), {"deleted": 2, "retained": 4})
        self.assertEqual(self.store.registry_trash(), {"entries": []})
        self.assertEqual(self.store.registry(), {"entries": []})
        self.assertEqual(self.store.empty_registry_trash(), {"deleted": 0})
        for record in (spare, child):
            with self.subTest(identifier=record["id"]), self.assertRaises(RecordNotFound):
                self.store.registry(record["id"])
        order = self.store.orders(self.order["id"])
        self.assertEqual(order["modelId"], self.model["id"])
        self.assertEqual(order["cuts"][0]["members"], [self.member(kit)])
        self.assertEqual(self.store.workspace(cut_id, "design"), before)
        self.assertEqual(self.store.collect([self.member(kit)]), paths)
        self.store.restore_registry(self.store.export_registry())
        self.assertEqual(self.store.registry(), {"entries": []})
        self.assertEqual(self.store.collect([self.member(kit)]), paths)
        self.store.check()

    def test_empty_trash_removes_both_a_kit_and_its_unreferenced_parts(self):
        self.store.trash_registry(self.folder)
        self.store.trash_registry(self.kit)
        self.assertEqual(self.store.empty_registry_trash(), {"deleted": 3})
        for record in (self.folder, self.part, self.kit):
            with self.subTest(identifier=record["id"]), self.assertRaises(RecordNotFound):
                self.store.registry(record["id"])

    def test_purge_drops_sketchup_data_after_its_last_drawing_is_removed(self):
        model = self.store.stage_model(self.backup_model())
        drawings = [self.store.save_registry({"kind": "drawing", "name": name, "entities": self.part["entities"],
                    "model3d": {"id": model, "state": {}}}, allow_3d=True) for name in ("Первый", "Второй")]
        for drawing in drawings:
            trashed = self.store.trash_registry(drawing)
            self.assertEqual(self.store.purge_registry_trash(trashed), {"id": drawing["id"], "deleted": True})
            if drawing is drawings[0]:
                self.assertTrue(self.store.model(model))
        with self.assertRaises(RecordNotFound):
            self.store.model(model)

    def test_move_registry_selection_is_atomic_and_keeps_nested_structure(self):
        destination = self.store.save_registry({"kind": "folder", "name": "Назначение"})
        selection = [self.kit, self.folder, self.part]
        before = self.store.export_registry()
        stale = {**self.part, "revision": self.part["revision"] + 1}
        with self.assertRaises(WorkspaceConflict):
            self.store.move_registry_selection({"entries": [self.kit, self.folder, stale], "parent": destination["id"]})
        with self.assertRaisesRegex(ValueError, "внутрь"):
            self.store.move_registry_selection({"entries": selection, "parent": self.folder["id"]})
        self.assertEqual(self.store.export_registry(), before)
        moved = self.store.move_registry_selection({"entries": selection, "parent": destination["id"]})["entries"]
        self.assertEqual([(entry["id"], entry["parent"], entry["revision"]) for entry in moved],
                         [(entry["id"], destination["id"], entry["revision"] + 1) for entry in selection[:2]])
        self.assertEqual(self.store.registry(self.part["id"]), self.part)
        self.assertEqual(self.store.move_registry_selection({"entries": [*moved, self.part], "parent": destination["id"]}),
                         {"entries": []})

    def test_trash_registry_selection_checks_all_entries_and_marks_only_roots(self):
        selection = [self.kit, self.folder, self.part]
        stale = {**self.part, "revision": self.part["revision"] + 1}
        with self.assertRaises(WorkspaceConflict):
            self.store.trash_registry_selection({"entries": [self.kit, self.folder, stale]})
        self.assertEqual(self.store.registry_trash(), {"entries": []})
        result = self.store.trash_registry_selection({"entries": selection})["entries"]
        self.assertEqual([entry["id"] for entry in result], [self.kit["id"], self.folder["id"]])
        self.assertEqual(len({entry["trashedAt"] for entry in result}), 1)
        self.assertEqual(self.store.registry(self.part["id"]), self.part)
        self.assertEqual({entry["id"] for entry in self.store.registry()["entries"]}, {self.model["id"]})
        with self.assertRaisesRegex(WorkspaceConflict, "корзины"):
            self.store.trash_registry_selection({"entries": [self.model, self.part]})
        self.assertEqual({entry["id"] for entry in self.store.registry_trash()["entries"]}, {self.kit["id"], self.folder["id"]})

    def test_copy_registry_selection_clones_active_tree_references_and_saved_kit_layout(self):
        model = self.store.save_registry({**self.model, "color": "violet"})
        hidden_part = self.store.save_registry({"kind": "part", "name": "Скрытая деталь", "parent": self.folder["id"],
                                               "entities": self.part["entities"]})
        external_part = self.store.save_registry({"kind": "part", "name": "Внешняя деталь", "entities": self.part["entities"]})
        kit = self.store.save_registry({**self.kit, "members": [self.member(self.part), self.member(hidden_part),
                                                              self.member(external_part)]})
        layout = self.save_backup_layout()
        trashed_part = self.store.trash_registry(hidden_part)
        self.store.purge_registry_trash(trashed_part)
        hidden_folder = self.store.save_registry({"kind": "folder", "name": "Скрытая папка", "parent": self.folder["id"]})
        self.store.save_registry({"kind": "folder", "name": "Скрытый потомок", "parent": hidden_folder["id"]})
        self.store.trash_registry(hidden_folder)
        model_id = self.store.stage_model(self.backup_model())
        drawing = self.store.save_registry({"kind": "drawing", "name": "Модель", "parent": self.folder["id"],
                                            "entities": self.part["entities"], "model3d": {"id": model_id, "state": {}}},
                                           allow_3d=True)
        now = "2026-11-27T10:03:00.000Z"
        with patch("production_store.utc_now", return_value=now):
            copied_model, copied_kit = self.store.copy_registry_selection({"entries": [model, kit, self.part], "parent": None})["entries"]
        self.assertEqual((copied_model["name"], copied_model["model"], copied_model["color"]),
                         (f"{model['name']} — копия", True, "violet"))
        self.assertNotEqual(copied_model["id"], model["id"])
        self.assertEqual((copied_model["createdAt"], copied_model["updatedAt"], copied_model["revision"]), (now, now, 1))
        entries = self.store.registry()["entries"]
        folder, = (entry for entry in entries if entry["parent"] == copied_model["id"])
        children = {entry["kind"]: entry for entry in entries if entry["parent"] == folder["id"]}
        self.assertEqual(set(children), {"drawing", "part"})
        copied_part = self.store.registry(children["part"]["id"])
        copied_drawing = self.store.registry(children["drawing"]["id"])
        self.assertNotEqual(copied_part["id"], self.part["id"])
        self.assertEqual(copied_part["paths"], self.part["paths"])
        self.assertEqual(copied_drawing["model3d"], drawing["model3d"])
        self.assertEqual(copied_drawing["entities"], drawing["entities"])
        copied_kit = self.store.registry(copied_kit["id"])
        self.assertEqual(copied_kit["members"], [self.member(copied_part), self.member(hidden_part), self.member(external_part)])
        copied_layout = self.store.kit_layout(copied_kit["id"], 1500, 1)
        self.assertTrue(copied_layout["valid"])
        self.assertEqual(copied_layout["revision"], 1)
        self.assertTrue(copied_layout["workspace"]["source"]["filename"].startswith(f"kit-{copied_kit['id']}-"))
        self.assertEqual([path["points"] for path in copied_layout["workspace"]["paths"]],
                         [path["points"] for path in layout["workspace"]["paths"]])
        self.assertEqual(copied_layout["workspace"]["paths"][0]["productName"], copied_model["name"])
        self.assertEqual(self.store.kit_layout(kit["id"], 1500, 1), layout)
        target = ProductionStore(self.store.path.with_name("restored-copy.sqlite3"))
        target.restore_registry(self.store.export_registry())
        self.assertEqual(target.kit_layout(copied_kit["id"], 1500, 1), copied_layout)
        target.check()

    def test_copy_registry_selection_is_atomic_and_numbers_destination_collisions(self):
        selection = [self.model, self.part]
        before = self.store.export_registry()
        with self.assertRaises(WorkspaceConflict):
            self.store.copy_registry_selection({"entries": [self.model, {**self.part, "revision": 0}], "parent": None})
        with self.assertRaisesRegex(ValueError, "внутрь"):
            self.store.copy_registry_selection({"entries": selection, "parent": self.folder["id"]})
        self.assertEqual(self.store.export_registry(), before)
        copies = [self.store.copy_registry_selection({"entries": selection, "parent": None})["entries"] for _ in range(2)]
        self.assertEqual([copy[0]["name"] for copy in copies], [f"{self.model['name']} — копия", f"{self.model['name']} — копия 2"])
        self.assertTrue(all(len(copy) == 1 for copy in copies))

    def test_registry_rename_changes_only_title_revision_and_edit_time(self):
        folder = self.store.save_registry({**self.folder, "color": "violet"})
        model = self.store.stage_model(self.backup_model())
        drawing = self.store.save_registry({"kind": "drawing", "name": "Чертёж", "entities": self.part["entities"],
                                            "model3d": {"id": model, "state": {}}}, allow_3d=True)
        layout = self.save_backup_layout()
        now = "2026-11-27T10:03:00.000Z"
        with patch("production_store.utc_now", return_value=now):
            for entry in (folder, self.part, drawing, self.kit):
                with self.subTest(kind=entry["kind"]):
                    title = entry["name"] + " новая"
                    payload = {"id": entry["id"], "revision": entry["revision"], "name": f" {title} "}
                    renamed = self.store.rename_registry(payload)
                    self.assertEqual(renamed, {**entry, "name": title, "revision": entry["revision"] + 1, "updatedAt": now})
                    self.assertEqual(self.store.rename_registry({**payload, "revision": renamed["revision"]}), renamed)
                    with self.assertRaises(WorkspaceConflict):
                        self.store.rename_registry(payload)
        saved_layout = self.store.kit_layout(self.kit["id"], 1500, 1)
        self.assertTrue(saved_layout["valid"])
        self.assertEqual(saved_layout["workspace"], layout["workspace"])
        self.store.trash_registry(self.store.registry(folder["id"]))
        part = self.store.registry(self.part["id"])
        with self.assertRaisesRegex(WorkspaceConflict, "корзины"):
            self.store.rename_registry({**part, "name": "Скрытая деталь"})

    def test_registry_tree_and_conflicting_writes(self):
        with self.assertRaises(ValueError):
            self.store.save_registry({**self.model, "parent": self.folder["id"]})
        self.store.save_registry({**self.model, "name": "Диван с подушками"})
        with self.assertRaises(WorkspaceConflict):
            self.store.save_registry({**self.model, "name": "Старая вкладка"})

    def test_saved_part_centers_cut_geometry_and_keeps_legend_internal(self):
        entities = [
            {"id": "cut", "type": "circle", "layer": "Рез", "center": [105, 207], "radius": 23},
            {"id": "notch", "type": "line", "layer": "Надсечки", "parentId": "cut", "notch": True,
             "start": [128, 207], "end": [129, 207]},
            {"id": "axis", "type": "line", "layer": "Долевая", "reference": True,
             "start": [85, 180], "end": [150, 180]},
            {"id": "seam", "type": "spline", "layer": "Шов", "reference": True,
             "fitPoints": [[100, 200], [105, 201], [110, 200]]},
        ]
        draft = {"kind": "part", "parent": self.folder["id"], "name": "Круг", "entities": entities,
                 "partLegend": {"developer": "", "comments": "Уточнить припуск\nПроверить долевую", "date": "01.1999"}}
        saved = self.store.save_registry(draft)
        shapes = {entity["id"]: entity for entity in saved["entities"]}
        self.assertEqual(shapes["cut"]["center"], [0, 0])
        self.assertEqual(shapes["notch"]["start"], [23, 0])
        self.assertEqual(shapes["notch"]["parentId"], "cut")
        self.assertEqual(shapes["axis"]["start"], [-20, -27])
        self.assertEqual(shapes["seam"]["fitPoints"], [[-5, -7], [0, -6], [5, -7]])
        self.assertEqual(saved["partLegend"], {**draft["partLegend"], "date": datetime.now(MOSCOW_TIME).strftime("%m.%Y")})
        self.assertEqual(self.store.registry(saved["id"])["partLegend"], saved["partLegend"])
        resaved = self.store.save_registry({**saved, "partLegend": {**saved["partLegend"], "developer": "  И. Иванов  ", "date": "12.2000"}})
        self.assertEqual(resaved["entities"], saved["entities"])
        self.assertEqual(resaved["partLegend"]["developer"], "И. Иванов")
        self.assertEqual(resaved["partLegend"]["date"], datetime.now(MOSCOW_TIME).strftime("%m.%Y"))
        collected = self.store.collect([self.member(resaved)])["paths"]
        self.assertEqual(len(collected), 2)
        self.assertTrue(all("partLegend" not in path for path in collected))

    def test_registry_move_preserves_content_and_delete_respects_history(self):
        with self.assertRaises(WorkspaceConflict):
            self.store.delete_registry(self.folder)
        with self.assertRaises(WorkspaceConflict):
            self.store.delete_registry(self.part)
        with self.assertRaises(ValueError):
            self.store.save_registry({"action": "move", "id": self.model["id"], "revision": self.model["revision"],
                                      "parent": self.folder["id"]})

        moved = self.store.save_registry({"action": "move", "id": self.kit["id"], "revision": self.kit["revision"],
                                          "parent": self.folder["id"]})
        self.assertEqual(moved["members"], self.kit["members"])
        self.assertEqual(moved["name"], self.kit["name"])
        self.assertEqual(moved["parent"], self.folder["id"])
        with self.assertRaises(WorkspaceConflict):
            self.store.delete_registry(self.kit)

        cut_id = self.add_cut("Велюр")
        with self.store.connect() as db:
            before = self.store.find_workspace(db, cut_id, "design")
        self.store.delete_order(self.order)
        with self.assertRaises(WorkspaceConflict):
            self.store.delete_registry(moved)
        with self.store.connect() as db:
            self.assertEqual(self.store.find_workspace(db, cut_id, "design"), before)

        loose = self.store.save_registry({"kind": "folder", "name": "Пусто"})
        model = self.store.save_registry({"action": "move", "id": self.model["id"], "revision": self.model["revision"],
                                          "parent": loose["id"]})
        self.assertTrue(model["model"])
        leaf = self.store.save_registry({"kind": "folder", "name": "Удалить", "parent": self.model["id"]})
        self.assertEqual(self.store.delete_registry(leaf), {"id": leaf["id"], "deleted": True})
        with self.assertRaises(RecordNotFound):
            self.store.registry(leaf["id"])

    def test_saved_kit_layout_seeds_only_matching_new_materials_as_independent_copies(self):
        old_cut = self.add_cut("До сохранения")
        before = self.store.workspace(old_cut, "design")
        draft = self.store.kit_layout(self.kit["id"], 1500, 2)
        self.assertEqual((draft["revision"], draft["saved"], draft["valid"], draft["name"]),
                         (0, False, False, self.kit["name"]))
        for path in draft["workspace"]["paths"]:
            path["points"] = [[60 * path["partNumber"] - y, x + 10] for x, y in path["points"]]
            path["partAngle"] = 90
        saved = self.store.save_kit_layout(draft, verify_arrangement)
        self.assertEqual((saved["revision"], saved["valid"]), (1, True))
        self.assertEqual(self.store.registry(self.kit["id"])["layouts"],
                         [{"widthMm": 1500, "quantity": 2, "revision": 1, "valid": True}])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_kit_layout(draft, verify_arrangement)
        self.assertEqual(self.store.workspace(old_cut, "design"), before)

        seeded_id = self.add_cut("Точное совпадение")
        seeded = self.store.workspace(seeded_id, "design")
        self.assertTrue(self.order["cuts"][-1]["prepared"])
        self.assertEqual(seeded["workspace"]["paths"], saved["workspace"]["paths"])
        self.assertEqual(seeded["workspace"]["source"]["filename"], f"cut-{seeded_id}.dxf")
        for path in saved["workspace"]["paths"]:
            path["points"] = [[x, y + 20] for x, y in path["points"]]
        changed = self.store.save_kit_layout(saved, verify_arrangement)
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.store.workspace(seeded_id, "design"), seeded)

        for width, members in ((1400, [self.member(self.kit)]), (1500, [self.member(self.kit, 2)]),
                               (1500, [self.member(self.kit), self.member(self.part)])):
            self.order = self.store.save_cut({"action": "save-cut", "orderId": self.order["id"],
                                              "orderRevision": self.order["revision"], "name": "Другой материал",
                                              "widthMm": width, "members": members})
            material = self.order["cuts"][-1]
            self.assertFalse(material["prepared"])
            default = self.store.workspace(material["id"], "design")["workspace"]
            self.assertEqual(default["paths"][0]["points"], before["workspace"]["paths"][0]["points"])

    def test_existing_kit_layout_recovers_display_numbers_by_part_identity(self):
        loose = self.store.save_registry({"kind": "part", "name": self.part["name"], "entities": self.part["entities"]})
        kit = self.store.save_registry({**self.kit, "members": [self.member(loose, 3), self.member(self.part, 2)]})
        draft = self.store.kit_layout(kit["id"], 1500, 1)
        for path in draft["workspace"]["paths"]:
            path["points"] = [[50 * path["partNumber"] - y, x + 10] for x, y in path["points"]]
            path["partAngle"] = 90
        saved = self.store.save_kit_layout(draft, verify_arrangement)
        paths = saved["workspace"]["paths"]
        self.assertEqual([(p["partNumber"], p["partDisplayNumber"], p["partName"], p.get("productName")) for p in paths],
                         [(1, "3/1", "Сиденье", None), (2, "3/2", "Сиденье", None), (3, "3/3", "Сиденье", None),
                          (4, "5/1", "Сиденье", "Диван"), (5, "5/2", "Сиденье", "Диван")])
        legacy = {**saved["workspace"], "paths": [{k: v for k, v in path.items() if k != "partDisplayNumber"}
                                                   for path in paths]}
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kit_layouts SET data=? WHERE kit_id=?", (json.dumps(legacy), kit["id"]))
        self.assertEqual(self.store.kit_layout(kit["id"], 1500, 1), saved)

    def test_legacy_operator_layout_recovers_names_without_changing_cut_revision(self):
        cut_id = self.add_cut("Велюр")
        self.arrange(cut_id)
        self.order = self.store.publish_order(self.order, verify_arrangement)
        record = self.store.progress(cut_id, "start", "operator")
        saved = self.store.save_workspace({"cutId": cut_id, **record}, "operator", verify_arrangement)
        legacy = saved["workspace"]
        for path in legacy["paths"]:
            path.pop("partName")
            path.pop("productName")
            path.pop("partDisplayNumber")
        legacy["paths"][0]["points"][1][0] += 5
        with self.store.connect(write=True) as db:
            db.execute("UPDATE workspaces SET data=? WHERE cut_id=? AND mode='operator'", (json.dumps(legacy), cut_id))
        restored = self.store.workspace(cut_id, "operator")
        actual = restored["workspace"]["paths"]
        self.assertEqual([(p.get("partName"), p.get("productName")) for p in actual],
                         [(None, None), *(("Сиденье", "Диван"),) * 3])
        self.assertEqual([p.get("partDisplayNumber") for p in actual], [None, "4/2", "4/3", "4/4"])
        self.assertEqual(restored["cutRevision"], saved["cutRevision"])
        persisted = self.store.save_workspace({"cutId": cut_id, **restored}, "operator", verify_arrangement)
        self.assertEqual(persisted["cutRevision"], saved["cutRevision"])
        self.assertEqual(persisted["workspace"]["paths"], actual)

    def test_kit_layout_rejects_changed_geometry_and_invalidates_after_source_edit(self):
        draft = self.store.kit_layout(self.kit["id"], 1500, 2)
        for path in draft["workspace"]["paths"]:
            path["points"] = [[x + 50 * path["partNumber"], y] for x, y in path["points"]]
        first = draft["workspace"]["paths"][0]
        first["points"][1][0] += 1
        with self.assertRaises(ValueError):
            self.store.save_kit_layout(draft, verify_arrangement)
        first["points"][1][0] -= 1
        saved = self.store.save_kit_layout(draft, verify_arrangement)
        cut_id = self.add_cut("Сохранённый состав")
        before = self.store.workspace(cut_id, "design")

        part = self.store.save_registry({**self.part, "name": "Новое название"})
        self.assertTrue(self.store.kit_layout(self.kit["id"], 1500, 2)["valid"])
        changed = {**part["entities"][0], "points": [[0, 0], [45, 0], [45, 30], [0, 30]]}
        self.store.save_registry({**part, "entities": [changed]})
        with self.assertRaises(WorkspaceConflict):
            self.store.save_kit_layout(saved, verify_arrangement)
        stale = self.store.kit_layout(self.kit["id"], 1500, 2)
        self.assertEqual((stale["saved"], stale["valid"], stale["revision"]), (True, False, 1))
        self.assertFalse(self.store.registry(self.kit["id"])["layouts"][0]["valid"])
        self.assertNotEqual(stale["workspace"]["source"]["filename"], saved["workspace"]["source"]["filename"])
        copied, = self.store.copy_registry_selection({"entries": [self.kit], "parent": None})["entries"]
        self.assertEqual(self.store.registry(copied["id"])["layouts"], [])
        self.add_cut("Новый размер детали")
        self.assertFalse(self.order["cuts"][-1]["prepared"])
        self.assertEqual(self.store.workspace(cut_id, "design"), before)

    def test_kit_preserves_directed_part_requirement_when_reversing_nap(self):
        part = {"partNumber": 1, "partRotation": "none", "layer": "0", "closed": True,
                "points": [[0, 0], [40, 0], [28, 30]]}
        turned = {**part, "partAngle": 180, "points": [[40 - x, 30 - y] for x, y in part["points"]]}
        with self.assertRaisesRegex(ValueError, "направленной ткани"):
            check_kit_geometry([part], {"paths": [turned], "excluded": [], "optimization": {}})
        check_kit_geometry([part], {"paths": [turned], "excluded": [], "optimization": {"napReversed": True}})
        with self.assertRaisesRegex(ValueError, "направленной ткани"):
            check_kit_geometry([part], {"paths": [part], "excluded": [], "optimization": {"napReversed": True}})

    def test_centering_a_legacy_part_preserves_saved_kit_layout(self):
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT data FROM registry WHERE id=?", (self.part["id"],)).fetchone()
            legacy = json.loads(row["data"])
            for shape in legacy["entities"] + legacy["paths"]:
                shape["points"] = [[x + 235, y + 90] for x, y in shape["points"]]
            db.execute("UPDATE registry SET data=? WHERE id=?", (json.dumps(legacy), self.part["id"]))
        draft = self.store.kit_layout(self.kit["id"], 1500, 1)
        for path in draft["workspace"]["paths"]:
            path["points"] = [[80 * path["partNumber"] - y, x + 12] for x, y in path["points"]]
            path["partAngle"] = 90
        saved = self.store.save_kit_layout(draft, verify_arrangement)
        metadata = {key: self.part[key] for key in ("id", "revision", "kind", "name", "parent")}
        centered = self.store.save_registry({**metadata, "partLegend": {}})
        self.assertEqual(centered["entities"], self.part["entities"])
        restored = self.store.kit_layout(self.kit["id"], 1500, 1)
        self.assertTrue(restored["valid"])
        self.assertEqual(restored["revision"], saved["revision"])
        self.assertEqual(restored["workspace"]["paths"], saved["workspace"]["paths"])
        self.assertNotEqual(restored["workspace"]["source"]["filename"], saved["workspace"]["source"]["filename"])
        self.assertTrue(self.store.save_kit_layout(restored, verify_arrangement)["valid"])

    def test_no_width_prefers_newest_distinct_compatible_kit_layout(self):
        def save_variant(width):
            draft = self.store.kit_layout(self.kit["id"], width, 2)
            for path in draft["workspace"]["paths"]:
                path["points"] = [[x + 50 * path["partNumber"], y + width / 10] for x, y in path["points"]]
            return self.store.save_kit_layout(draft, verify_arrangement)

        save_variant(1000)
        latest = save_variant(1400)
        first = save_variant(1000)
        self.assertEqual(first["revision"], 2)

        def reject_latest(draft, width):
            if width == 1400:
                raise ValueError("Недоступная ширина.")
            verify_arrangement(draft, width)

        for options, verifier, expected in (
                ({}, verify_arrangement, latest), ({"widthMm": None}, reject_latest, first),
                ({"widthMm": 1000}, verify_arrangement, first)):
            with self.subTest(options=options, verifier=verifier.__name__):
                self.order = self.store.save_cut({"orderId": self.order["id"], "orderRevision": self.order["revision"],
                                                  "name": "Ткань", "members": [self.member(self.kit)], **options}, verifier)
                cut = self.order["cuts"][-1]
                draft = self.store.workspace(cut["id"], "design")
                self.assertEqual((cut["widthMm"], cut["prepared"], draft["widthMm"]),
                                 (expected["widthMm"], True, expected["widthMm"]))
                self.assertEqual(draft["workspace"]["paths"], expected["workspace"]["paths"])

    def test_new_order_without_model_or_width_can_be_assigned_and_arranged_by_operator(self):
        today = datetime.now(MOSCOW_TIME).date().isoformat()
        order = self.store.save_order({"number": "260555", "notes": "Ткань у оператора"})
        self.assertEqual((order["modelId"], order["modelName"], order["quantity"], order["dueOn"]), (None, None, 1, None))
        self.assertIn(order["placedOn"], (today, datetime.now(MOSCOW_TIME).date().isoformat()))
        order = self.store.save_cut({"orderId": order["id"], "orderRevision": order["revision"], "name": "Ткань",
                                     "members": [self.member(self.part)]})
        cut_id = order["cutId"]
        draft = self.store.workspace(cut_id, "design")
        self.assertIsNone(draft["widthMm"])
        self.assertNotIn("nesting", draft["workspace"]["optimization"])
        draft["workspace"]["excluded"] = [0]
        saved = self.store.save_workspace({"cutId": cut_id, **draft}, "design", verify_arrangement)
        with self.assertRaisesRegex(ValueError, "все детали"):
            self.store.publish_order(order, verify_arrangement)
        saved["workspace"]["excluded"] = []
        self.store.save_workspace({"cutId": cut_id, **saved}, "design", verify_arrangement)
        published = self.store.publish_order(order, verify_arrangement)
        cut, = published["cuts"]
        self.assertEqual((cut["widthMm"], cut["prepared"], cut["released"]), (None, False, True))
        self.assertIsNone(self.store.workspace(cut_id, "operator")["widthMm"])

        demo = self.store.progress(cut_id, "start", "demo")
        nesting = {"widthMm": 700, "gapMm": 3, "marginMm": 0, "rotation": "quarter"}
        demo["workspace"]["optimization"]["nesting"] = nesting
        saved_demo = self.store.save_workspace({"cutId": cut_id, **demo}, "demo", verify_arrangement)
        self.assertIsNone(saved_demo["widthMm"])
        opened = self.store.progress(cut_id, "start", "operator")
        self.assertEqual((opened["widthMm"], opened["opened"], opened["status"]), (None, True, "active"))
        opened["workspace"]["optimization"]["nesting"] = {**nesting, "widthMm": 10}
        invalid = self.store.save_workspace({"cutId": cut_id, **opened}, "operator", verify_arrangement)
        self.assertIsNone(invalid["widthMm"])
        self.assertFalse(self.store.orders(order["id"])["cuts"][0]["prepared"])

        invalid["workspace"]["optimization"]["nesting"] = nesting
        saved = self.store.save_workspace({"cutId": cut_id, **invalid}, "operator", verify_arrangement)
        self.assertEqual(saved["widthMm"], 700)
        released = self.store.release_cut(cut_id, saved["revision"])
        self.assertEqual((released["widthMm"], released["status"]), (700, "pending"))
        public, = self.store.orders(published_only=True)["orders"]
        self.assertEqual((public["materials"][0]["widthMm"], public["materials"][0]["prepared"]), (700, True))
        reopened = self.store.reopen_order(published)
        pending, = reopened["cuts"]
        self.assertEqual((pending["widthMm"], pending["prepared"]), (None, False))
        self.assertNotIn("nesting", self.store.workspace(cut_id, "design")["workspace"]["optimization"])
        republished = self.store.publish_order(reopened, verify_arrangement)
        self.assertEqual((republished["cuts"][0]["widthMm"], republished["cuts"][0]["released"]), (None, True))

    def test_designer_adopts_width_only_after_valid_layout_and_metadata_edits_keep_it(self):
        order = self.store.save_cut({"orderId": self.order["id"], "orderRevision": self.order["revision"],
                                     "name": "Подкладка", "members": [self.member(self.part)]})
        cut_id = order["cutId"]
        record = self.store.workspace(cut_id, "design")
        record["workspace"]["optimization"]["nesting"] = {"widthMm": 750, "gapMm": 3, "marginMm": 0,
                                                             "rotation": "quarter"}
        invalid = self.store.save_workspace({"cutId": cut_id, **record}, "design", verify_arrangement)
        self.assertIsNone(invalid["widthMm"])
        for path in invalid["workspace"]["paths"]:
            path["points"] = [[x + 50 * path["partNumber"], y] for x, y in path["points"]]
        saved = self.store.save_workspace({"cutId": cut_id, **invalid}, "design", verify_arrangement)
        self.assertEqual(saved["widthMm"], 750)
        renamed_order = self.store.save_order({"id": order["id"], "revision": order["revision"], "number": "260556",
                                               "notes": "Сначала подкладка"})
        for field in ("modelId", "modelName", "quantity", "placedOn", "dueOn"):
            self.assertEqual(renamed_order[field], order[field])
        renamed = self.store.save_cut({"id": cut_id, "orderId": order["id"], "orderRevision": renamed_order["revision"],
                                       "name": "Ткань"})
        kept = self.store.workspace(cut_id, "design")
        self.assertEqual((kept["widthMm"], kept["cutRevision"], kept["workspace"]),
                         (750, saved["cutRevision"], saved["workspace"]))
        self.assertTrue(self.store.publish_order(renamed, verify_arrangement)["cuts"][0]["prepared"])

    def test_snapshots_operator_progress_demo_and_reopen(self):
        first, second = self.add_cut("Основная ткань"), self.add_cut("Подкладка")
        self.assertEqual(self.store.orders(published_only=True), {"orders": []})
        with self.assertRaisesRegex(ValueError, "пересекаются"):
            self.store.publish_order(self.order, verify_arrangement)
        for cut_id in (first, second):
            self.arrange(cut_id)
        self.store.publish_order(self.order, verify_arrangement)
        reviewed = self.store.workspace(first, "preview")["workspace"]
        self.store.save_registry({**self.part, "entities": [{**self.part["entities"][0],
            "points": [[0, 0], [25, 0], [25, 20], [0, 20]]}]})
        self.assertEqual(self.store.workspace(first, "operator")["workspace"], reviewed)
        self.assertEqual(len(reviewed["paths"]), 4)
        demo = self.store.progress(first, "start", "demo")
        self.store.save_workspace({"cutId": first, **demo}, "demo", verify_arrangement)
        self.assertEqual(self.store.workspace(first, "operator")["status"], "pending")
        work = self.store.progress(first, "start", "operator")
        work["workspace"]["paths"][0]["points"][0][0] += 1
        saved = self.store.save_workspace({"cutId": first, **work}, "operator", verify_arrangement)
        self.assertGreater(saved["cutRevision"], work["cutRevision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_workspace({"cutId": first, **work}, "operator", verify_arrangement)
        self.store.progress(first, "finish", "operator", saved["revision"])
        reopened = ProductionStore(self.store.path)
        self.assertEqual(reopened.workspace(first, "operator")["workspace"], saved["workspace"])
        self.assertEqual(reopened.workspace(first, "preview")["workspace"], reviewed)
        self.assertEqual(reopened.workspace(second, "operator")["status"], "pending")
        self.assertEqual([(o["done"], o["total"]) for o in reopened.orders(published_only=True)["orders"]], [(1, 2)])
        backup = ProductionStore(self.store.path.with_name("backup.sqlite3"))
        self.store.backup(backup.path)
        backup.check()
        self.assertEqual(backup.orders(self.order["id"]), reopened.orders(self.order["id"]))

    def test_empty_operator_layout_persists_after_deleting_final_part(self):
        cut_id = self.add_cut("Основная ткань")
        self.arrange(cut_id)
        self.store.publish_order(self.order, verify_arrangement)
        work = self.store.progress(cut_id, "start", "operator")
        work["workspace"].update(paths=[], excluded=[])

        self.store.save_workspace({"cutId": cut_id, **work}, "operator", verify_arrangement)

        reopened = ProductionStore(self.store.path)
        self.assertEqual(reopened.workspace(cut_id, "operator")["workspace"]["paths"], [])
        self.assertEqual(len(reopened.workspace(cut_id, "preview")["workspace"]["paths"]), 4)

    def test_delete_uses_revision_and_preserves_v1_records(self):
        cut_id = self.add_cut("Основная ткань")
        order_id = self.order["id"]
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            old_data = json.loads(db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()[0])
            self.assertNotIn("deleted", old_data)
            cut = tuple(db.execute("SELECT * FROM cuts WHERE id=?", (cut_id,)).fetchone())
            workspace = tuple(db.execute("SELECT * FROM workspaces WHERE cut_id=?", (cut_id,)).fetchone())
        with self.assertRaises(WorkspaceConflict):
            self.store.delete_order({"id": order_id, "revision": self.order["revision"] - 1})
        self.assertEqual(self.store.orders(order_id)["revision"], self.order["revision"])
        self.assertEqual(self.store.delete_order(self.order), {"id": order_id, "deleted": True})
        self.assertEqual(self.store.orders(), {"orders": []})
        for validate in (lambda: self.store.orders(order_id),
                         lambda: self.store.workspace(cut_id, "design"),
                         lambda: self.store.workspace(cut_id, "preview"),
                         lambda: self.store.progress(cut_id, "start", "operator")):
            with self.assertRaises(RecordNotFound):
                validate()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            self.assertEqual(row["revision"], self.order["revision"] + 1)
            self.assertEqual(json.loads(row["data"]), {**old_data, "deleted": True})
            self.assertEqual(tuple(db.execute("SELECT * FROM cuts WHERE id=?", (cut_id,)).fetchone()), cut)
            self.assertEqual(tuple(db.execute("SELECT * FROM workspaces WHERE cut_id=?", (cut_id,)).fetchone()), workspace)
        self.assertEqual(self.store.registry(self.part["id"]), self.part)

    def test_active_cut_blocks_deletion_and_reopening(self):
        first, second = self.add_cut("Ткань"), self.add_cut("Подкладка")
        for cut_id in (first, second):
            self.arrange(cut_id)
        order = self.store.publish_order(self.order, verify_arrangement)
        work = self.store.progress(first, "start", "operator")
        for update in (self.store.delete_order, self.store.reopen_order):
            with self.assertRaises(WorkspaceConflict):
                update(order)
        self.store.progress(first, "finish", "operator", work["revision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.reopen_order(order)
        self.assertEqual(self.store.delete_order(order), {"id": order["id"], "deleted": True})
        self.assertEqual(self.store.orders(published_only=True), {"orders": []})
        with self.assertRaises(RecordNotFound):
            self.store.progress(second, "start", "operator")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM cuts WHERE id=?", (first,)).fetchone()[0], "done")

    def test_reopening_all_pending_cuts_preserves_design_and_rebases_demo(self):
        cut_id = self.add_cut("Ткань")
        self.arrange(cut_id)
        original = self.store.workspace(cut_id, "design")
        order = self.store.publish_order(self.order, verify_arrangement)
        demo = self.store.progress(cut_id, "start", "demo")
        demo["workspace"]["paths"][0]["points"][0][0] += 1
        self.store.save_workspace({"cutId": cut_id, **demo}, "demo", verify_arrangement)
        with self.assertRaises(WorkspaceConflict):
            self.store.reopen_order(self.order)

        draft = self.store.reopen_order(order)
        self.assertFalse(draft["published"])
        self.assertEqual(draft["revision"], order["revision"] + 1)
        self.assertEqual(draft["cuts"], order["cuts"])
        self.assertEqual(self.store.orders(published_only=True), {"orders": []})
        self.assertEqual(self.store.workspace(cut_id, "design"), original)
        with self.assertRaises(WorkspaceConflict):
            self.store.progress(cut_id, "start", "operator")
        edited = self.store.save_order({**draft, "number": "260551"})
        sent = self.store.publish_order(edited, verify_arrangement)
        self.assertEqual(sent["number"], "260551")
        self.assertEqual(self.store.workspace(cut_id, "demo")["revision"], 0)
        self.assertEqual(self.store.workspace(cut_id, "demo")["workspace"], original["workspace"])

    def test_changing_draft_quantity_rebuilds_each_cut_and_keeps_materials(self):
        first, second = self.add_cut("Ткань"), self.add_cut("Подкладка")
        for cut_id in (first, second):
            self.arrange(cut_id)
        baseline = self.store.orders(self.order["id"])
        existing = self.store.workspace(first, "design")
        self.assertTrue(baseline["cuts"][0]["prepared"])
        other = self.store.save_registry({"kind": "folder", "model": True, "name": "Кресло"})
        with self.assertRaisesRegex(ValueError, "изделие"):
            self.store.save_order({**baseline, "modelId": other["id"], "quantity": 3})
        with self.assertRaises(ValueError):
            self.store.save_order({**baseline, "quantity": 50_000})
        self.assertEqual(self.store.orders(self.order["id"]), baseline)
        self.assertEqual(self.store.workspace(first, "design"), existing)

        updated = self.store.save_order({**baseline, "quantity": 3})
        self.assertEqual(updated["revision"], baseline["revision"] + 1)
        self.assertEqual([item["id"] for item in updated["cuts"]], [first, second])
        for cut, old in zip(updated["cuts"], baseline["cuts"]):
            self.assertEqual(cut["members"], old["members"])
            self.assertEqual(cut["widthMm"], old["widthMm"])
            self.assertEqual(cut["revision"], old["revision"] + 1)
            self.assertEqual(cut["partCount"], 6)
            self.assertFalse(cut["prepared"])
            self.assertEqual(len(self.store.workspace(cut["id"], "design")["workspace"]["paths"]), 6)
        current = self.store.workspace(first, "design")
        self.assertGreater(current["cutRevision"], existing["cutRevision"])
        with self.assertRaises(WorkspaceConflict):
            self.store.save_workspace({"cutId": first, **existing}, "design", verify_arrangement)

    def test_old_order_metadata_defaults_and_validates_dates(self):
        order_id = self.order["id"]
        self.assertRegex(self.order["placedOn"], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        with self.store.connect(write=True) as db:
            data = json.loads(db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()[0])
            for field in ("placedOn", "dueOn", "notes"):
                data.pop(field)
            db.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(data), order_id))
        old = self.store.orders(order_id)
        self.assertIsNone(old["placedOn"])
        self.assertIsNone(old["dueOn"])
        self.assertEqual(old["notes"], "")
        for overrides in ({"placedOn": "2026-02-29"}, {"placedOn": "2026-2-01"},
                          {"placedOn": "2026-11-20", "dueOn": "2026-11-14"}, {"notes": "x" * 2001}):
            with self.assertRaises(ValueError):
                self.store.update_order_metadata({**old, **overrides})
        self.assertEqual(self.store.orders(order_id), old)

    def test_published_metadata_updates_without_changing_active_cut(self):
        cut_id = self.add_cut("Ткань")
        self.arrange(cut_id)
        order = self.store.publish_order(self.order, verify_arrangement)
        active = self.store.progress(cut_id, "start", "operator")
        metadata = {"number": "260559", "placedOn": "2026-11-14", "dueOn": "2026-11-19", "notes": "Сначала подкладка"}
        new = self.store.update_order_metadata({**metadata, "id": order["id"], "revision": order["revision"], "quantity": 12})
        self.assertEqual({field: new[field] for field in metadata}, metadata)
        self.assertEqual(new["quantity"], order["quantity"])
        self.assertEqual(new["revision"], order["revision"] + 1)
        self.assertEqual(new["cuts"][0]["status"], "active")
        self.assertEqual(self.store.workspace(cut_id, "operator")["workspace"], active["workspace"])
        shown, = self.store.orders(published_only=True)["orders"]
        self.assertEqual(shown["active"], 1)
        self.assertEqual(shown["materials"], [{"id": cut_id, "name": "Ткань", "widthMm": 1500,
                                               "status": "active", "prepared": True, "released": True,
                                               "stats": new["cuts"][0]["stats"]}])
        with self.assertRaises(WorkspaceConflict):
            self.store.update_order_metadata({**order, "notes": "старая вкладка"})

    def test_published_pending_cut_requires_fresh_publication_while_others_are_active(self):
        first, second, third = self.add_cut("Ткань"), self.add_cut("Подкладка"), self.add_cut("Карман")
        for cut_id in (first, second, third):
            self.arrange(cut_id)
        order = self.store.publish_order(self.order, verify_arrangement)
        active = self.store.progress(first, "start", "operator")
        before = self.store.workspace(second, "operator")
        renamed = self.store.save_cut({"action": "save-cut", "orderId": order["id"], "orderRevision": order["revision"],
                                       "id": second, "name": "Подкладочная ткань", "widthMm": 1500,
                                       "members": [self.member(self.kit)]})
        self.assertTrue(renamed["cuts"][1]["released"])
        self.assertEqual(self.store.workspace(second, "operator")["workspace"], before["workspace"])
        pending = {"action": "save-cut", "orderId": order["id"], "orderRevision": renamed["revision"],
                   "id": second, "name": "Новая ткань", "widthMm": 1300,
                   "members": [self.member(self.part)]}
        with self.assertRaises(WorkspaceConflict):
            self.store.save_cut({**pending, "id": first})
        changed = self.store.save_cut(pending)
        self.assertFalse(changed["cuts"][1]["released"])
        self.assertFalse(changed["cuts"][1]["prepared"])
        with self.assertRaises(WorkspaceConflict):
            self.store.progress(second, "start", "operator")
        with self.assertRaises(WorkspaceConflict):
            self.store.progress(second, "start", "demo")
        publication = {"id": second, "orderId": order["id"], "orderRevision": changed["revision"]}
        with self.assertRaisesRegex(ValueError, "пересекаются"):
            self.store.publish_cut(publication, verify_arrangement)
        self.arrange(second)
        self.assertFalse(self.store.orders(order["id"])["cuts"][1]["released"])
        sent = self.store.publish_cut(publication, verify_arrangement)
        self.assertTrue(sent["cuts"][1]["released"])
        self.assertGreater(self.store.workspace(second, "operator")["cutRevision"], before["cutRevision"])
        self.assertEqual(self.store.workspace(first, "operator")["workspace"], active["workspace"])
        remaining = self.store.save_cut({"action": "remove-cut", "id": third, "orderId": order["id"],
                                         "orderRevision": sent["revision"]})
        self.assertEqual([cut["id"] for cut in remaining["cuts"]], [first, second])
        self.assertEqual(self.store.progress(second, "start", "operator")["status"], "active")
