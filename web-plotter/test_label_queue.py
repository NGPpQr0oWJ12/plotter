import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from label_layout import defaults
from label_printer import DriverFormatError, PrinterUnavailable, SubmissionUnknown
from label_queue import LabelQueue, part_ends
from workspace import WorkspaceConflict


class LabelQueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "labels.sqlite3"
        self.bundle = {"templates": defaults(), "master": {"count": 2}, "cutRevision": 7, "parts": {
            "4": {"number": "5/1"}, "5": {"number": "5/2"}}}
        self.queue = LabelQueue(self.path, lambda *_: self.bundle)
        self.addCleanup(self.queue.close)
        self.queue.kick = lambda: None  # drive the outbox deterministically in these tests
        self.ends = [("4", "5/1", "Левая", 1, 4), ("5", "5/2", "Правая", 1, 8)]

    def begin(self, cut="material", mode="demo", first=1, ends=None):
        run = self.queue.begin(cut, mode, 7, first, self.ends if ends is None else ends)
        from label_queue import _json
        with self.queue.db() as db:
            db.execute("UPDATE runs SET bundle=? WHERE id=?", (_json(self.bundle), run))
        return run

    def test_fragments_notches_and_trim_have_only_physical_part_thresholds(self):
        source = [{"partNumber": 4}, {"notch": True}, {"partNumber": 4, "notch": True},
                  {"partNumber": 5}, {"layer": "unnumbered"}]
        for i, item in enumerate(source):
            x = i * 100
            item.update(points=[[x, 0], [x+5, 0]] if item.get("notch") else
                               [[x, 0], [x+20, 0], [x+20, 20], [x, 20]], closed=not item.get("notch"))
        pages = [{"number": 1, "paths": [{"index": i} for i in (0, 1, 3, 4)], "pathEndIndices": [3, 5, 8, 11]},
                 {"number": 2, "paths": [{"index": i} for i in (0, 2)], "pathEndIndices": [3, 5],
                  "wasteTrim": {"pathEndIndices": [7]}}]
        result = {key: (page, vertex) for key, _, _, page, vertex in part_ends(source, pages)}
        self.assertEqual(result, {"4": (2, 5), "5": (1, 8), "path:4": (1, 11)})

    def test_enable_midway_manual_idempotence_and_two_materials(self):
        run = self.begin()
        self.queue.configure("material", "demo", False)
        self.queue.observe(run, 1, 4)
        self.queue.configure("material", "demo", True)
        self.queue.observe(run, 1, 8)
        self.queue.observe(run, 1, 8)
        self.queue.observe(run, 1, 10, complete=True)
        for _ in range(2):
            self.queue.manual("material", "demo", run, ["4"], "same-request")
        self.queue._drain()
        first = self.queue.status("material", "demo")
        self.assertEqual([j["key"] for j in first["jobs"]], ["5", "master", "4"])
        self.assertEqual({j["status"] for j in first["jobs"]}, {"ready"})
        self.assertTrue(first["enabled"])
        for job in first["jobs"]:
            self.assertEqual(len(self.queue.pages("material", "demo", [job["id"]])), 1)
        with self.assertRaises(WorkspaceConflict):
            self.queue.manual("material", "demo", "wrong-run", ["4"], "another-request")
        self.queue.configure("lining", "demo", True)
        lining = self.begin("lining")
        self.queue.observe(lining, 1, 8, complete=True)
        self.assertEqual([j["key"] for j in self.queue.status("lining", "demo")["jobs"]], ["4", "5", "master"])

    def test_old_unnumbered_hole_and_outer_ring_get_one_label(self):
        from label_layout import label_context
        paths = [{"closed": True, "points": [[0, 0], [60, 0], [60, 40], [0, 40]]},
                 {"closed": True, "points": [[20, 10], [30, 10], [30, 20], [20, 20]]}]
        context = label_context({"workspace": {"paths": paths}})
        ends = part_ends(paths, [{"number": 1, "paths": [{"index": 1}, {"index": 0}], "pathEndIndices": [5, 10]}])
        self.assertEqual(context["master"]["count"], 1)
        self.assertEqual([row[0] for row in ends], list(context["parts"]))
        self.assertEqual(ends[0][-1], 10)

    def test_master_date_is_the_verified_material_completion_even_on_late_reprint(self):
        end = int(datetime(2026, 11, 26, 21, 7, 53, tzinfo=timezone.utc).timestamp() * 1000)
        self.bundle["master"]["date"] = "2026-11-01"
        self.bundle["parts"]["4"]["date"] = "11.2026"
        self.queue.configure("material", "demo", False)
        run = self.begin()
        self.queue.observe(run, 1, 8, complete=True, completed_ms=end)
        self.queue.observe(run, 1, 8, complete=True, completed_ms=end + 86_400_000)
        reopened = LabelQueue(self.path, lambda *_: self.bundle)
        self.addCleanup(reopened.close)
        reopened.kick = lambda: None
        for token in ("print-one", "print-two"):
            reopened.manual("material", "demo", run, ["4", "master"], token)
        pages = reopened.pages("material", "demo", None)
        self.assertEqual([fields["date"] for _, fields in pages],
                         ["11.2026", "27.11.2026 00:07"] * 2)

    def test_existing_queue_migrates_without_guessing_a_completion_time(self):
        self.bundle["master"]["date"] = "2026-11-01"
        run = self.begin()
        self.queue.configure("material", "demo", True)
        self.queue.observe(run, 1, 8, complete=True)
        with self.queue.db() as db:
            db.execute("ALTER TABLE runs DROP COLUMN completed_ms")
        reopened = LabelQueue(self.path, lambda *_: self.bundle)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.pages("material", "demo", None)[-1][1]["date"], "—")

    def test_partial_run_cancel_and_template_concurrency(self):
        run = self.begin(first=2, ends=[("4", "4", "Деталь", 2, 3)])
        self.queue.configure("material", "demo", True)
        self.queue.observe(run, 2, 3, complete=True)
        self.assertFalse(self.queue.status("material", "demo")["masterReady"])
        run = self.begin()
        self.queue.configure("material", "demo", False)
        self.queue.abandon(run)
        self.queue.observe(run, 1, 8, complete=True)
        self.assertFalse(self.queue.status("material", "demo")["enabled"])
        old = self.queue.templates()
        self.queue.save_template("part", old["templates"]["part"], 0)
        with self.assertRaises(WorkspaceConflict):
            self.queue.save_template("master", old["templates"]["master"], 0)
        self.assertEqual(self.queue.templates()["templates"]["master"], old["templates"]["master"])

    def test_editor_filters_retired_part_fields_from_new_and_saved_templates(self):
        from label_queue import _json
        retired = {"ticket", "patternNumber", "side", "seam", "developer"}
        initial = self.queue.templates()
        self.assertTrue(retired.isdisjoint(initial["fieldsByKind"]["part"]))
        self.assertTrue(retired.isdisjoint(item["field"] for item in initial["templates"]["part"]["elements"]))
        self.assertIn("dimensions", initial["fieldsByKind"]["part"])
        self.assertIn("name", initial["fieldsByKind"]["part"])
        for kind in ("part", "master"):
            self.assertIn("order", initial["fieldsByKind"][kind])
            self.assertEqual([e["field"] for e in initial["templates"][kind]["elements"]].count("order"), 1)
        self.assertTrue({"ticket", "productNumber"}.isdisjoint(initial["fieldsByKind"]["master"]))
        generated = {"id": "order-label", "field": "text", "text": "Заказ", "x": 1, "y": 2, "w": 12, "h": 5}
        custom = {**generated, "id": "custom", "text": "Проверено"}
        order = {"id": "order", "field": "order", "x": 16, "y": 2, "w": 20, "h": 5}
        old = {"widthMm": 58, "heightMm": 40, "elements": [generated, order, custom] + [
            {"id": f, "field": f, "x": 1, "y": 7, "w": 15, "h": 6} for f in sorted(retired)]}
        with self.queue.db() as db:
            db.execute("INSERT INTO templates VALUES(?,?)", ("part", _json(old)))
        self.assertEqual(self.queue.templates()["templates"]["part"]["elements"], [generated, order, custom])
        self.assertEqual(self.queue.save_template("part", old, 0)["templates"]["part"]["elements"], [generated, order, custom])
        old["elements"][0] = {**generated, "id": "title", "text": "Заказ №"}
        self.assertEqual(self.queue.save_template("master", old, 1)["templates"]["master"]["elements"], [old["elements"][0], order, custom])

    def test_pdf_worker_never_spools_or_resends_a_legacy_uncertain_job(self):
        run = self.begin(mode="operator")
        self.queue.observe(run, 1, 8, complete=True)
        with self.queue.db() as db:
            db.execute("UPDATE jobs SET status='submitting' WHERE key='master'")
        reopened = LabelQueue(self.path, lambda *_: self.bundle)
        self.addCleanup(reopened.close)
        with patch("label_printer.print_label") as driver:
            reopened._drain()
            driver.assert_not_called()
        self.assertEqual([j["status"] for j in reopened.status("material", "operator")["jobs"]],
                         ["ready", "ready", "unknown"])
        self.assertIn("Проверьте", reopened.status("material", "operator")["autoMessage"])
        self.assertEqual(len(reopened.pages("material", "operator", None)), 3)

    def test_auto_is_on_by_default_and_explicit_off_survives_runs_and_reopen(self):
        for mode in ("demo", "operator"):
            self.assertTrue(self.queue.status("material", mode)["enabled"])
        self.queue.configure("material", "operator", False)
        run = self.begin(mode="operator")
        self.queue.observe(run, 1, 8, complete=True)
        next_run = self.begin(mode="operator")
        self.queue.abandon(next_run)
        reopened = LabelQueue(self.path, lambda *_: self.bundle)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.status("material", "operator")["enabled"])
        self.assertFalse(reopened.status("material", "operator")["autoPending"])
        self.assertTrue(reopened.status("material", "demo")["enabled"])

    def test_manual_pdf_pages_use_current_layout_before_start_and_actual_completion_after(self):
        before = self.queue.manual_pages("material", "demo")
        self.assertEqual([item[1].get("number", "master") for item in before], ["5/1", "5/2", "master"])
        self.assertEqual(before[-1][1]["date"], "—")
        self.assertEqual(self.queue.manual_pages("material", "demo", "5"), [before[1]])
        with self.assertRaises(ValueError):
            self.queue.manual_pages("material", "demo", "missing")
        self.assertFalse(self.queue.status("material", "demo")["runId"])
        run = self.begin()
        completed_ms = int(datetime(2026, 11, 26, 14, 10, tzinfo=timezone.utc).timestamp() * 1000)
        self.queue.observe(run, 1, 8, complete=True, completed_ms=completed_ms)
        self.assertEqual(self.queue.manual_pages("material", "demo", "master")[0][1]["date"], "26.11.2026 17:10")
        self.bundle["cutRevision"] += 1
        self.assertEqual(self.queue.manual_pages("material", "demo", "master")[0][1]["date"], "—")

    def test_local_auto_retries_printer_unavailable_then_spools_parts_before_master(self):
        self.queue.local_spool = lambda: True
        run = self.begin()
        self.queue.observe(run, 1, 8, complete=True)
        with patch("label_printer.printers", return_value=["A"]), patch(
                "label_printer.print_label", side_effect=[PrinterUnavailable("Нет связи"), 5, 6, 7]) as spool:
            self.queue._drain()
            spool.assert_not_called()
            self.assertIn("Выберите принтер", self.queue.status("material", "demo")["autoMessage"])
            self.queue.select_printer("A")
            self.queue._drain()
            self.assertEqual(self.queue.status("material", "demo")["autoMessage"], "Нет связи")
            self.assertEqual(spool.call_count, 1)
            self.queue._drain()
            self.queue._drain()
            self.assertEqual([job.args[4] for job in spool.call_args_list],
                             ["Бирка №5/1", "Бирка №5/1", "Бирка №5/2", "Мастер-бирка"])
            self.assertEqual([job["status"] for job in self.queue.status("material", "demo")["jobs"]], ["submitted"] * 3)

    def test_older_ready_auto_labels_are_not_hidden_by_another_run(self):
        first = self.begin()
        self.queue.observe(first, 1, 8, complete=True)
        self.queue._drain()
        second = self.begin()
        view = self.queue.status("material", "demo")
        self.assertEqual(view["runId"], second)
        self.assertEqual(view["jobs"], [])
        self.queue.local_spool = lambda: True
        with patch("label_printer.printers", return_value=["A"]), patch("label_printer.print_label", return_value=9) as spool:
            self.queue.select_printer("A")
            self.queue._drain()
            self.assertEqual(spool.call_count, 3)

    def test_worker_freezes_master_page_at_the_verified_completion_time(self):
        from label_layout import render_label
        run = self.begin()
        end = int(datetime(2026, 11, 26, 21, 7, tzinfo=timezone.utc).timestamp() * 1000)
        self.queue.observe(run, 1, 8, complete=True, completed_ms=end)
        self.queue._drain()
        with self.queue.db(write=True) as db:
            db.execute("UPDATE runs SET completed_ms=? WHERE id=?", (end + 60_000, run))
        self.queue.local_spool = lambda: True
        with patch("label_printer.printers", return_value=["A"]), patch("label_printer.print_label", return_value=9), \
                patch("label_layout.render_label", wraps=render_label) as render:
            self.queue.select_printer("A")
            self.queue._drain()
            self.assertEqual(render.call_count, 3)
            self.assertEqual(render.call_args.args[1]["date"], "27.11.2026 00:07")

    def test_relay_keeps_station_ownership_and_replays_ack_without_spooling_twice(self):
        run = self.begin()
        self.queue.observe(run, 1, 8, complete=True)
        self.queue._drain()
        token = self.queue.relay_key("material", run)
        path = self.path.with_name("windows.sqlite3")
        fail_ack = True

        def relay(data):
            nonlocal fail_ack
            if "jobId" in data and fail_ack:
                fail_ack = False
                raise OSError("Disconnected before ack")
            return self.queue.relay_source(data["cutId"], data["relayKey"], data["station"], data.get("take"),
                                           data.get("jobId"), data.get("status"))

        def browser_helper():
            helper = LabelQueue(path, lambda *_: self.bundle, local_spool=lambda: True, relay=relay)
            self.addCleanup(helper.close)
            helper.kick = lambda: None
            return helper

        helper = browser_helper()
        helper.register_relay("material", token)
        with patch("label_printer.printers", return_value=["A"]), patch("label_printer.print_label", return_value=8) as spool:
            helper._relay_waiting()
            spool.assert_not_called()
            helper.select_printer("A")
            helper._relay_waiting()
            self.assertEqual(spool.call_count, 1)
            self.assertIsNone(self.queue.relay_source("material", token, "b" * 32, True)["job"])
            reopened = browser_helper()
            self.assertEqual(reopened.station, helper.station)
            reopened._relay_waiting()
            reopened._relay_waiting()
            self.assertEqual([job.args[4] for job in spool.call_args_list], ["Бирка №5/1", "Бирка №5/2", "Мастер-бирка"])
            self.assertEqual([j["status"] for j in self.queue.status("material", "demo")["jobs"]], ["submitted"] * 3)

    def test_pre_27_pdf_queue_never_emits_physical_jobs(self):
        run = self.begin()
        self.queue.observe(run, 1, 8, complete=True)
        with self.queue.db(write=True) as db:
            db.execute("UPDATE jobs SET auto_spool=0,status='ready',pdf=? WHERE run=?", (b"%PDF-legacy", run))
        reopened = LabelQueue(self.path, lambda *_: self.bundle, local_spool=lambda: True)
        self.addCleanup(reopened.close)
        reopened.kick = lambda: None
        with patch("label_printer.printers", return_value=["A"]), patch("label_printer.print_label") as spool:
            reopened.select_printer("A")
            reopened._drain()
            spool.assert_not_called()
        self.assertEqual([j["status"] for j in reopened.status("material", "demo")["jobs"]], ["legacy"] * 3)

    def test_station_printer_accepts_an_installed_network_queue_and_persists(self):
        network, local = r"\\station\labels", "USB label printer"
        with patch("label_printer.printers", return_value=[local, network]):
            self.assertEqual(self.queue.printer_settings(), {"printers": [local, network], "preferred": ""})
            with self.assertRaises(ValueError):
                self.queue.select_printer(r"\\different\uninstalled")
            self.assertEqual(self.queue.select_printer(network)["preferred"], network)
            reopened = LabelQueue(self.path, lambda *_: self.bundle)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.printer_settings()["preferred"], network)
        with patch("label_printer.printers", return_value=[local]):
            self.assertEqual(reopened.printer_settings(), {"printers": [local], "preferred": network})

    def test_explicit_part_print_uses_the_current_selected_printer_and_renders_once(self):
        loader = self.queue.load_bundle = Mock(return_value=self.bundle)
        self.bundle["parts"]["4"]["order"] = "DEMO-051"
        with patch("label_printer.printers", return_value=["A", "B"]), \
                patch("label_printer.print_label", return_value=81) as driver, \
                patch("label_layout.render_label", return_value=(b"bitmap", [])) as render:
            self.queue.select_printer("A")
            with self.assertRaises(WorkspaceConflict):
                self.queue.print_part("material", "demo", 7, "4", "B", "part-label-82")
            sent = self.queue.print_part("material", "demo", 7, "4", "A", "part-label-81")
            self.assertEqual((sent["status"], sent["spoolId"]), ("submitted", 81))
            loader.assert_called_once_with("material", "demo", 7)
            render.assert_called_once_with(self.bundle["templates"]["part"], self.bundle["parts"]["4"])
            driver.assert_called_once_with("A", b"bitmap", 58, 40, "Бирка №5/1 · заказ DEMO-051")
            self.queue.select_printer("B")
            self.assertEqual(self.queue.print_part("material", "demo", 7, "4", "A", "part-label-81"), sent)
            with self.assertRaises(WorkspaceConflict):
                self.queue.print_part("material", "demo", 7, "4", "B", "part-label-81")
            self.assertEqual(driver.call_count, 1)
        self.assertIsNone(self.queue.status("material", "demo")["runId"])

    def test_duplicate_in_flight_and_unknown_after_restart_never_submit_twice(self):
        sending, release = Event(), Event()

        def interrupted(*_):
            sending.set()
            if not release.wait(3):
                raise AssertionError("Test did not release the print call")
            raise SubmissionUnknown("Lost Win32 acknowledgement")

        with patch("label_printer.printers", return_value=["A"]), patch("label_printer.print_label", side_effect=interrupted) as driver:
            self.queue.select_printer("A")
            args = ("material", "operator", 7, "4", "A", "single-label-1")
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.queue.print_part, *args)
                try:
                    self.assertTrue(sending.wait(3))
                    self.assertEqual(self.queue.print_part(*args)["status"], "submitting")
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3)["status"], "unknown")
            with self.queue.db(write=True) as db:
                db.execute("UPDATE direct_label_prints SET status='submitting' WHERE request_id=?", (args[-1],))
            reopened = LabelQueue(self.path, lambda *_: self.bundle)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.print_part(*args)["status"], "unknown")
            self.assertEqual(driver.call_count, 1)

    def test_rejected_driver_format_is_terminal_for_the_original_request(self):
        with patch("label_printer.printers", return_value=["A"]), \
                patch("label_printer.print_label", side_effect=DriverFormatError("Другой формат")) as driver:
            self.queue.select_printer("A")
            args = ("material", "operator", 7, "4", "A", "one-label-failure")
            first = self.queue.print_part(*args)
            self.assertEqual((first["status"], first["message"]), ("failed", "Другой формат"))
            self.assertEqual(self.queue.print_part(*args), first)
            self.assertEqual(driver.call_count, 1)
