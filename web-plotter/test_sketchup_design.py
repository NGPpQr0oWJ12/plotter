import gzip
import json
import os
import unittest
from contextlib import closing, contextmanager
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import Mock, patch
from urllib.parse import quote

from server import COMPANION_PROTOCOL, PlotterServer


EXAMPLE_PROFILE = Path(__file__).resolve().parent.parent / "research" / "machine-profile.example.json"
FILENAME = "Лекало дивана.skp"
RAW_SKP = b"native reader fixture"
MODEL = {
    "name": "model.skp",
    "bounds": [[0, 0, 0], [80, 60, 0]],
    "nodes": [{
        "id": "root", "parent": None, "name": "Обивка", "visible": True,
        "vertices": [[0, 0, 0], [80, 0, 0], [80, 60, 0], [0, 60, 0]],
        "triangles": [[0, 1, 2], [0, 2, 3]], "faces": ["face", "face"],
        "edges": [[0, 1], [1, 2], [2, 3], [3, 0]],
    }],
}
MARKUP = {"panels": [{"id": "panel", "nodeId": "root", "name": "Боковина", "faces": ["face"]}]}


class SketchUpDesignTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    @contextmanager
    def running_server(self, name="standalone", **options):
        workspace = Path(self.directory.name) / name / "workspace.json"
        with patch("server.Controller", return_value=Mock()), \
                PlotterServer(0, workspace, profile_path=EXAMPLE_PROFILE, **options) as server:
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                yield server
            finally:
                server.shutdown()
                worker.join()

    def request(self, server, method, path, expected=HTTPStatus.OK, body=None, headers=None):
        with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=10)) as client:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            contents = response.read()
        encoding = response.getheader("Content-Encoding")
        payload = json.loads(gzip.decompress(contents) if encoding == "gzip" else contents)
        self.assertEqual(response.status, expected, payload)
        self.assertEqual(int(response.getheader("Content-Length")), len(contents))
        return payload, encoding

    def post(self, server, path, data, expected=HTTPStatus.OK):
        return self.request(server, "POST", path, expected, json.dumps(data).encode("utf-8"),
                            {"Content-Type": "application/json"})[0]

    def import_fixture(self, server, model=MODEL):
        staged = []

        def read_file(command, *, stdout, **kwargs):
            worker, path = map(Path, command[-2:])
            self.assertEqual(worker.name, "skp_reader.py")
            self.assertEqual(path.read_bytes(), RAW_SKP)
            staged.append(path)
            stdout.write(json.dumps(model).encode("utf-8"))
            return Mock(returncode=0)

        with patch("sketchup_design.subprocess.run", side_effect=read_file) as reader:
            result, _ = self.request(server, "POST", "/api/cad/3d/import?filename=" + quote(FILENAME),
                                     HTTPStatus.CREATED, RAW_SKP, {"Content-Type": "application/octet-stream"})
        reader.assert_called_once()
        self.assertFalse(staged[0].exists())
        self.assertRegex(result["id"], "^[0-9a-f]{32}$")
        return result["id"]

    def test_staged_model_is_available_as_plain_and_compressed_json(self):
        with self.running_server() as server:
            server.sketchup_design = True
            model_id = self.import_fixture(server)
            link = f"/api/cad/3d/model?id={model_id}"
            plain, encoding = self.request(server, "GET", link)
            self.assertIsNone(encoding)
            self.assertEqual(plain, {**MODEL, "name": FILENAME,
                                     "nodes": [{**MODEL["nodes"][0], "name": FILENAME}]})
            compressed, encoding = self.request(server, "GET", link, headers={"Accept-Encoding": "deflate, gzip"})
            self.assertEqual(encoding, "gzip")
            self.assertEqual(compressed, plain)
            self.assertTrue(self.request(server, "GET", "/api/registry")[0]["sketchupDesign"])

    def test_preview_contour_returns_world_patch_across_faces_without_saving(self):
        with self.running_server() as server:
            server.sketchup_design = True
            model_id = self.import_fixture(server)
            layer = {"id": "draft", "name": "Обивка", "color": "#447799",
                     "surfaces": [{"nodeId": "root", "faces": ["face"]}]}
            anchors = []
            for x, y in ((10,10), (60,10), (60,50), (10,50)):
                u, v = x/80, y/60
                anchors.append({"triangle": 0, "barycentric": [1-u, u-v, v]} if u >= v
                               else {"triangle": 1, "barycentric": [1-v, u, v-u]})
            body = {"id": model_id, "state": {"fabricLayers": [layer]}, "anchors": anchors, "nodeId": "fabric:draft"}
            path = "/api/cad/3d/preview-contour"
            before = self.request(server, "GET", "/api/registry")[0]["entries"]
            preview = self.post(server, path, body)
            self.assertEqual(set(preview), {"vertices", "triangles", "loops", "seed"})
            self.assertGreater(len(preview["triangles"]), 2)
            self.assertEqual(len(preview["loops"]), 1)
            self.assertEqual(self.post(server, path, {**body, "seed": preview["seed"]}), preview)
            self.post(server, path, {**body, "anchors": anchors[:2]}, HTTPStatus.BAD_REQUEST)
            self.assertEqual(self.request(server, "GET", "/api/registry")[0]["entries"], before)

    def test_pattern_transfer_and_drawing_attachment_survive_revision_and_move(self):
        with self.running_server() as server:
            server.sketchup_design = True
            root = MODEL["nodes"][0]
            insert = {**root, "id": "root/C2", "parent": "root", "name": "Вставка", "faces": ["insert-face"] * 2,
                      "vertices": [[x + 80, y, z] for x, y, z in root["vertices"]]}
            model = {**MODEL, "bounds": [[0, 0, 0], [160, 60, 0]], "nodes": [root, insert]}
            model_id = self.import_fixture(server, model)
            fabric = {"id": "fabric-1", "name": "Основная ткань", "color": "#365f82",
                      "surfaces": [{"nodeId": "root", "faces": ["face"]},
                                   {"nodeId": "root/C2", "faces": ["insert-face"]}]}
            model_markup = {**MARKUP, "componentNames": {"root": "Боковина слева"}, "fabricLayers": [fabric]}
            options = {"id": model_id, "state": model_markup, "allowanceMm": 5, "notchDepthMm": 2}
            calculated = self.post(server, "/api/cad/3d/calculate", options)
            self.assertTrue(calculated["complete"])
            self.assertEqual([(panel["id"], panel["kind"]) for panel in calculated["panels"]], [("panel", "flat")])
            self.post(server, "/api/cad/3d/transfer", {**options, "notchDepthMm": 6}, HTTPStatus.BAD_REQUEST)
            entities = self.post(server, "/api/cad/3d/transfer", options)["entities"]
            self.assertEqual(len(entities), 2)
            outline = next(entity for entity in entities if not entity.get("reference"))
            self.assertTrue(outline["closed"])
            points = outline["points"]
            double_area = sum(x * v - y * u for (x, y), (u, v) in zip(points, points[1:] + points[:1]))
            self.assertAlmostEqual(abs(double_area) / 2, (80 + 10) * (60 + 10))

            attachment = {"id": model_id, "state": model_markup}
            registry = "/api/registry"
            draft = self.post(server, registry, {"action": "save", "kind": "drawing", "name": "Чертёж",
                                                 "entities": entities, "model3d": attachment})
            self.assertEqual((draft["revision"], draft["model3d"]), (1, attachment))
            link = f"{registry}?id={draft['id']}"
            changes = {"action": "save", "kind": "drawing", "id": draft["id"],
                       "revision": draft["revision"], "name": "Уточнённый чертёж"}
            invalid = {**fabric, "surfaces": [{"nodeId": "root", "faces": []}]}
            invalid_attachment = {**attachment, "state": {**model_markup, "fabricLayers": [invalid]}}
            self.post(server, registry, {**changes, "model3d": invalid_attachment}, HTTPStatus.BAD_REQUEST)
            self.assertEqual(self.request(server, "GET", link)[0], draft)
            updated = self.post(server, registry, changes)
            self.post(server, registry, changes, HTTPStatus.CONFLICT)
            folder = self.post(server, registry, {"action": "save", "kind": "folder", "name": "Изделие"})
            moved = self.post(server, registry, {"action": "move", "id": draft["id"],
                                                 "revision": updated["revision"], "parent": folder["id"]})
            self.assertEqual((moved["revision"], moved["parent"], moved["name"]),
                             (3, folder["id"], changes["name"]))
            self.assertEqual((moved["entities"], moved["model3d"]), (draft["entities"], attachment))
            self.assertEqual(self.request(server, "GET", link)[0], moved)
            with server.production.connect(write=True) as db:
                db.execute("UPDATE sketchup_models SET expires = 0 WHERE id = ?", (model_id,))
            model_link = f"/api/cad/3d/model?id={model_id}"
            self.assertEqual(self.request(server, "GET", model_link)[0]["name"], FILENAME)
            deleted = self.post(server, registry, {"action": "delete", "id": moved["id"], "revision": moved["revision"]})
            self.assertTrue(deleted["deleted"])
            self.request(server, "GET", model_link, HTTPStatus.NOT_FOUND)

    def test_model_edge_splits_selectable_face_and_preserves_fabric_anchors(self):
        with self.running_server() as server:
            server.sketchup_design = True
            identifier = self.import_fixture(server)
            edge = {"id": "sample-edge", "nodeId": "root", "faceId": "face",
                    "points": [[40, 0, 0], [40, 60, 0]]}
            state = {"modelEdges": [edge]}
            model = self.post(server, "/api/cad/3d/model-surface", {
                "id": identifier, "state": state, "previousEdges": [],
            })
            original, derived = model["nodes"]
            self.assertEqual(original["editorHiddenFaces"], ["face"])
            self.assertEqual(derived["editorSourceFace"], "face")
            first, second = dict.fromkeys(derived["faces"])
            self.assertNotEqual(first, second)
            node_id = "fabric:split-surface"
            layer = {"id": "split-surface", "name": "Chosen portion", "color": "#365f82",
                     "surfaces": [{"nodeId": derived["id"], "faces": [first]}]}
            state = {**state, "fabricLayers": [layer]}
            regions = self.post(server, "/api/cad/3d/selected-regions", {
                "id": identifier, "state": state, "nodeId": node_id, "faces": ["0:" + first],
            })["regions"]
            region, = regions
            state["panels"] = [{"id": "half", "nodeId": node_id, "name": "One side", **region}]
            panel, = self.post(server, "/api/cad/3d/calculate", {
                "id": identifier, "state": state,
            })["panels"]
            points = [panel["vertices"][index] for index in panel["loops"][0]]
            area = sum(x * v - y * u for (x, y), (u, v) in zip(points, points[1:] + points[:1]))
            self.assertAlmostEqual(abs(area) / 2, 40 * 60)
            saved = self.post(server, "/api/registry", {
                "action": "save", "kind": "drawing", "name": "Drawing with edges", "entities": [],
                "model3d": {"id": identifier, "state": state},
            })
            self.assertEqual(saved["model3d"]["state"]["modelEdges"], [edge])
            self.post(server, "/api/cad/3d/model-surface", {
                "id": identifier, "state": {**state, "modelEdges": []}, "previousEdges": [edge],
            }, HTTPStatus.BAD_REQUEST)
            raw, _ = self.request(server, "GET", "/api/cad/3d/model?id=" + identifier)
            self.assertEqual(len(raw["nodes"]), 1)
            self.assertNotIn("editorHiddenFaces", raw["nodes"][0])

    def test_surface_fabrics_round_trip_by_occurrence_alongside_legacy_markup(self):
        with self.running_server() as server:
            server.sketchup_design = True
            root = MODEL["nodes"][0]
            child = {**root, "id": "root/C1", "parent": "root"}
            model_id = self.import_fixture(server, {**MODEL, "nodes": [root, child]})
            registry = "/api/registry"
            first = {"nodeId": "root", "faceId": "face", "name": "Рогожка", "color": "#AA8844"}
            second = {**first, "nodeId": "root/C1", "name": "Велюр", "color": "#334488"}
            legacy = {**MARKUP, "seams": [{"id": "hem", "kind": "seam", "nodeId": "root",
                                           "anchors": [{"vertex": 0}, {"vertex": 1}]}]}
            state = {**legacy, "surfaceFabrics": [first, second]}
            payload = {"action": "save", "kind": "drawing", "name": "Обивка", "entities": [],
                       "model3d": {"id": model_id, "state": state}}
            drawing = self.post(server, registry, payload)
            link = f"{registry}?id={drawing['id']}"
            self.assertEqual(self.request(server, "GET", link)[0]["model3d"]["state"], state)
            changes = {**payload, "id": drawing["id"], "revision": drawing["revision"]}
            for invalid in ([first, first], None, [None], [{**first, "name": "а" * 81}], [{**first, "color": "#abc"}],
                            [{**first, "nodeId": ""}], [{**first, "faceId": 1}]):
                with self.subTest(invalid=invalid):
                    attachment = {"id": model_id, "state": {**legacy, "surfaceFabrics": invalid}}
                    self.post(server, registry, {**changes, "model3d": attachment}, HTTPStatus.BAD_REQUEST)
            self.assertEqual(self.request(server, "GET", link)[0], drawing)
            cleared = self.post(server, registry, {**changes, "model3d": {"id": model_id, "state": legacy}})
            self.assertEqual(cleared["model3d"]["state"], legacy)

    def test_spatial_drafting_and_edge_notches_survive_a_saved_drawing(self):
        with self.running_server() as server:
            server.sketchup_design = True
            model_id = self.import_fixture(server)
            guide = {"id": "guide-1", "kind": "guide", "from": [2, 3, 0], "to": [10, 3, 0], "nodeIds": ["root"]}
            line = {**guide, "id": "line-1", "kind": "line", "to": [10, 3, 8]}
            notch = {"id": "notch-1", "edgeId": "edge-1", "position": 0.25}
            panel = {**MARKUP["panels"][0], "notches": [notch]}
            state = {"panels": [panel], "drafting": [guide, line]}
            payload = {"action": "save", "kind": "drawing", "name": "Разметка", "entities": [],
                       "model3d": {"id": model_id, "state": state}}
            path = "/api/registry"
            saved = self.post(server, path, payload)
            self.assertEqual(self.request(server, "GET", f"{path}?id={saved['id']}")[0]["model3d"]["state"], state)
            changes = {**payload, "id": saved["id"], "revision": saved["revision"]}
            for invalid in ({"drafting": [guide, guide]}, {"drafting": [{**line, "to": line["from"]}]},
                            {"drafting": [{**guide, "from": [0, True, 0]}]},
                            {"panels": [{**panel, "notches": [{**notch, "position": 1}]}]}):
                with self.subTest(invalid=invalid):
                    bad = {"id": model_id, "state": {**state, **invalid}}
                    self.post(server, path, {**changes, "model3d": bad}, HTTPStatus.BAD_REQUEST)

    def test_missing_or_expired_stage_cannot_change_a_drawing(self):
        with self.running_server() as server:
            server.sketchup_design = True
            model_id = self.import_fixture(server)
            with server.production.connect(write=True) as db:
                db.execute("UPDATE sketchup_models SET expires = 0 WHERE id = ?", (model_id,))
            registry = "/api/registry"
            payload = {"action": "save", "kind": "drawing", "name": "Чертёж", "entities": []}
            original = self.post(server, registry, payload)
            for missing_id in (model_id, "0" * 32):
                with self.subTest(missing_id=missing_id):
                    self.request(server, "GET", f"/api/cad/3d/model?id={missing_id}", HTTPStatus.NOT_FOUND)
                    self.post(server, "/api/cad/3d/calculate", {"id": missing_id, "state": MARKUP},
                              HTTPStatus.NOT_FOUND)
                    changed = {**payload, "name": "Не сохранится", "model3d": {"id": missing_id, "state": MARKUP}}
                    self.post(server, registry, changed, HTTPStatus.NOT_FOUND)
                    self.post(server, registry, {**changed, "id": original["id"], "revision": original["revision"]},
                              HTTPStatus.NOT_FOUND)
                    self.assertEqual(self.request(server, "GET", f"{registry}?id={original['id']}")[0], original)
                    self.assertEqual(len(self.request(server, "GET", registry)[0]["entries"]), 1)

    def test_transfer_check_and_selection_with_an_unavailable_panel(self):
        with self.running_server() as server:
            server.sketchup_design = True
            model_id = self.import_fixture(server)
            bad_panel = {"id": "bad", "nodeId": "root", "name": "Повреждённая панель", "faces": ["missing"]}
            state = {"panels": [*MARKUP["panels"], bad_panel]}
            options = {"id": model_id, "state": state, "allowanceMm": 0, "notchDepthMm": 0}
            calculated = self.post(server, "/api/cad/3d/calculate", options)
            self.assertFalse(calculated["complete"])
            failure = next(item for item in calculated["diagnostics"] if item["panelId"] == "bad")
            self.assertEqual((failure["severity"], failure["code"]), ("error", "geometry_unavailable"))
            requested = {**options, "panelIds": ["bad", "panel"]}
            report = self.post(server, "/api/cad/3d/check-transfer", requested)["panels"]
            self.assertEqual([(panel["id"], panel["ready"]) for panel in report], [("bad", False), ("panel", True)])
            self.assertEqual([panel["issues"][0]["code"] for panel in report], ["geometry_unavailable", "ready"])
            self.assertIn("Повреждённая панель", self.post(server, "/api/cad/3d/transfer", options, HTTPStatus.BAD_REQUEST)["error"])
            one = {**options, "panelIds": ["panel"]}
            transferred = self.post(server, "/api/cad/3d/transfer", one)
            self.assertEqual(len(transferred["entities"]), 2)
            invalid_notch = self.post(server, "/api/cad/3d/check-transfer", {**one, "notchDepthMm": 1})
            self.assertEqual(invalid_notch["panels"][0]["issues"][0]["code"], "notch_invalid")
            for selected in (["missing"], ["panel", "panel"], []):
                with self.subTest(selected=selected):
                    self.post(server, "/api/cad/3d/transfer", {**options, "panelIds": selected}, HTTPStatus.BAD_REQUEST)

    def test_native_reader_is_gated_to_standalone_windows(self):
        with self.running_server() as standalone:
            self.assertEqual(standalone.sketchup_design, os.name == "nt")
        origin = "https://plotter.example.invalid"
        for name, option in (("central", "public_origin"), ("companion", "companion_origin")):
            with self.subTest(mode=name), self.running_server(name, **{option: origin}) as server, \
                    patch("sketchup_design.subprocess.run") as reader:
                self.assertFalse(server.sketchup_design)
                headers = {"Content-Type": "application/octet-stream"}
                if name == "companion":
                    headers.update({"Origin": origin, "X-Plotter-Companion": str(COMPANION_PROTOCOL)})
                self.request(server, "POST", "/api/cad/3d/import?filename=" + quote(FILENAME),
                             HTTPStatus.NOT_FOUND, RAW_SKP, headers)
                self.request(server, "GET", "/api/cad/3d/model?id=" + "0" * 32, HTTPStatus.NOT_FOUND,
                             headers=headers)
                reader.assert_not_called()


    def test_fit_runs_in_local_worker_and_returns_triangle_metrics(self):
        with self.running_server() as server:
            server.sketchup_design = True
            identifier = self.import_fixture(server)
            job = self.post(server, "/api/cad/3d/fit", {
                "id": identifier, "state": {**MARKUP, "fit": {"foamNodeIds": ["root"]}},
            }, HTTPStatus.ACCEPTED)["id"]
            deadline = monotonic() + 8
            while True:
                response, _ = self.request(server, "GET", "/api/cad/3d/fit?id=" + job)
                if response["status"] != "running":
                    break
                self.assertLess(monotonic(), deadline, "fit worker did not finish")
                sleep(.03)
            self.assertEqual(response["status"], "done", response)
            result = response["result"]
            self.assertFalse(result["calibrated"])
            self.assertEqual(result["panels"][0]["id"], "panel")
            self.assertEqual(len(result["panels"][0]["strain"]), 2)
            self.assertEqual(self.post(server, "/api/cad/3d/fit-cancel", {"id": job})["status"], "cancelled")

    def test_transferred_pattern_metadata_and_grain_survive_2d_registry_save(self):
        with self.running_server() as server:
            server.sketchup_design = True
            identifier = self.import_fixture(server)
            state = {"panels": [{**MARKUP["panels"][0], "number": "Д07", "quantity": 2, "side": "left",
                                "fabricName": "Ткань", "direction": "nap",
                                "grain": [{"vertex": 0}, {"vertex": 1}]}]}
            response = self.post(server, "/api/cad/3d/transfer", {
                "id": identifier, "state": state, "allowanceMm": 10, "notchDepthMm": 2,
            })
            self.assertEqual({tuple(item["groupPath"]) for item in response["entities"]}, {("Лекала 3D", "panel")})
            pattern = response["patterns"]["panel"]
            self.assertEqual(pattern["number"], "Д07")
            draft = self.post(server, "/api/registry", {
                "action": "save", "kind": "drawing", "name": "Лекало", "patterns": response["patterns"],
                "entities": response["entities"],
            })
            self.assertEqual(draft["patterns"]["panel"]["quantity"], 2)
            part = self.post(server, "/api/registry", {
                "action": "save", "kind": "part", "name": "Д07 Боковина", "pattern": pattern,
                "entities": response["entities"],
            })
            self.assertEqual(part["pattern"]["direction"], "nap")
            self.assertEqual(part["pattern"]["side"], "left")
            prepared = self.post(server, "/api/registry", {
                "action": "collect", "members": [{"id": part["id"], "quantity": 2}],
            })
            self.assertTrue(all(path["partRotation"] == "none" for path in prepared["paths"]))
            self.assertEqual({path["partNumber"] for path in prepared["paths"]}, {1, 2})


if __name__ == "__main__":
    unittest.main()
