import io
import json
import re
import unittest
from contextlib import closing
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import Mock, patch

import ezdxf

from controller import ControllerError
from cut_journal import CutJournal
from server import PlotterServer


def drawing():
    return {
        "source": {"filename": "Лекало.dxf", "warnings": []},
        "paths": [
            {"layer": "Разметка", "closed": False, "points": [[20, 30], [30, 40]]},
            {"layer": "Крой", "closed": True,
             "points": [[40, 50], [60, 50], [60, 70], [40, 50], [40, 50]]},
            {"layer": "Исключён", "closed": False, "points": [[100, 120], [150, 170]]},
        ],
        "excluded": [2],
        "view": {"zoom": 1, "focusX": 0, "focusY": 0, "grid": True},
        "optimization": {"route": True, "join": True},
    }


class ServerFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = Mock()
        self.controller.upload_page_add.return_value = 0.12
        self.controller.upload_job.side_effect = self.acknowledge_transfer
        self.controller.operate.return_value = {"sent": True}
        self.controller.actuator_snapshot.return_value = {
            "epoch": "test-epoch", "revision": 0, "clampLowered": None, "vacuumEnabled": None,
        }
        self.controller.upload_uncertain = False
        self.controller.upload_in_progress = False
        with patch("server.Controller", return_value=self.controller):
            self.server = PlotterServer(0, Path(self.directory.name) / "workspace.json")
        self.worker = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()

    @staticmethod
    def acknowledge_transfer(job, finish, page_add, **kwargs):
        return {"uploaded": True, "phase": "confirmed", "commitSent": True,
                "points": job.point_count, "jobReceipt": "fake-receipt", "baselineEvent": {"code": 5}}

    def request(self, path, data, expected=HTTPStatus.OK):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("POST", path, json.dumps(data).encode("utf-8"), {"Content-Type": "application/json"})
            response = client.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(response.status, expected, payload)
        return payload

    def save(self, workspace, revision=0):
        return self.request("/api/workspace", {"revision": revision, "workspace": workspace})["revision"]

    def test_close_contours_leaves_the_saved_draft_and_exclusions_intact(self):
        def contour(points):
            return {"layer": "Крой", "closed": False, "points": points, "partNumber": 1}

        paths = [contour([[0, 0], [20, 0]]), contour([[10, 3], [10, 8]]),
                 contour([[20, -5], [20, 0], [20, 10], [0, 10], [0, 0]])]
        workspace = {**drawing(), "paths": paths, "excluded": [1]}
        self.save(workspace)

        repaired = self.request("/api/close-contours", {"paths": paths, "excluded": [1]})

        self.assertEqual(repaired["closedCount"], 1)
        self.assertEqual(repaired["sources"], [[1], [2], [0, 2]])
        self.assertEqual(repaired["excluded"], [0])
        self.assertEqual(repaired["paths"][0], paths[1])
        self.assertTrue(repaired["paths"][-1]["closed"])
        self.assertEqual(self.server.workspace.read()["workspace"], workspace)
        self.controller.operate.assert_not_called()

    def test_close_contours_bridges_a_small_gap_only_on_explicit_repair(self):
        outline = {"layer": "Крой", "closed": False, "partNumber": 4,
                   "points": [[10, 10], [110, 10], [110, 110], [10, 110], [10, 10.01153]]}
        notch = {"layer": "Крой", "closed": False, "partNumber": 4, "points": [[10, 10], [5, 10]]}
        workspace = {**drawing(), "paths": [outline, notch], "excluded": []}
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual(plan["repairableContourCount"], 1)
        self.assertEqual(plan["material"]["openContourCount"], 1)

        repaired = self.request("/api/close-contours", {"paths": workspace["paths"]})

        self.assertEqual(repaired["closedCount"], 1)
        self.assertEqual(repaired["paths"], [{**outline, "closed": True}, notch])
        self.assertEqual(self.server.workspace.read()["workspace"], workspace)
        fixed_plan = self.preview({**workspace, "paths": repaired["paths"]})
        self.assertEqual(fixed_plan["repairableContourCount"], 0)
        self.assertEqual(fixed_plan["material"]["partsAreaMm2"], 10_000)
        self.assertAlmostEqual(fixed_plan["cutDistanceMm"] - plan["cutDistanceMm"], 0.01153)

    def preview(self, workspace):
        paths = [path for index, path in enumerate(workspace["paths"]) if index not in workspace["excluded"]]
        optimize = workspace["optimization"].get("routeApplied", True)
        return self.request("/api/cut-plan", {"job": {"paths": paths, "optimize": optimize},
                                              "filename": workspace["source"]["filename"]})

    def upload(self, plan, expected=HTTPStatus.OK, **options):
        return self.request("/api/controller-upload", {
            "controlToken": "control", "planId": plan["planId"], "tool": 7, **options,
        }, expected)

    def start(self, plan, upload, expected=HTTPStatus.OK, *, verified=True):
        return self.request("/api/controller-operation", {
            "action": "cut", "controlToken": "control", "planId": plan["planId"],
            "jobReceipt": upload["jobReceipt"], "jobVerified": verified,
        }, expected)

    def roll_plan(self, *, start_points=(20, 2600, 5100), route_applied=True):
        workspace = drawing()
        workspace["paths"] = [
            {"layer": "Крой", "closed": False, "partNumber": index,
             "points": [[x, 50], [x + 10, 70]]}
            for index, x in enumerate(start_points, 1)
        ]
        workspace["excluded"] = []
        workspace["optimization"]["routeApplied"] = route_applied
        revision = self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual((plan["mode"], len(plan["pages"]), plan["seams"], plan["canUpload"]),
                         ("roll", len(start_points), [], True))
        return workspace, revision, plan

    def report_live(self, upload, event, state, *, index=0, **run_fields):
        report = {"responded": True, "busy": False, "position": {"x": 0, "y": 0, "z": 0},
                  "lastEvent": {"code": event}, "currentIndex": index, "indexLimit": upload["points"],
                  "run": {"state": state, "fresh": True, "index": index, "indexLimit": upload["points"],
                          **run_fields}}
        self.controller.read_live.return_value = report
        return report

    def test_status_reports_material_control_capabilities(self):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("GET", "/api/status")
            response = client.getresponse()
            status = json.loads(response.read())
        self.assertEqual(response.status, HTTPStatus.OK)
        self.assertEqual(status["apiVersion"], 19)
        self.assertIs(status["machine"]["liveCutProgress"], True)
        self.assertIs(status["nestingKeepsNotches"], True)
        self.assertIs(status["nestingPartRotation"], True)
        self.assertIs(status["pageRouteOptimization"], True)
        self.assertIs(status["prepareXiao"], True)
        self.assertIs(status["machine"]["directRoll"], True)
        self.assertEqual(status["machine"]["rollPassLengthMm"], 2450)
        self.assertIs(status["machine"]["continuousRoll"], True)
        self.assertIs(status["machine"]["coordinateMove"], True)
        self.assertIs(status["machine"]["stopDuringExternalRun"], True)
        self.assertIs(status["machine"]["independentVacuumControl"], True)
        self.assertIs(status["machine"]["clampControl"], True)
        self.assertIsNot(status["machine"].get("independentClampControl"), True)
        self.assertEqual(status["machine"]["actuators"], self.controller.actuator_snapshot.return_value)
        self.controller.read_live.assert_not_called()
        self.controller.operate.assert_not_called()

    def test_coordinate_move_passes_target_and_control_session_to_controller(self):
        move = {"state": "moving", "x": 100.25, "y": 42}
        self.controller.operate.return_value = {"sent": True, "coordinateMove": move}
        result = self.request("/api/controller-operation", {"action": "move-to", "controlToken": "control",
                                                            "x": move["x"], "y": move["y"]})
        self.assertEqual(result["coordinateMove"], move)
        self.controller.operate.assert_called_once_with("move-to", None, None, None, None, "control",
                                                        job_receipt=None, job_verified=None, x=100.25, y=42)

    def test_coordinate_move_and_upload_cannot_invalidate_each_others_state(self):
        with self.server.upload_lock:
            self.request("/api/controller-operation", {"action": "move-to", "controlToken": "control", "x": 0, "y": 0},
                         HTTPStatus.CONFLICT)
        self.controller.operate.assert_not_called()

        loaded = ("test-receipt", "test-plan", 1)
        self.server.loaded_plan = loaded
        self.controller.require_coordinate_move_idle.side_effect = ControllerError("Перемещение ещё выполняется.")
        self.request("/api/controller-upload", {"controlToken": "control"}, HTTPStatus.BAD_REQUEST)
        self.controller.upload_job.assert_not_called()
        self.assertEqual(self.server.loaded_plan, loaded)

    def test_single_page_preview_matches_the_sp4_wire_geometry(self):
        workspace = drawing()
        workspace["paths"][0]["points"].reverse()
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertTrue(plan["canUpload"])
        self.assertEqual((plan["mode"], plan["tool"], plan["issues"]), ("single", 4, []))
        self.assertEqual(self.upload(plan)["planId"], plan["planId"])
        encoded, finish, _ = self.controller.upload_job.call_args.args
        vertices = []
        for block in encoded.chunks:
            ascii_data = block.payload.split(b"<", 1)[0].decode("ascii")
            vertices.extend(re.findall(r"\(([01]),(\d),(\d+\.\d{3}),(\d+\.\d{3}),1\.00\)", ascii_data))

        page, = plan["pages"]
        self.assertEqual([path["layer"] for path in page["paths"]], ["Разметка", "Крой"])
        self.assertEqual(page["paths"][0]["points"], [[20, 30], [30, 40]])
        expected = []
        for path in page["paths"]:
            route = path["points"] + [path["points"][0]] if path["closed"] else path["points"]
            expected.extend(("0" if index == 0 else "1", "4", f"{x + 0.01:.3f}", f"{y + 0.01:.3f}")
                            for index, (x, y) in enumerate(route))
        self.assertEqual(vertices, expected)
        self.assertEqual(encoded.point_count, len(expected))
        self.assertEqual(finish.packet, b"end60120)\0")

    def test_changing_applied_route_requires_new_upload_but_checkbox_alone_does_not(self):
        workspace = drawing()
        revision = self.save(workspace)
        plan = self.preview(workspace)
        receipt = self.upload(plan)
        workspace["optimization"]["route"] = False
        revision = self.save(workspace, revision)
        self.assertEqual(self.preview(workspace)["planId"], plan["planId"])
        workspace["optimization"]["routeApplied"] = False
        self.save(workspace, revision)
        updated = self.preview(workspace)
        self.assertNotEqual(updated["planId"], plan["planId"])
        self.start(plan, receipt, HTTPStatus.CONFLICT)
        self.upload(plan, HTTPStatus.CONFLICT)
        self.assertEqual(self.upload(updated)["planId"], updated["planId"])

    def test_view_only_save_keeps_start_grant_but_geometry_edit_revokes_it(self):
        workspace = drawing()
        revision = self.save(workspace)
        plan = self.preview(workspace)
        receipt = self.upload(plan)
        workspace["view"].update(zoom=2, grid=False)
        workspace["optimization"]["join"] = False
        revision = self.save(workspace, revision)
        self.assertEqual(self.preview(workspace)["planId"], plan["planId"])
        self.start(plan, receipt, HTTPStatus.BAD_REQUEST, verified=False)
        self.controller.operate.assert_not_called()
        self.assertTrue(self.start(plan, receipt)["sent"])
        self.controller.operate.assert_called_once()
        self.assertTrue(self.controller.operate.call_args.kwargs["job_verified"])
        self.controller.operate.reset_mock()

        workspace["paths"][0]["points"][0][0] += 1
        revision = self.save(workspace, revision)
        updated = self.preview(workspace)
        self.assertNotEqual(updated["planId"], plan["planId"])
        self.start(plan, receipt, HTTPStatus.CONFLICT)
        self.start(updated, receipt, HTTPStatus.CONFLICT)
        workspace["paths"][0]["points"][0][0] -= 1
        self.save(workspace, revision)
        self.start(plan, receipt, HTTPStatus.CONFLICT)
        self.controller.operate.assert_not_called()

    def test_stale_preview_cannot_upload_a_different_saved_drawing(self):
        workspace = drawing()
        revision = self.save(workspace)
        plan = self.preview(workspace)
        workspace["excluded"] = [0, 2]
        self.save(workspace, revision)
        self.upload(plan, HTTPStatus.CONFLICT)
        self.controller.upload_job.assert_not_called()

    def test_roll_transfers_a_long_outline_as_two_open_sections_joined_at_local_zero(self):
        workspace = drawing()
        width = self.server.simulator.width
        outline = {**workspace["paths"][1], "partNumber": 76,
                   "points": [[40, 50], [width + 100, 50], [width + 100, 70], [40, 70]]}
        workspace["paths"] = [outline]
        workspace["excluded"] = []
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual((plan["mode"], plan["issues"], plan["canUpload"], plan["hasContinuations"]),
                         ("roll", [], True, True))
        first_page, second_page = plan["pages"]
        boundary = first_page["endX"]
        self.assertTrue(first_page["canStart"])
        self.assertFalse(second_page["canStart"])
        self.assertEqual(first_page["outgoingSeams"], second_page["incomingSeams"])
        self.assertEqual(first_page["outgoingSeams"], plan["seams"])
        self.assertEqual({(seam["x"], seam["y"]) for seam in plan["seams"]}, {(boundary, 50), (boundary, 70)})

        self.controller.auto_run_readback = None
        self.upload(plan, HTTPStatus.BAD_REQUEST, autoRunOff=True)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, autoRunOff=True, advanceConfirmed=True)
        self.controller.upload_job.assert_not_called()
        first = self.upload(plan, autoRunOff=True, advanceConfirmed=True)
        self.report_live(first, 8, "ready")
        self.upload(plan, HTTPStatus.BAD_REQUEST, autoRunOff=True)  # A ready-section retry still requires alignment.
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 11}}
        self.start(plan, first)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, autoRunOff=True, advanceConfirmed=True)

        self.report_live(first, 4, "completed", index=first["points"], startedAt=11, endedAt=12, completedPages=1)
        self.assertEqual(self.request("/api/controller-live", {})["roll"]["nextPage"], 2)
        self.upload(plan, HTTPStatus.BAD_REQUEST, page=2, autoRunOff=True)
        self.report_live(first, 50, "feeding")
        self.upload(plan, HTTPStatus.CONFLICT, page=2, autoRunOff=True, advanceConfirmed=True)
        live = self.report_live(first, 5, "completed", startedAt=11, endedAt=12, completedPages=1)
        # Releasing the material can reset the controller's last job index after observed completion.
        live["indexLimit"] = 0
        live["run"].update(index=None, indexLimit=None)
        second = self.upload(plan, page=2, autoRunOff=True, advanceConfirmed=True)
        self.assertEqual((second["roll"]["completedPages"], second["roll"]["page"]), (1, 2))

        def vertices(call):
            job, finalization, _ = call.args
            self.assertEqual(finalization.acknowledgement, b"endok")
            body = b"".join(chunk.payload.split(b"<", 1)[0] for chunk in job.chunks)
            return [(int(move), float(x), float(y)) for move, x, y in
                    re.findall(rb"\(([01]),4,([0-9.]+),([0-9.]+),1\.00\)", body)]

        # Each section starts raised (0), then follows only original contour segments (1).
        # No additional line along the boundary closes either section's contour.
        before, after = map(vertices, self.controller.upload_job.call_args_list)
        end_x = round(boundary + 0.01, 3)
        remainder_x = round(width + 100 - boundary + 0.01, 3)
        self.assertEqual(before, [(0, end_x, 70.01), (1, 40.01, 70.01),
                                  (1, 40.01, 50.01), (1, end_x, 50.01)])
        self.assertEqual(after, [(0, 0.01, 50.01), (1, remainder_x, 50.01),
                                 (1, remainder_x, 70.01), (1, 0.01, 70.01)])
        self.report_live(second, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 21}}
        self.start(plan, second)
        self.report_live(second, 4, "completed", index=second["points"], startedAt=21, endedAt=22, completedPages=1)
        self.assertEqual(self.request("/api/controller-live", {})["roll"]["state"], "completed")
        self.request("/api/roll-reset", {"planId": plan["planId"], "controlToken": "control", "stopped": True})
        self.upload(plan, HTTPStatus.CONFLICT, page=2, autoRunOff=True, advanceConfirmed=True)
        self.assertEqual(self.controller.upload_job.call_count, 2)

    def test_roll_prevalidates_a_later_section_before_transferring_the_first(self):
        workspace = drawing()
        width = self.server.simulator.width
        workspace["paths"] = [
            {"layer": "Крой", "closed": False, "points": [[10, 30], [width + 100, 30]]},
            {"layer": "Крой", "closed": False, "points": [[width + 100, 50], [width + 100, 50.0001]]},
        ]
        workspace["excluded"] = []
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertFalse(plan["canUpload"])
        self.assertTrue(plan["canPrepareXiao"])
        self.assertIn("Проход 2 нельзя загрузить", plan["uploadIssue"])
        self.assertIn("вырождается после округления", plan["uploadIssue"])
        self.upload(plan, HTTPStatus.BAD_REQUEST, advanceConfirmed=True)
        self.controller.upload_job.assert_not_called()

    def test_roll_upload_advances_one_verified_page_at_a_time(self):
        _, _, plan = self.roll_plan()

        self.controller.auto_run_readback = None
        self.upload(plan, HTTPStatus.BAD_REQUEST)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, autoRunOff=True)
        first = self.upload(plan, page=1, autoRunOff=True)
        self.assertEqual((first["roll"]["state"], first["roll"]["nextPage"]), ("ready", 1))
        self.assertTrue(self.controller.upload_job.call_args.kwargs["auto_run_off"])
        self.report_live(first, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 11}}
        self.assertEqual(self.start(plan, first)["roll"]["state"], "running")
        self.start(plan, first, HTTPStatus.CONFLICT)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, advanceConfirmed=True, autoRunOff=True)

        def finish(started_at, upload, *, event=4):
            self.controller.read_live.return_value = {
                "responded": True, "busy": False, "position": {"x": 0, "y": 0, "z": 0},
                "lastEvent": {"code": event},
                "run": {"state": "completed", "startedAt": started_at, "endedAt": started_at + 1,
                        "fresh": True, "completedPages": 1, "index": 0, "indexLimit": upload["points"]},
            }
            return self.request("/api/controller-live", {})["roll"]

        self.assertEqual(finish(10, first)["state"], "running")
        self.assertEqual(finish(11, first, event=50)["state"], "running")
        after_first = finish(11, first)
        self.assertEqual((after_first["state"], after_first["completedPages"], after_first["nextPage"]),
                         ("advance", 1, 2))
        self.assertEqual(after_first["feedAfterMm"], plan["pages"][0]["feedAfterMm"])
        self.upload(plan, HTTPStatus.BAD_REQUEST, page=2, autoRunOff=True)
        self.upload(plan, HTTPStatus.CONFLICT, page=3, advanceConfirmed=True, autoRunOff=True)
        finish(11, first, event=50)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, advanceConfirmed=True, autoRunOff=True)
        finish(11, first, event=11)
        second = self.upload(plan, page=2, advanceConfirmed=True, autoRunOff=True)
        self.assertEqual((second["roll"]["state"], second["roll"]["completedPages"]), ("ready", 1))
        encoded, _, _ = self.controller.upload_job.call_args.args
        x = plan["pages"][1]["paths"][0]["points"][0][0] - plan["pages"][1]["startX"]
        self.assertIn(f"(0,4,{x + 0.01:.3f},50.010,1.00)".encode(), encoded.chunks[0].payload)
        self.report_live(second, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 21}}
        self.start(plan, second)
        self.assertEqual(finish(21, second)["nextPage"], 3)

        third = self.upload(plan, page=3, advanceConfirmed=True, autoRunOff=True)
        self.report_live(third, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 31}}
        self.start(plan, third)
        complete = finish(31, third)
        self.assertEqual((complete["state"], complete["completedPages"], complete["nextPage"]),
                         ("completed", 3, None))
        self.upload(plan, HTTPStatus.CONFLICT, page=1, autoRunOff=True)
        actions = [call.args[0] for call in self.controller.operate.call_args_list]
        self.assertEqual(actions, ["cut", "cut", "cut"])
        self.assertEqual(self.controller.upload_job.call_count, 3)
        reset = {"planId": plan["planId"], "controlToken": "control", "stopped": False}
        self.request("/api/roll-reset", reset, HTTPStatus.BAD_REQUEST)
        self.assertIsNone(self.request("/api/roll-reset", {**reset, "stopped": True})["roll"])

    def test_live_cut_progress_tracks_verified_points_and_retains_final_journal(self):
        workspace = drawing()
        workspace["paths"] = [
            {"layer": "Крой", "closed": False, "points": [[x, 50], [x + 10, 70]]}
            for x in (20, 60, 2600)
        ]
        workspace["excluded"] = []
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual([page["pathEndIndices"] for page in plan["pages"]], [[2, 4], [2]])
        first = self.upload(plan)
        self.assertEqual((first["cutProgress"]["state"], first["cutProgress"]["page"]), ("ready", 1))
        self.report_live(first, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 11}}
        self.start(plan, first)

        self.report_live(first, 3, "running", startedAt=10, completedIndex=4)
        self.assertEqual(self.request("/api/controller-live", {})["cutProgress"]["index"], 0)
        self.report_live(first, 3, "running", startedAt=11, startedReceipt="other", completedIndex=4)
        live = self.request("/api/controller-live", {})
        self.assertEqual(live["cutProgress"]["index"], 0)
        self.assertNotIn("drawingPosition", live)
        self.report_live(first, 3, "running", startedAt=11, completedIndex=2)
        progress = self.request("/api/controller-live", {})["cutProgress"]
        self.assertEqual((progress["index"], progress["indexLimit"], progress["completedPages"]), (2, 4, 0))
        self.report_live(first, 3, "running", startedAt=11, completedIndex=0)
        self.assertEqual(self.request("/api/controller-live", {})["cutProgress"]["index"], 2)

        self.report_live(first, 4, "completed", startedAt=11, endedAt=12, completedPages=1)
        progress = self.request("/api/controller-live", {})["cutProgress"]
        self.assertEqual((progress["state"], progress["page"], progress["index"]), ("advance", 1, 4))
        second = self.upload(plan, page=2, advanceConfirmed=True)
        self.assertEqual((second["cutProgress"]["index"], second["cutProgress"]["completedPages"]), (0, 1))
        self.assertEqual(second["cutProgress"]["events"][-2]["kind"], "page_completed")
        self.report_live(second, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 21}}
        self.start(plan, second)
        report = self.report_live(second, 3, "running", startedAt=21)
        report["position"] = {"x": 40.01, "y": 50.01, "z": 0}
        live = self.request("/api/controller-live", {})
        point = live["drawingPosition"]
        self.assertEqual((point["planId"], point["page"]), (plan["planId"], 2))
        self.assertAlmostEqual(point["x"], plan["pages"][1]["startX"] + 40)
        self.assertAlmostEqual(point["y"], 50)
        self.assertEqual(live["position"], report["position"])
        self.report_live(second, 3, "running", startedAt=21)["busy"] = True
        self.assertNotIn("drawingPosition", self.request("/api/controller-live", {}))
        self.report_live(second, 4, "completed", startedAt=21, endedAt=22, completedPages=1)
        live = self.request("/api/controller-live", {})
        self.assertNotIn("drawingPosition", live)
        self.assertEqual((live["cutProgress"]["state"], live["cutProgress"]["completedPages"]), ("completed", 2))
        self.assertEqual(live["cutJournal"][-1]["kind"], "cut_completed")
        self.assertEqual(sum(entry["kind"] == "cut_completed" for entry in live["cutJournal"]), 1)
        self.assertEqual(CutJournal(self.server.cut_journal.path).snapshot(), live["cutJournal"])
        reset = self.request("/api/roll-reset", {"planId": plan["planId"], "controlToken": "control", "stopped": True})
        self.assertIsNone(reset["cutProgress"])
        self.assertEqual(reset["cutJournal"], live["cutJournal"])

    def test_single_cut_progress_is_reported_without_roll_session(self):
        workspace = drawing()
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual(plan["pages"][0]["pathEndIndices"], [2, 6])
        upload = self.upload(plan)
        self.assertIsNone(upload["roll"])
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 11}}
        self.start(plan, upload)
        self.report_live(upload, 4, "completed", startedAt=11, endedAt=12, completedPages=1)
        live = self.request("/api/controller-live", {})
        self.assertEqual(live["cutProgress"]["state"], "completed")
        self.assertEqual(live["cutJournal"][-1]["kind"], "cut_completed")

    def test_roll_can_start_at_selected_page_and_only_advance_from_there(self):
        _, _, plan = self.roll_plan()
        self.upload(plan, HTTPStatus.CONFLICT, page=2)
        second = self.upload(plan, page=2, advanceConfirmed=True)
        self.assertEqual((second["page"], second["roll"]["firstPage"], second["roll"]["completedPages"]), (2, 2, 0))
        self.report_live(second, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 21}}
        self.start(plan, second)
        self.report_live(second, 4, "completed", startedAt=21, endedAt=22, completedPages=1)
        advance = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((advance["state"], advance["completedPages"], advance["nextPage"]), ("advance", 1, 3))
        self.upload(plan, HTTPStatus.CONFLICT, page=1, advanceConfirmed=True)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, advanceConfirmed=True)

        third = self.upload(plan, page=3, advanceConfirmed=True)
        self.report_live(third, 8, "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 31}}
        self.start(plan, third)
        self.report_live(third, 4, "completed", startedAt=31, endedAt=32, completedPages=1)
        complete = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((complete["state"], complete["firstPage"], complete["completedPages"], complete["nextPage"]),
                         ("completed", 2, 2, None))

    def test_roll_adopts_verified_panel_start_and_advances_the_loaded_section(self):
        workspace, revision, plan = self.roll_plan(route_applied=False)
        first = self.upload(plan)
        workspace["view"].update(zoom=2, grid=False)
        self.save(workspace, revision)
        panel_run = {"startedAt": 11, "startedFromPanel": True, "startedReceipt": first["jobReceipt"]}
        self.report_live(first, 3, "running", index=1, **panel_run)

        # A retry before the next live poll also notices that the panel already started this page.
        self.upload(plan, HTTPStatus.CONFLICT)
        roll = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((roll["state"], roll["nextPage"]), ("running", None))
        self.start(plan, first, HTTPStatus.CONFLICT)
        self.controller.operate.assert_not_called()
        self.controller.upload_job.assert_called_once()

        self.report_live(first, 4, "completed", endedAt=22, completedPages=1, **panel_run)
        after_first = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((after_first["state"], after_first["completedPages"], after_first["nextPage"]),
                         ("advance", 1, 2))
        second = self.upload(plan, page=2, advanceConfirmed=True)
        self.assertEqual((second["roll"]["page"], second["roll"]["state"]), (2, "ready"))

    def test_unattributed_panel_completion_cannot_advance_or_reload_page(self):
        _, _, plan = self.roll_plan()
        first = self.upload(plan)
        self.report_live(first, 4, "ready")
        self.start(plan, first, HTTPStatus.CONFLICT)
        roll = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((roll["state"], roll["completedPages"], roll["nextPage"]), ("unknown", 0, None))
        self.upload(plan, HTTPStatus.CONFLICT)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, advanceConfirmed=True)
        self.controller.operate.assert_not_called()
        self.controller.upload_job.assert_called_once()

    def test_sticky_completion_event_from_before_upload_does_not_block_new_page(self):
        def transfer_after_completion(job, finish, page_add, **options):
            return {**self.acknowledge_transfer(job, finish, page_add, **options), "baselineEvent": {"code": 4}}

        _, _, plan = self.roll_plan()
        self.controller.upload_job.side_effect = transfer_after_completion
        first = self.upload(plan)
        self.report_live(first, 4, "ready")
        self.assertEqual(self.request("/api/controller-live", {})["roll"]["state"], "ready")
        self.controller.operate.return_value = {"sent": True, "run": {"state": "starting", "startedAt": 11}}
        self.assertEqual(self.start(plan, first)["roll"]["state"], "running")

    def test_roll_never_binds_a_panel_run_with_a_different_upload_receipt(self):
        _, _, plan = self.roll_plan()
        first = self.upload(plan)
        panel_run = {"startedAt": 11, "startedFromPanel": True, "startedReceipt": "another-upload"}
        self.report_live(first, 3, "running", index=1, **panel_run)
        self.assertEqual(self.request("/api/controller-live", {})["roll"]["state"], "unknown")
        self.report_live(first, 4, "completed", endedAt=22, completedPages=1, **panel_run)
        roll = self.request("/api/controller-live", {})["roll"]
        self.assertEqual((roll["state"], roll["completedPages"], roll["nextPage"]), ("unknown", 0, None))

    def test_confirmed_browser_stop_does_not_require_control_session(self):
        self.request("/api/controller-operation", {"action": "cancel-cut"})
        self.controller.require_control.assert_not_called()
        self.controller.operate.assert_called_once()
        self.assertEqual(self.controller.operate.call_args.args[0], "cancel-cut")
        self.assertIs(self.controller.operate.call_args.kwargs["confirmed_stop"], True)

    def test_roll_detects_a_continuation_of_numbered_part_without_crossing_a_contour(self):
        workspace = drawing()
        workspace["paths"] = [
            {"layer": "Крой", "closed": False, "partNumber": 1, "points": [[x, 50], [x + 10, 70]]}
            for x in (20, 2600)
        ]
        workspace["excluded"] = []
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual((plan["mode"], plan["seams"], plan["canUpload"], plan["hasContinuations"]),
                         ("roll", [], True, True))
        self.assertEqual([page["canStart"] for page in plan["pages"]], [True, False])
        self.upload(plan, HTTPStatus.BAD_REQUEST)
        self.upload(plan, HTTPStatus.CONFLICT, page=2, advanceConfirmed=True)
        self.controller.upload_job.assert_not_called()

    def test_uncertain_roll_transfer_requires_stopped_reset_before_retry(self):
        workspace = drawing()
        workspace["paths"][1]["points"] = [[2600, 50], [2610, 50], [2610, 70]]
        self.save(workspace)
        plan = self.preview(workspace)
        self.controller.upload_job.side_effect = None
        self.controller.upload_job.return_value = {
            "uploaded": False, "phase": "verification", "commitSent": True, "points": 2, "uploadUncertain": True,
        }
        self.assertEqual(self.upload(plan)["roll"]["state"], "unknown")
        self.upload(plan, HTTPStatus.CONFLICT)
        self.controller.upload_job.assert_called_once()

        self.controller.read_live.return_value = {
            "responded": True, "busy": False, "position": {"x": 0, "y": 0, "z": 0},
            "lastEvent": {"code": 50}, "run": {"state": "error", "startedAt": None, "fresh": True},
        }
        reset = {"planId": plan["planId"], "controlToken": "control", "stopped": True}
        self.request("/api/roll-reset", reset, HTTPStatus.BAD_REQUEST)
        self.controller.read_live.return_value["lastEvent"]["code"] = 5
        self.assertIsNone(self.request("/api/roll-reset", reset)["roll"])

    def test_prepare_xiao_download_preserves_optimized_roll_geometry(self):
        workspace = drawing()
        workspace["paths"][1]["points"] = [[5050, 50], [5060, 50], [5060, 70]]
        self.save(workspace)
        plan = self.preview(workspace)
        self.assertEqual((plan["mode"], plan["seams"], plan["canPrepareXiao"]), ("roll", [], True))

        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request("POST", "/api/prepare-xiao", json.dumps({"planId": plan["planId"]}),
                           {"Content-Type": "application/json"})
            response = client.getresponse()
            contents = response.read()
        self.assertEqual(response.status, HTTPStatus.OK)
        self.assertEqual(response.getheader("Content-Type"), "application/dxf")
        self.assertIn("filename*=UTF-8''", response.getheader("Content-Disposition"))
        dxf = ezdxf.read(io.StringIO(contents.decode("cp1251")))
        mark, outline = dxf.modelspace()
        self.assertEqual(dxf.units, 4)
        self.assertEqual(mark.dxftype(), "LINE")
        self.assertEqual(outline.dxftype(), "LWPOLYLINE")
        self.assertEqual([list(mark.dxf.start)[:2], list(mark.dxf.end)[:2]], plan["pages"][0]["paths"][0]["points"])
        self.assertEqual([list(point) for point in outline.get_points("xy")], plan["pages"][-1]["paths"][0]["points"])
        self.assertTrue(outline.closed)
        self.controller.upload_job.assert_not_called()
        self.controller.operate.assert_not_called()

    def test_prepare_xiao_handoff_requires_current_plan_and_uses_whole_contours(self):
        workspace = drawing()
        workspace["paths"][1]["points"] = [[40, 50], [2600, 50], [2600, 70], [40, 70]]
        revision = self.save(workspace)
        plan = self.preview(workspace)
        self.assertTrue(plan["seams"])
        options = {"planId": plan["planId"], "openInXiao": True}
        staging = Path(self.directory.name).resolve() / "staging"
        with patch("server.STAGING", staging), patch("xiao_desktop.open_dxf", return_value="queued") as handoff:
            result = self.request("/api/prepare-xiao", options)
            self.assertTrue(result["opened"])
            path = Path(result["path"])
            self.assertEqual(handoff.call_args.args[1], path)
            outline, = ezdxf.readfile(path).modelspace().query("LWPOLYLINE")
            self.assertTrue(outline.closed)
            self.assertEqual([list(point) for point in outline.get_points("xy")], workspace["paths"][1]["points"])
            workspace["paths"][1]["points"][0][0] += 1
            self.save(workspace, revision)
            self.request("/api/prepare-xiao", options, HTTPStatus.CONFLICT)
            self.assertEqual(handoff.call_count, 1)
            self.assertEqual(list(path.parent.iterdir()), [path])
        self.controller.require_control.assert_not_called()
        self.controller.upload_job.assert_not_called()
        self.controller.operate.assert_not_called()

    def test_nesting_returns_a_draft_without_changing_the_saved_drawing(self):
        workspace = drawing()
        workspace["paths"][1]["points"] = [[1000, 400], [1020, 400], [1020, 420], [1000, 420]]
        self.save(workspace)
        result = self.request("/api/nest", {
            "paths": [workspace["paths"][1]], "widthMm": 100, "gapMm": 5, "marginMm": 10, "rotation": "none",
        })
        self.assertEqual(result["partCount"], 1)
        first = result["paths"][0]
        self.assertEqual(first["layer"], "Крой")
        self.assertTrue(first["closed"])
        self.assertEqual({y for _, y in first["points"]}, {10, 30})
        self.assertEqual(self.server.workspace.read()["workspace"], workspace)
        self.controller.upload_job.assert_not_called()
        self.controller.operate.assert_not_called()

    def test_nesting_reports_which_outline_cannot_fit(self):
        paths = [{"layer": "Крой", "closed": True, "points": [[0, 0], [500, 0], [500, 300], [0, 300]]}]
        error = self.request("/api/nest", {"paths": paths, "widthMm": 100}, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(error["indices"], [0])
        self.assertEqual(error["blockedPartCount"], 1)
        self.assertAlmostEqual(error["usableWidthMm"], 79.99)
        self.assertAlmostEqual(error["minimumPartWidthMm"], 300)
        self.assertAlmostEqual(error["minimumWidthMm"], 320.01)
        self.assertAlmostEqual(error["quarterTurnMinimumWidthMm"], 320.01)
        self.assertFalse(error["quarterTurnFits"])
        self.controller.upload_job.assert_not_called()

    def test_part_number_and_rotation_follow_all_paths_of_a_piece(self):
        outline = {"layer": "Крой", "closed": True, "points": [[0, 0], [80, 0], [80, 140], [0, 140]]}
        notch = {"layer": "Насечки", "closed": False, "points": [[40, -2], [40, 3]]}
        other = {"layer": "Крой", "closed": True,
                 "points": [[300, 0], [380, 0], [380, 90], [300, 90]]}
        identified = self.request("/api/nest-parts", {"paths": [notch, other, outline]})
        self.assertEqual(identified["parts"], [{"number": 1, "indices": [0, 2]}, {"number": 2, "indices": [1]}])

        paths = [{**outline, "partNumber": 1, "partRotation": "quarter"},
                 {**notch, "partNumber": 1},
                 {**other, "partNumber": 2, "partRotation": "none"}]
        laid_out = self.request("/api/nest", {"paths": paths, "widthMm": 112, "rotation": "none"})
        self.assertEqual([(part["partNumber"], part["angle"]) for part in laid_out["placements"]], [(1, 90), (2, 0)])
        self.assertEqual([path["partRotation"] for path in laid_out["paths"]], ["quarter", "quarter", "none"])
        self.assertEqual(self.request("/api/nest-parts", {"paths": laid_out["paths"]})["parts"], [
            {"number": 1, "indices": [0, 1], "rotation": "quarter"}, {"number": 2, "indices": [2], "rotation": "none"},
        ])

        paths[0]["partRotation"] = "none"
        settings = {"paths": paths, "widthMm": 112}
        for rotation in ("none", "quarter"):
            error = self.request("/api/nest", {**settings, "rotation": rotation}, HTTPStatus.UNPROCESSABLE_ENTITY)
            self.assertFalse(error["quarterTurnFits"])
            self.assertAlmostEqual(error["minimumWidthMm"], 162.01)

    def test_numbered_touching_pieces_stay_separate_and_invalid_overrides_are_rejected(self):
        first = {"layer": "Крой", "closed": True,
                 "points": [[0, 0], [100, 0], [100, 100], [0, 100]], "partNumber": 1}
        second = {"layer": "Крой", "closed": True,
                  "points": [[100, 0], [200, 0], [200, 100], [100, 100]], "partNumber": (1 << 53) - 1}
        third = {"layer": "Крой", "closed": False, "points": [[200, 0], [300, 0]]}
        identified = self.request("/api/nest-parts", {"paths": [first, second, third]})
        self.assertEqual(identified["parts"], [{"number": 1, "indices": [0]},
                                              {"number": (1 << 53) - 1, "indices": [1]},
                                              {"number": 2, "indices": [2]}])
        for invalid in (
            [{**first, "partNumber": True}],
            [{**third, "partRotation": "quarter"}],
            [{**first, "partRotation": "quarter"}, {**second, "partNumber": 1, "partRotation": "none"}],
        ):
            self.request("/api/nest-parts", {"paths": invalid}, HTTPStatus.BAD_REQUEST)

    def test_part_grouping_changes_plan_identity_when_used(self):
        path = {"layer": "Крой", "closed": False, "points": [[2380, 10], [2440, 10]]}
        other = {"layer": "Крой", "closed": False, "points": [[2500, 10], [3000, 10]]}
        filename = "Лекало.dxf"

        def preview(paths, optimize=True):
            return self.request("/api/cut-plan", {"filename": filename, "job": {"paths": paths, "optimize": optimize}})

        numbered = {**path, "partNumber": 1}
        rotated = {**numbered, "partRotation": "none"}
        self.assertEqual(preview([numbered])["planId"], preview([rotated])["planId"])
        self.assertEqual(preview([path], False)["planId"], preview([numbered], False)["planId"])
        separate = preview([path, other])
        together = preview([{**path, "partNumber": 1}, {**other, "partNumber": 1}])
        self.assertNotEqual(separate["planId"], together["planId"])
        self.assertNotEqual(separate["pages"][0]["endX"], together["pages"][0]["endX"])

    def test_nesting_and_cut_plan_share_the_table_pass_limit(self):
        paths = [{"closed": True, "points": [[0, y], [1240, y], [1240, y + 1350], [0, y + 1350]]}
                 for y in (0, 1400)]
        for allowance, limit in ((0.12, 2450), (70, 2430)):
            with self.subTest(allowance=allowance):
                self.controller.upload_page_add.return_value = allowance
                layout = self.request("/api/nest", {"paths": paths})
                plan = self.request("/api/cut-plan", {"job": {"paths": layout["paths"]}, "filename": "roll.dxf"})
                self.assertEqual(plan["passLimitMm"], limit)
                self.assertEqual((len(plan["pages"]), plan["issues"], plan["seams"]), (layout["pageCount"], [], []))
                self.assertTrue(all(page["endX"] - page["startX"] <= limit for page in plan["pages"]))

    def test_nesting_default_width_is_1380_mm_and_can_be_overridden(self):
        paths = [{"layer": "Крой", "closed": True, "points": [[0, 0], [500, 0], [500, 1400], [0, 1400]]}]
        error = self.request("/api/nest", {"paths": paths}, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(error["indices"], [0])
        result = self.request("/api/nest", {"paths": paths, "widthMm": 1450})
        self.assertEqual(result["widthMm"], 1450)

    def test_nesting_default_width_respects_machine_width(self):
        paths = [{"layer": "Крой", "closed": True, "points": [[0, 0], [500, 0], [500, 800], [0, 800]]}]
        self.assertEqual(self.request("/api/nest", {"paths": paths})["widthMm"], 1380)
        self.server.simulator.height = 1000
        self.assertEqual(self.server.default_fabric_width, 1000)
        self.assertEqual(self.request("/api/nest", {"paths": paths, "widthMm": None})["widthMm"], 1000)


if __name__ == "__main__":
    unittest.main()
