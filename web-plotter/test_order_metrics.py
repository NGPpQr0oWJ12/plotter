from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from order_metrics import cut_metrics, plan_snapshot, summarize_cuts
from production_store import ProductionStore, verify_arrangement
from workspace import WorkspaceConflict


START = "2026-11-25T10:30:00.000Z"
RESTART = "2026-11-25T10:31:00.000Z"
FINISH = "2026-11-25T10:31:32.500Z"


class OrderMetricsTests(unittest.TestCase):
    def test_order_span_fabric_time_and_area_weighted_utilization(self):
        def record(stats, start, finish):
            return {"status": "done", "stats": cut_metrics(plan_snapshot(stats, "saved"), "done", start, finish)}

        first = record({"lengthMm": 10, "areaMm2": 100, "partsAreaMm2": 60, "utilizationPercent": 60,
                        "estimatedWorkSeconds": 20}, START, "2026-11-25T10:30:40.000Z")
        second = record({"lengthMm": 30, "areaMm2": 300, "partsAreaMm2": 60, "utilizationPercent": 20,
                         "estimatedWorkSeconds": 25}, RESTART, FINISH)
        stats = summarize_cuts([first, second])
        self.assertEqual((stats["lengthMm"], stats["utilizationPercent"]), (40, 30))
        self.assertEqual((stats["timedCount"], stats["fabricActualSeconds"], stats["actualSeconds"], stats["deltaSeconds"]),
                         (2, 72.5, 92.5, 47.5))
        second["stats"]["utilizationPercent"] = None
        self.assertIsNone(summarize_cuts([first, second])["utilizationPercent"])

    def test_partial_history_has_no_inferred_start_or_total_duration(self):
        known = {"status": "done", "stats": cut_metrics(None, "done", RESTART, FINISH)}
        unknown = {"status": "done", "stats": cut_metrics(None, "done", None, FINISH)}
        stats = summarize_cuts([known, unknown])
        self.assertIsNone(stats["startedAt"])
        self.assertEqual(stats["finishedAt"], FINISH)
        self.assertIsNone(stats["actualSeconds"])
        self.assertEqual((stats["timedCount"], stats["fabricActualSeconds"]), (1, 32.5))
        live = {"status": "active", "stats": cut_metrics(None, "active", START, None)}
        pending = {"status": "pending"}
        stats = summarize_cuts([live, pending])
        self.assertEqual(stats["startedAt"], START)
        self.assertIsNone(stats["finishedAt"])


class ProductionStatisticsTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store = ProductionStore(Path(root.name) / "production.sqlite3")
        self.calls = []

    def verify(self, workspace, width):
        verify_arrangement(workspace, width)
        self.calls.append(width)
        length = max(x for path in workspace["paths"] for x, _y in path["points"])
        return {"lengthMm": length, "areaMm2": length * width, "partsAreaMm2": 1200,
                "utilizationPercent": 120_000 / (length * width), "estimatedWorkSeconds": 20}

    def create_cut(self, *, metrics=True):
        if metrics:
            self.store.configure_metrics(self.verify)
        order = self.store.save_order({"number": "Статистика"})
        part = self.store.save_registry({"kind": "part", "name": "Сиденье", "entities": [{
            "id": "outline", "type": "polyline", "layer": "0", "closed": True,
            "points": [[0, 0], [40, 0], [40, 30], [0, 30]],
        }]})
        order = self.store.save_cut({"orderId": order["id"], "orderRevision": order["revision"],
                                     "name": "Ткань", "widthMm": 100, "members": [{"id": part["id"], "quantity": 1}]})
        self.order_id = order["id"]
        self.cut_id = order["cutId"]
        record = self.store.workspace(self.cut_id, "design")
        for path in record["workspace"]["paths"]:
            path["points"] = [[x + 40, y + 30] for x, y in path["points"]]
        verify = self.verify if metrics else verify_arrangement
        self.store.save_workspace({"cutId": self.cut_id, **record}, "design", verify)
        self.store.publish_order(order, verify)

    def material_stats(self):
        return self.store.orders(self.order_id)["cuts"][0]["stats"]

    def test_live_clocks_are_idempotent_and_release_clears_attempt(self):
        self.create_cut()
        self.assertEqual(len(self.calls), 2)
        with patch("production_store.utc_now", side_effect=[START, RESTART, FINISH]) as clock:
            self.store.progress(self.cut_id, "start", "demo")
            self.assertIsNone(self.material_stats()["startedAt"])
            opened = self.store.progress(self.cut_id, "start", "operator")
            self.store.progress(self.cut_id, "start", "operator")
            self.assertEqual(self.material_stats()["startedAt"], START)
            self.store.release_cut(self.cut_id, opened["revision"])
            self.assertIsNone(self.material_stats()["startedAt"])
            reopened = self.store.progress(self.cut_id, "start", "operator")
            with self.assertRaises(WorkspaceConflict):
                self.store.progress(self.cut_id, "finish", "operator", reopened["revision"] + 1)
            self.assertIsNone(self.material_stats()["finishedAt"])
            self.store.progress(self.cut_id, "finish", "operator", reopened["revision"])
            self.store.progress(self.cut_id, "finish", "operator", reopened["revision"])
        self.assertEqual(clock.call_count, 3)
        stats = self.material_stats()
        summary, = self.store.orders(published_only=True)["orders"]
        self.assertEqual(summary["materials"][0]["stats"], stats)
        self.assertEqual(summary["stats"]["actualSeconds"], 32.5)
        self.assertEqual((stats["startedAt"], stats["finishedAt"], stats["deltaSeconds"]), (RESTART, FINISH, 12.5))

    def test_geometry_snapshots_survive_reads_and_clear_on_invalid_operator_layout(self):
        self.create_cut()
        planned = self.material_stats()
        self.assertEqual(planned["source"], "design")
        calls = len(self.calls)
        self.store.orders()
        self.store.backfill_metrics()
        self.assertEqual(len(self.calls), calls)
        opened = self.store.progress(self.cut_id, "start", "operator")
        for path in opened["workspace"]["paths"]:
            path["points"] = [[x + 20, y] for x, y in path["points"]]
        layout = self.store.save_workspace({"cutId": self.cut_id, **opened}, "operator", self.verify)
        current = self.material_stats()
        self.assertEqual((current["source"], current["lengthMm"]), ("operator", planned["lengthMm"] + 20))
        calls = len(self.calls)
        layout["workspace"]["view"]["zoom"] = 2
        layout = self.store.save_workspace({"cutId": self.cut_id, **layout}, "operator", self.verify)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.material_stats(), current)
        for path in layout["workspace"]["paths"]:
            path["points"] = [[x, y + 200] for x, y in path["points"]]
        self.store.save_workspace({"cutId": self.cut_id, **layout}, "operator", self.verify)
        invalid = self.material_stats()
        self.assertIsNone(invalid["lengthMm"])
        self.assertEqual(invalid["startedAt"], current["startedAt"])

    def test_backfill_restores_saved_geometry_without_fabricating_historical_clocks(self):
        self.store.configure_metrics(self.verify)
        self.store.backfill_metrics()
        self.assertFalse(self.store.path.exists())
        self.store.configure_metrics(None)
        self.create_cut(metrics=False)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE cuts SET status='active' WHERE id=?", (self.cut_id,))
        self.store.configure_metrics(self.verify)
        self.store.backfill_metrics()
        self.store.backfill_metrics()
        self.assertEqual(len(self.calls), 1)
        info = self.material_stats()
        self.assertEqual(info["source"], "saved")
        self.assertIsNone(info["startedAt"])
        record = self.store.progress(self.cut_id, "start", "operator")
        with patch("production_store.utc_now", return_value=FINISH):
            self.store.progress(self.cut_id, "finish", "operator", record["revision"])
        info = self.material_stats()
        self.assertEqual(info["finishedAt"], FINISH)
        self.assertIsNone(info["startedAt"])
        self.assertIsNone(info["actualSeconds"])
