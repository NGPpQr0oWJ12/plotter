import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from production_store import ProductionStore, RecordNotFound, verify_arrangement
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

    def test_registry_tree_and_conflicting_writes(self):
        with self.assertRaises(ValueError):
            self.store.save_registry({**self.model, "parent": self.folder["id"]})
        self.store.save_registry({**self.model, "name": "Диван с подушками"})
        with self.assertRaises(WorkspaceConflict):
            self.store.save_registry({**self.model, "name": "Старая вкладка"})

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
                                               "status": "active", "prepared": True, "released": True}])
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
