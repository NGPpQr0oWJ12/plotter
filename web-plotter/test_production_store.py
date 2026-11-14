from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from production_store import ProductionStore, verify_arrangement
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
