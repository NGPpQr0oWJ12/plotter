from __future__ import annotations

import argparse
from base64 import b64encode
from bisect import bisect_right
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from email.message import Message
import gzip
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
from struct import unpack_from
from sys import platform
from threading import Event, Lock, Thread, Timer
from time import monotonic
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request
from uuid import uuid4
import webbrowser

import ezdxf
from ezdxf.lldxf.validator import is_valid_layer_name

from app_update import MAX_ARCHIVE, AppUpdates, UpdateError, read_release_notes, read_version
from auto_run import AutoRunReadback
from cad import MAX_FILE_BYTES, dwg_available, import_drawing, repair_closed_contours
from controller import (ACTIVE_CONTROLLER_EVENTS, CONTROLLER_PORT, EXTERNAL_CUT_EVENTS, HOLD_LEASE_SECONDS, TICKS_PER_MM,
                        Controller)
from cut_journal import MAX_EVENTS, CutJournal
from cut_plan import DEFAULT_WASTE_MARGIN_MM, prepare_cut_plan, section_local_limit, section_local_x
from demo_controller import DemoController
from designer_cad import edit_design, import_design, prepare_design
from fit_jobs import FitJobs
from label_queue import LabelQueue, part_ends
from machine import PlotterError, Simulator, build_plan, number
from nesting import MAX_PART_NUMBER, NestingFitError, annotate_notches, identify_parts, nest_paths
from order_analysis import analysis_period, order_analysis
from panel_diagnostics import compare_panel_samples
from panel_readback import PanelReadback
from production_store import RECORD_REVISION_MESSAGE, ProductionStore, RecordNotFound, key, verify_arrangement
from registry_archive import (MAX_REGISTRY_ARCHIVE, RAR_MIME, RAR_SIGNATURE, RegistryArchiveTooLarge,
                              read_registry_archive, write_registry_archive)
from run_monitor import timestamp
from sketchup_design import MAX_MARKUP_BYTES, MAX_SKP_BYTES, check_surface_edit, import_model, markup, transfer_manifest
from workspace import (MAX_REVISION, MAX_WORKSPACE_BYTES, WORKSPACE_REVISION_CONFLICT, WORKSPACE_REVISION_MESSAGE,
                       WorkspaceConflict, WorkspaceStore, drawing_paths, excluded_paths, validate_workspace)
from xiao_job import COORDINATE_OFFSET_MM, EncodedJob, JobEncodingError, prepare_single_page


ROOT = Path(__file__).resolve().parent
APP_VERSION = read_version(ROOT)
COMPANION_INSTALLER = f"Plotter-Companion-Setup-{APP_VERSION}-x64.exe"
COMPANION_UPDATE = f"plotter-companion-v{APP_VERSION}-windows-x64.zip"
PROFILE = ROOT.parent / "research" / "machine-profile.json"
LOCALIZED_XIAO = ROOT.parent / "builds" / "Xiao-RU-2022-09-18-v1" / "xiao.exe"
STAGING = ROOT / "staging"
LOCAL_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8765
API_VERSION = 27
COMPANION_PORT = 8767
COMPANION_PROTOCOL = 1
DIRECT_LABEL_ROUTES = frozenset({"/api/label-printers", "/api/label-print-part", "/api/labels-auto-relay"})
HARDWARE_ROUTES = frozenset({"/api/status", "/api/cut-plan", "/api/controller", "/api/controller-live",
    "/api/controller-position", "/api/controller-job", "/api/controller-feed-mode", "/api/control-session",
    "/api/controller-hold", "/api/controller-operation", "/api/controller-upload", "/api/roll-reset",
    "/api/panel-snapshot", "/api/panel-compare", "/api/reachability", "/api/open-in-xiao", "/api/prepare-xiao",
    "/api/cut-context", "/api/labels-status", "/api/labels-print", "/api/labels-settings",
    "/api/labels-pdf", "/api/labels-image", "/api/labels-auto"}) | DIRECT_LABEL_ROUTES
RESTART_SAFE_ACTIONS = {
    "/api/controller-hold": "stop", "/api/controller-operation": "cancel-cut", "/api/control-session": "disarm",
}
CUT_TOOL = 4
DEFAULT_FABRIC_WIDTH_MM = 1380
ROLL_PASS_LENGTH_MM = 2450
PREVIEW_PAGE_ADD_MM = 0.12
WORKSPACE_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Plotter" / "workspace.json"
MAX_JSON = 16 * 1024 * 1024
DEMO_CUT_CAPACITY = 32
DEMO_CUT_IDLE_SECONDS = 15 * 60
MIME = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/simulation.js": ("simulation.js", "text/javascript; charset=utf-8"),
    "/workplaces.js": ("workplaces.js", "text/javascript; charset=utf-8"),
    "/labels.js": ("labels.js", "text/javascript; charset=utf-8"),
    "/label-printer-settings.js": ("label-printer-settings.js", "text/javascript; charset=utf-8"),
    "/labels.css": ("labels.css", "text/css; charset=utf-8"),
    "/confirm-action.js": ("confirm-action.js", "text/javascript; charset=utf-8"),
    "/workplaces.css": ("workplaces.css", "text/css; charset=utf-8"),
    "/designer-cad.js": ("designer-cad.js", "text/javascript; charset=utf-8"),
    "/designer-shortcuts.js": ("designer-shortcuts.js", "text/javascript; charset=utf-8"),
    "/designer-cad.css": ("designer-cad.css", "text/css; charset=utf-8"),
    "/OpenGostTypeB-Regular.ttf": ("OpenGostTypeB-Regular.ttf", "font/ttf"),
    "/designer-3d.js": ("designer-3d.js", "text/javascript; charset=utf-8"),
    "/designer-3d-magnet.js": ("designer-3d-magnet.js", "text/javascript; charset=utf-8"),
    "/designer-3d-drafting.js": ("designer-3d-drafting.js", "text/javascript; charset=utf-8"),
    "/designer-3d-fabric.js": ("designer-3d-fabric.js", "text/javascript; charset=utf-8"),
    "/designer-3d-folds.js": ("designer-3d-folds.js", "text/javascript; charset=utf-8"),
    "/designer-3d-outline.js": ("designer-3d-outline.js", "text/javascript; charset=utf-8"),
    "/designer-3d-overlay.js": ("designer-3d-overlay.js", "text/javascript; charset=utf-8"),
    "/designer-3d-selection.js": ("designer-3d-selection.js", "text/javascript; charset=utf-8"),
    "/designer-3d-objects.js": ("designer-3d-objects.js", "text/javascript; charset=utf-8"),
    "/designer-3d-patterns.js": ("designer-3d-patterns.js", "text/javascript; charset=utf-8"),
    "/designer-3d-fit.js": ("designer-3d-fit.js", "text/javascript; charset=utf-8"),
    "/designer-3d.css": ("designer-3d.css", "text/css; charset=utf-8"),
    "/three-3d.js": ("three-3d.js", "text/javascript; charset=utf-8"),
    "/plotter-monochrome.css": ("plotter-monochrome.css", "text/css; charset=utf-8"),
}
PRODUCTION_ROUTES = {"/api/registry", "/api/orders", "/api/operator-workspace"}
WORKSPACE_IN_USE = "workspace_in_use"
LOGGER = logging.getLogger("plotter")


class RequestError(PlotterError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def https_origin(value):
    parsed = urlsplit(value or "")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.\-]+(?::[0-9]{1,5})?", parsed.netloc)):
        raise ValueError("Укажите точный HTTPS-адрес веб-панели без пути.")
    port = parsed.port
    if port == 0:
        raise ValueError("Недопустимый HTTPS-порт.")
    return f"https://{parsed.hostname}" + (f":{port}" if port not in (None, 443) else "")


class OfflineController:
    auto_run_readback = None
    upload_uncertain = False
    upload_in_progress = False

    def upload_page_add(self):
        return None

    def actuator_snapshot(self):
        return None

    def read_live(self):
        return {"responded": False, "busy": False, "message": "Подключите помощник на компьютере у станка."}

    def stop_active_hold(self):
        pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class CompanionWorkspace:
    """Keep the authoritative drawing at the central server."""

    def __init__(self, path, origin, ca_file=None):
        self.path = path
        self.origin = origin
        handlers = [NoRedirect()]
        if ca_file:
            handlers += [ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file)))]
        self.opener = build_opener(*handlers)

    def read(self):
        return self._request("/api/workspace")

    def write(self, expected_revision, workspace):
        payload = {"revision": expected_revision, "workspace": validate_workspace(workspace)}
        if workspace is None:
            return self._request("/api/new-cut", {"revision": expected_revision, "stopped": True})
        return self._request("/api/workspace", payload)

    def _request(self, endpoint, payload=None):
        data = self.production_request(endpoint, payload)
        if not isinstance(data, dict) or type(data.get("revision")) is not int or "workspace" not in data:
            raise RequestError("Общая раскладка имеет неверный формат.", HTTPStatus.BAD_GATEWAY)
        if data.get("workspace") is not None:
            data["workspace"] = validate_workspace(data["workspace"])
        return data

    def production_request(self, endpoint, payload=None, *, raw=None, pdf=False, rar=False):
        request = Request(self.origin + endpoint)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > MAX_WORKSPACE_BYTES:
                raise ValueError("Файл рабочей области слишком велик.")
            request = Request(request.full_url, body, {"Content-Type": "application/json", "Origin": self.origin})
        elif raw is not None:
            request = Request(request.full_url, raw, {"Content-Type": "application/octet-stream", "Origin": self.origin})
        binary = ("application/pdf", b"%PDF-") if pdf else (RAR_MIME, RAR_SIGNATURE) if rar else None
        maximum = MAX_REGISTRY_ARCHIVE if rar else MAX_WORKSPACE_BYTES
        try:
            with self.opener.open(request, timeout=90 if raw is not None or binary else 30 if payload is not None else 10) as response:
                contents = response.read(maximum + 1)
                content_type = response.headers.get_content_type() if binary else None
            if len(contents) > maximum:
                raise ValueError("Workspace too large")
            if binary:
                mime, signature = binary
                if content_type != mime or not contents.startswith(signature):
                    raise ValueError("Invalid binary response")
                return contents
            return json.loads(contents)
        except HTTPError as error:
            with error:
                if error.code in (400, 404, 409, 413, 415, 422):
                    try:
                        response = json.loads(error.read(4096))
                        message = response.get("error")
                    except (OSError, ValueError, AttributeError, RecursionError):
                        message = None
                    if isinstance(message, str) and message:
                        if error.code == 409:
                            code = response.get("code")
                            if not isinstance(code, str) or not code:
                                code = None
                                revision = payload.get("revision") if isinstance(payload, dict) else None
                                if type(revision) is int and 0 <= revision <= MAX_REVISION:
                                    legacy_standalone = (endpoint in ("/api/workspace", "/api/new-cut")
                                                         and message == WORKSPACE_REVISION_MESSAGE)
                                    legacy_managed = (endpoint == "/api/operator-workspace" and payload.get("action") == "save"
                                                      and message == RECORD_REVISION_MESSAGE)
                                    if legacy_standalone or legacy_managed:
                                        code = WORKSPACE_REVISION_CONFLICT
                            raise WorkspaceConflict(message, code=code) from error
                        raise RequestError(message, error.code) from error
            raise RequestError("Помощник не смог обратиться к общей раскладке.", HTTPStatus.BAD_GATEWAY) from error
        except (URLError, OSError, ValueError, RecursionError) as error:
            raise RequestError("Помощник не смог обратиться к общей раскладке.", HTTPStatus.BAD_GATEWAY) from error


@contextmanager
def request_lock(lock, message):
    if not lock.acquire(blocking=False):
        raise RequestError(message, HTTPStatus.CONFLICT)
    try:
        yield
    finally:
        lock.release()


@dataclass(frozen=True)
class RollRun:
    plan_id: str
    page: int
    pages: int
    first_page: int
    feed_after_mm: float
    state: str
    completed_pages: int
    points: int = 0
    started_at: int | None = None
    baseline_event: int | None = None
    material_advanced: bool = False
    head_positioning: str | None = None
    head_move_target: tuple[float, float] | None = None

    def snapshot(self):
        next_page = self.page if self.state == "ready" else self.page + 1 if self.state == "advance" else None
        return {"planId": self.plan_id, "page": self.page, "pages": self.pages, "firstPage": self.first_page,
                "state": self.state,
                "completedPages": self.completed_pages, "feedAfterMm": self.feed_after_mm, "nextPage": next_page,
                "materialAdvanced": self.material_advanced, "headPositioning": self.head_positioning}


@dataclass(frozen=True)
class CutProgress:
    plan_id: str
    filename: str
    page: int
    pages: int
    first_page: int
    points: int
    start_x: float
    end_x: float
    state: str = "transferring"
    receipt: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    updated_at: int | None = None
    index: int = 0
    completed_pages: int = 0
    fresh: bool = False
    events: tuple = ()
    route: EncodedJob | None = None

    def snapshot(self):
        return {"planId": self.plan_id, "filename": self.filename, "page": self.page,
                "pages": self.pages, "firstPage": self.first_page, "completedPages": self.completed_pages,
                "index": self.index, "indexLimit": self.points, "state": self.state,
                "startX": self.start_x, "endX": self.end_x, "fresh": self.fresh,
                "startedAt": self.started_at, "endedAt": self.ended_at, "updatedAt": self.updated_at,
                "events": list(self.events)}


def drawing_key(job, filename, width, height, page_add, *, roll=False):
    # A view or preferences save must not invalidate an unchanged drawing.
    paths = [{"layer": path.get("layer", "0"), "closed": path.get("closed", False),
              "points": [[float(x), float(y)] for x, y in path["points"]]} for path in job["paths"]]
    optimize = job.get("optimize", True)
    trims_planned = job.get("materialWidthMm") is not None
    if roll or optimize or trims_planned:
        for source, path in zip(job["paths"], paths):
            part_number = source.get("partNumber")
            if type(part_number) is int and 1 <= part_number <= MAX_PART_NUMBER:
                path["partNumber"] = part_number
            if (optimize or trims_planned) and source.get("notch") is True:
                path["notch"] = True
    document = [filename, width, height, page_add, CUT_TOOL, paths, optimize]
    if trims_planned:
        margin = job.get("materialMarginMm") or DEFAULT_WASTE_MARGIN_MM
        document.append(["waste-trim-v1", float(job["materialWidthMm"]), float(margin)])
    if roll:
        document.append(ROLL_PASS_LENGTH_MM)
    if "cutContext" in job:
        document.append(job["cutContext"])
    return sha256(json.dumps(document, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def translated_page_paths(page, max_x):
    paths = [*page["paths"], *page.get("wasteTrim", {}).get("paths", [])]
    return [{**path, "points": [[section_local_x(x, page["startX"], max_x), y] for x, y in path["points"]]}
            for path in paths]


def describe_section_continuations(plan, job):
    pages = plan["pages"]
    part_spans = {}
    seams_by_x = {}
    for seam in plan["seams"]:
        seams_by_x.setdefault(seam["x"], []).append(seam)
    for position, page in enumerate(pages):
        for path in page["paths"]:
            index = path["index"]
            part = job["paths"][index].get("partNumber")
            key = ("part", part) if type(part) is int and 1 <= part <= MAX_PART_NUMBER else ("path", index)
            span = part_spans.setdefault(key, [position, position])
            span[1] = position

    # A new session may begin only at a boundary that does not leave an unfinished part behind.
    changes = [0] * (len(pages) + 1)
    for first, last in part_spans.values():
        if first != last:
            changes[first + 1] += 1
            changes[last + 1] -= 1
    crossing = 0
    for position, page in enumerate(pages):
        crossing += changes[position]
        page.update(canStart=crossing == 0, incomingSeams=seams_by_x.get(page["startX"], []),
                    outgoingSeams=seams_by_x.get(page["endX"], []))
    plan["hasContinuations"] = any(not page["canStart"] for page in pages)


def encode_cut_section(page, width, height, page_add):
    local_limit = section_local_limit(width, page_add)
    plan = build_plan({"paths": translated_page_paths(page, local_limit)}, width, height)
    return prepare_single_page(plan, tool=CUT_TOOL, width=width, height=height, page_add_mm=page_add)


def demo_drawing():
    def contour(layer, points, closed=True):
        return {"layer": layer, "points": points, "closed": closed}

    return {
        "filename": "Лекала · демонстрация.dxf",
        "units": "mm",
        "warnings": [],
        "paths": [
            contour("Контуры", [[0, 0], [580, 0], [588, 330], [595, 470], [618, 590], [640, 665], [597, 702],
                                  [510, 763], [421, 808], [393, 773], [349, 742], [291, 731], [232, 742],
                                  [183, 773], [158, 808], [70, 766], [-9, 710], [-58, 668], [-33, 590],
                                  [-10, 466], [-4, 335]]),
            contour("Контуры", [[760, 5], [1305, 5], [1311, 333], [1314, 475], [1339, 599], [1362, 666],
                                  [1308, 713], [1234, 760], [1147, 805], [1111, 798], [1076, 785],
                                  [1035, 779], [992, 785], [956, 798], [922, 805], [832, 759], [756, 713],
                                  [704, 665], [729, 596], [753, 472], [756, 331]]),
            contour("Контуры", [[1424, 620], [1886, 621], [1932, 747], [1964, 884], [1957, 950],
                                  [1919, 1009], [1861, 1066], [1796, 1115], [1726, 1149], [1654, 1161],
                                  [1585, 1147], [1511, 1110], [1442, 1058], [1393, 1006], [1355, 950],
                                  [1349, 886], [1380, 748]]),
            contour("Контуры", [[1424, 14], [1886, 14], [1932, 141], [1964, 276], [1957, 343],
                                  [1919, 402], [1861, 458], [1796, 506], [1726, 540], [1654, 553],
                                  [1585, 540], [1511, 503], [1442, 450], [1393, 397], [1355, 341],
                                  [1349, 276], [1380, 140]]),
            contour("Контуры", [[-16, 939], [659, 939], [659, 1101], [-16, 1101]]),
            contour("Контуры", [[739, 939], [1041, 939], [1041, 1101], [739, 1101]]),
            contour("Контуры", [[1101, 939], [1285, 939], [1285, 1101], [1101, 1101]]),
            contour("Разметка", [[288, 126], [288, 647]], False),
            contour("Разметка", [[1035, 126], [1035, 647]], False),
            contour("Разметка", [[1654, 751], [1654, 1052]], False),
            contour("Разметка", [[1654, 137], [1654, 443]], False),
        ],
    }


def output_name(value):
    if not isinstance(value, str):
        value = "Раскрой"
    name = re.split(r"[/\\]", value)[-1]
    name = re.sub(r"\.(?:dxf|dwg)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)[:80].strip(" .")
    if not name or re.fullmatch(r"(?i:con|prn|aux|nul|(?:com|lpt)[0-9¹²³])(?:\..*)?", name):
        name = "Раскрой"
    return f"{name}.dxf"


def export_dxf(plan):
    document = ezdxf.new("R2000", units=4)
    document.encoding = "cp1251"
    document.header["$MEASUREMENT"] = 1
    modelspace = document.modelspace()
    layers = {}
    reserved = {name.casefold() for name in ("0", "Defpoints")}
    for contour in plan.contours:
        source = contour.layer
        if source not in layers:
            candidate = source if len(source) <= 100 and is_valid_layer_name(source) else "Контур"
            if source != "0":
                base = candidate
                sequence = 1
                while candidate.casefold() in reserved:
                    sequence += 1
                    candidate = f"{base}_{sequence}"
                reserved.add(candidate.casefold())
                document.layers.new(candidate)
            layers[source] = candidate
        attributes = {"layer": layers[source]}
        if len(contour.points) == 2:
            modelspace.add_line(*contour.points, dxfattribs=attributes)
        else:
            modelspace.add_lwpolyline(contour.points, format="xy", close=contour.closed, dxfattribs=attributes)
    with io.StringIO() as output:
        document.write(output)
        return output.getvalue().encode(document.output_encoding, errors="dxfreplace")


def single_page_xiao_settings(executable, machine_ip):
    try:
        data = executable.with_name("xparam.dat").read_bytes()
        if len(data) != 3704:
            return None
        # Xiao 2022-09-18: prepared XY, paged X, one head, no page override/offsets.
        expected = {2132: 1, 2444: 1, 2516: 0, 2532: 0, 3152: 0, 3524: CONTROLLER_PORT}
        if any(unpack_from("<i", data, offset)[0] != value for offset, value in expected.items()):
            return None
        if unpack_from("<dd", data, 2476) != (0.0, 0.0):
            return None
        configured_ip = str(ipaddress.IPv4Address(bytes(unpack_from("<4i", data, 2092))))
        if configured_ip != machine_ip:
            return None
        page_add = unpack_from("<d", data, 2540)[0]
        return page_add if math.isfinite(page_add) and page_add >= 0 else None
    except (OSError, ValueError):
        return None


class PlotterState:
    def __init__(self, profile, workspace, workspace_lock, *, demo=False, central=False, companion=False, demo_cut_id=None):
        config = profile["observedComputerConfiguration"]
        board = profile.get("observedControllerConfiguration", {})
        bounds = board.get("workrectMm", config["workrectMm"])
        width, height = bounds["x"], bounds["y"]
        self.demo_mode = demo
        self.demo_cut_id = demo_cut_id
        self.central_mode = central
        self.companion_mode = companion
        self.simulator = Simulator(width, height)
        self.machine_ip = None if demo or central else str(ipaddress.IPv4Address(config["machineIp"]))
        self.xiao_path = Path(profile["software"]["path"])
        self.workspace = workspace
        self.workspace_lock = workspace_lock
        self.selected_cut = None
        self.releasable_cut = None
        self.upload_lock = Lock()
        self.motion_start_lock = Lock()
        self.loaded_plan = None
        self.roll = None
        self.cut_progress = None
        self.label_run = None
        self.label_thresholds = {}
        self.label_position = None
        self.label_monitor = None
        self.label_shutdown = Event()
        self.last_live_read = 0
        self.cut_journal = CutJournal(None if demo else workspace.path.with_name("cut-journal.json"))
        if demo:
            self.controller = DemoController(width, height)
            return
        if central:
            self.controller = OfflineController()
            return
        self.controller = Controller(self.machine_ip, width, height,
                                     upload_profile=lambda: single_page_xiao_settings(self.xiao_path, self.machine_ip),
                                     z_travel=board.get("zTravelMm", 0),
                                     auto_run_readback=AutoRunReadback.from_profile(board.get("autoRunReadback")),
                                     manual_speed_readback=PanelReadback.from_profile(
                                         board.get("manualSpeedReadback"), {"fastValue": "fast", "slowValue": "slow"}))

    @property
    def default_fabric_width(self):
        return min(DEFAULT_FABRIC_WIDTH_MM, self.simulator.height)

    def status(self, simulation=None):
        with self.workspace_lock:
            cut_status = self.cut_status()
        page_add = self.controller.upload_page_add()
        return {
            "mode": "controller",
            "appVersion": APP_VERSION,
            "apiVersion": API_VERSION,
            "demoAvailable": True,
            "demoMode": self.demo_mode,
            "centralMode": self.central_mode,
            "companionMode": self.companion_mode,
            "companionProtocol": COMPANION_PROTOCOL,
            "nestingKeepsNotches": True,
            "nestingPartRotation": True,
            "pageRouteOptimization": True,
            "prepareXiao": True,
            **cut_status,
            "machine": {
                "ip": self.machine_ip,
                "width": self.simulator.width,
                "height": self.simulator.height,
                "defaultFabricWidthMm": self.default_fabric_width,
                "rollPassLengthMm": self.roll_pass_length(page_add),
                "headCount": 1,
                "directControl": not self.central_mode or self.demo_mode,
                "directUpload": page_add is not None,
                "directRoll": True,
                "continuousRoll": True,
                "automaticRollFeed": self.demo_mode,
                "rollHeadPositioning": not (self.demo_mode or self.central_mode),
                "liveCutProgress": True,
                "stopDuringExternalRun": True,
                "arbitraryNestRotation": True,
                "optimizedNesting": True,
                "verifiedJobUpload": True,
                "persistentControl": True,
                "coordinateMove": True,
                "panelDiagnostics": True,
                "actuators": self.controller.actuator_snapshot(),
                "clampControl": True,
                "independentClampControl": False,
                "independentVacuumControl": True,
                "autoRunReadback": self.controller.auto_run_readback is not None,
                "uploadUncertain": self.controller.upload_uncertain,
                "uploadInProgress": self.controller.upload_in_progress,
                "controllerPort": CONTROLLER_PORT,
                "holdLeaseMs": int(HOLD_LEASE_SECONDS * 1000),
                "xiaoAvailable": not self.demo_mode and not self.central_mode
                                 and (self.xiao_path.is_file() or LOCALIZED_XIAO.is_file()),
                "dwgAvailable": dwg_available(),
            },
            "simulation": simulation if simulation is not None else self.simulator.snapshot(),
        }

    def roll_snapshot(self):
        roll = self.roll
        return roll.snapshot() if roll else None

    def cut_status(self):
        return {"roll": self.roll_snapshot(), "selectedCut": self.selected_cut,
                "cutProgress": self.cut_progress.snapshot() if self.cut_progress else None,
                "cutJournal": self.cut_journal.snapshot(), "canReleaseCut": self.can_release_cut()}

    def can_release_cut(self):
        return (not (self.demo_mode or self.central_mode or self.companion_mode)
                and self.selected_cut is not None and self.selected_cut == self.releasable_cut
                and all(state is None for state in (self.loaded_plan, self.cut_progress, self.roll)))

    def record_cut_event(self, progress, kind, message):
        event = self.cut_journal.record(progress.filename, progress.page, kind, message)
        return replace(progress, events=(*progress.events, event)[-MAX_EVENTS:])

    @staticmethod
    def page_finished(page, result):
        run = result.get("run", {})
        return (
            result.get("responded") is True and not result.get("busy") and result.get("position")
            and not result.get("motionInhibited") and run.get("fresh") is True and run.get("state") == "completed"
            and result.get("lastEvent", {}).get("code") not in ACTIVE_CONTROLLER_EVENTS
            and type(page.started_at) is int and run.get("startedAt") == page.started_at
            and type(run.get("endedAt")) is int and run["endedAt"] >= page.started_at
            and run.get("completedPages") == 1
            and (run.get("completedIndex") == page.points or type(run.get("index")) is int)
            and run.get("indexLimit") == page.points
        )

    def observe_cut_progress(self, result):
        progress = self.cut_progress
        if not progress or progress.state == "transferring":
            return
        run = result.get("run", {})
        state = run.get("state")
        started_at = run.get("startedAt")
        fresh = (result.get("responded") is True and not result.get("busy")
                 and bool(result.get("position")) and run.get("fresh") is True)
        if progress.state in ("completed", "advance", "cancelled"):
            self.cut_progress = replace(progress, fresh=fresh, updated_at=run.get("updatedAt", progress.updated_at))
            return

        if progress.started_at is None and state != "idle":
            if (type(started_at) is int and progress.receipt and
                    run.get("startedReceipt") == progress.receipt):
                progress = replace(progress, started_at=started_at)
            elif self.roll and self.roll.state == "unknown" and progress.state == "ready":
                progress = replace(progress, state="unknown")
            elif progress.state == "ready" and fresh and result.get("jobReceipt") == progress.receipt:
                self.cut_progress = replace(progress, fresh=True, updated_at=run.get("updatedAt"))
                return
        if progress.started_at is None or started_at != progress.started_at:
            self.cut_progress = replace(progress, fresh=False)
            return
        if run.get("startedReceipt") not in (None, progress.receipt):
            self.cut_progress = replace(progress, fresh=False)
            return

        index = run.get("completedIndex")
        proof = progress.index
        if fresh and run.get("indexLimit") == progress.points and type(index) is int and 0 <= index <= progress.points:
            proof = max(proof, min(index, progress.points - 1))
        progress = replace(progress, index=proof, fresh=fresh, updated_at=run.get("updatedAt"))
        if fresh and state in ("running", "paused", "completed") and not any(
                event["kind"] == "page_started" and event["page"] == progress.page for event in progress.events):
            progress = self.record_cut_event(progress, "page_started", f"Начат рез прохода {progress.page}.")
        if self.page_finished(progress, result):
            complete = progress.page == progress.pages
            progress = replace(progress, state="completed" if complete else "advance", index=progress.points,
                               ended_at=run["endedAt"], completed_pages=progress.completed_pages + 1, route=None)
            progress = self.record_cut_event(progress, "page_completed", f"Проход {progress.page} завершён.")
            if complete:
                message = f"Раскрой завершён. Проходов: {progress.completed_pages}."
                progress = self.record_cut_event(progress, "cut_completed", message)
        elif state == "cancelled":
            progress = replace(progress, state=state, ended_at=run.get("endedAt"), route=None)
            progress = self.record_cut_event(progress, "cut_cancelled", f"Рез прохода {progress.page} отменён.")
        elif state in ("starting", "running", "pausing", "paused", "resuming", "feeding", "cancelling", "unknown", "error"):
            if state in ("unknown", "error") and progress.state not in ("unknown", "error"):
                progress = self.record_cut_event(
                    progress, "cut_uncertain", f"Состояние реза прохода {progress.page} требует сверки на пульте.",
                )
            progress = replace(progress, state=state)
        self.cut_progress = progress
        self.observe_labels(progress)

    def observe_labels(self, progress):
        if self.label_run and progress.state == "cancelled":
            self.close_label_run()
            return
        if not self.label_run or not progress.fresh or progress.started_at is None:
            return
        if progress.state not in ("running", "paused", "pausing", "resuming", "advance", "completed"):
            return
        index = bisect_right(self.label_thresholds.get(progress.page, ()), progress.index)
        completed = progress.state == "completed"
        position = (progress.page, index, completed)
        if position != self.label_position:
            try:
                self.labels.observe(self.label_run, progress.page, progress.index, complete=completed,
                                    completed_ms=progress.ended_at)
                self.label_position = position
            except Exception:
                LOGGER.exception("Не удалось обновить очередь бирок")

    def watch_label_progress(self):
        # A closed browser must not leave verified pieces unlabelled. Use the usual
        # readback if the operator UI is not already reading from the controller.
        while not self.label_shutdown.wait(0.5):
            progress = self.cut_progress
            if (self.label_run and progress and progress.state not in ("cancelled", "advance", "completed")
                    and monotonic() - self.last_live_read >= 1):
                try:
                    self.read_live()
                except Exception:
                    LOGGER.exception("Не удалось прочитать состояние реза для бирок")

    def register_label_run(self, source, preview, previous, previous_progress):
        cut, revision = source["cutContext"]
        # Reloading a ready page and advancing a roll retain the same physical run.
        if self.label_run and (previous or previous_progress and previous_progress.state == "ready"
                               and previous_progress.plan_id == preview["planId"]):
            return
        ends = part_ends(source["paths"], preview["pages"])
        run = self.labels.begin(cut, "demo" if self.demo_mode else "operator", revision,
                                self.cut_progress.first_page, ends)
        self.label_run, self.label_position = run, None
        self.label_thresholds = {}
        for _, _, _, page, vertex in ends:
            self.label_thresholds.setdefault(page, []).append(vertex)
        for vertices in self.label_thresholds.values():
            vertices.sort()
        if self.label_monitor is None:
            self.label_monitor = Thread(target=self.watch_label_progress, name="label-cut-observer", daemon=True)
            self.label_monitor.start()

    def verify_roll_advance(self, *, positioning=False):
        # Completion was already recorded before the operator moved the fabric.
        # Check that the machine is still ready even if releasing the material reset the job index.
        result = self.controller.read_live()
        run = result.get("run", {})
        if (result.get("responded") is not True or result.get("busy") or not result.get("position")
                or result.get("motionInhibited") or result.get("externalMotion")
                or run.get("fresh") is not True or run.get("state") not in ("idle", "ready", "completed", "cancelled")
                or (positioning and run.get("followupFeedPending"))
                or result.get("lastEvent", {}).get("code") in ACTIVE_CONTROLLER_EVENTS):
            raise RequestError("Контроллер не подтвердил готовность к следующему проходу. Сверьте станок на пульте.",
                               HTTPStatus.CONFLICT)

    def bind_panel_run(self, roll, run):
        receipt = run.get("startedReceipt")
        started_at = run.get("startedAt")
        if (run.get("startedFromPanel") is True and isinstance(receipt, str) and type(started_at) is int
                and self.loaded_plan == (receipt, roll.plan_id, roll.page)):
            self.loaded_plan = None
            self.roll = replace(roll, state="running", started_at=started_at)
        return self.roll

    def observe_ready_roll(self, roll, result, run):
        if not result.get("responded") or result.get("busy") or not run.get("fresh"):
            return roll
        adopted = self.bind_panel_run(roll, run)
        if adopted is not roll:
            return adopted

        event = result.get("lastEvent", {}).get("code")
        index = result.get("currentIndex")
        has_progress = result.get("indexLimit") == roll.points and type(index) is int and index > 0
        new_completion = event == 4 and roll.baseline_event not in (None, 4)
        if event in (3, 9) or has_progress or new_completion:
            self.loaded_plan = None
            self.roll = replace(roll, state="unknown")
        elif type(event) is int and event not in ACTIVE_CONTROLLER_EVENTS and event != roll.baseline_event:
            self.roll = replace(roll, baseline_event=event)
        return self.roll

    def observe_roll(self, result):
        roll = self.roll
        if roll is None:
            return
        if roll.state == "advance" and roll.head_positioning == "moving":
            move = result.get("coordinateMove") or {}
            state = move.get("state") if (move.get("x"), move.get("y")) == roll.head_move_target else None
            if state != "moving":
                self.roll = replace(roll, head_positioning="reached" if state == "reached" else "unconfirmed",
                                    head_move_target=None)
            return
        run = result.get("run", {})
        if roll.state == "ready":
            roll = self.observe_ready_roll(roll, result, run)
        if (roll.state not in ("running", "unknown") or type(roll.started_at) is not int
                or run.get("startedAt") != roll.started_at):
            return
        if self.page_finished(roll, result):
            state = "completed" if roll.page == roll.pages else "advance"
            head_positioning = "pending" if state == "advance" and not self.demo_mode else None
            self.roll = replace(roll, state=state, completed_pages=roll.completed_pages + 1,
                                head_positioning=head_positioning)
        elif roll.state == "unknown" and run.get("fresh") and run.get("state") in ("running", "paused", "feeding"):
            self.roll = replace(roll, state="running")
        elif run.get("state") in ("cancelled", "unknown", "error"):
            state = "cancelled" if run["state"] == "cancelled" else "unknown"
            if roll.state != state:
                self.roll = replace(roll, state=state)

    def verify_ready_roll(self):
        result = self.controller.read_live()
        self.observe_roll(result)
        if (self.roll.state != "ready" or result.get("responded") is not True or result.get("busy")
                or not result.get("position") or result.get("motionInhibited")
                or not result.get("run", {}).get("fresh")
                or result.get("lastEvent", {}).get("code") in ACTIVE_CONTROLLER_EVENTS):
            raise RequestError("Контроллер не подтвердил готовность текущего прохода. Проверьте станок на пульте.",
                               HTTPStatus.CONFLICT)

    @staticmethod
    def _live_route_position(progress, result):
        run = result.get("run", {})
        index = progress.index
        # The next vertex can be a target rather than a completed point.
        if (not progress.route or not progress.receipt or run.get("startedReceipt") != progress.receipt
                or run.get("startedAt") != progress.started_at or run.get("indexRegressed") or result.get("motionInhibited")
                or result.get("lastEvent", {}).get("code") not in EXTERNAL_CUT_EVENTS
                or run.get("state") not in ("running", "pausing", "paused", "resuming")
                or run.get("indexLimit") != progress.route.point_count or not 1 <= index < progress.route.point_count
                or type(run.get("completedIndex")) is not int or run["completedIndex"] != index
                or run.get("index") != index + 1):
            return None

        start, _ = progress.route.vertex(index)
        target, drawing = progress.route.vertex(index + 1)
        position = result["position"]
        measured = position["x"], position["y"]
        dx, dy = target[0] - start[0], target[1] - start[1]
        distance_squared = dx * dx + dy * dy
        point, source = start, "completed-index"
        if distance_squared:
            fraction = ((measured[0] - start[0]) * dx + (measured[1] - start[1]) * dy) / distance_squared
            fraction = min(1, max(0, fraction))
            projection = start[0] + fraction * dx, start[1] + fraction * dy
            # Use the position sample only while it agrees with the current encoded segment to board resolution.
            if math.dist(measured, projection) <= 2 / TICKS_PER_MM:
                point, source = projection, "controller-position"
        return {"planId": progress.plan_id, "page": progress.page, "index": index,
                "x": progress.start_x + point[0] - COORDINATE_OFFSET_MM, "y": point[1] - COORDINATE_OFFSET_MM,
                "kind": "cut" if drawing else "travel", "source": source}

    def read_live(self):
        self.last_live_read = monotonic()
        expected_roll = self.roll
        expected_progress = self.cut_progress
        result = self.controller.read_live()
        with self.workspace_lock:
            if self.roll is expected_roll:
                self.observe_roll(result)
            if self.cut_progress is expected_progress:
                self.observe_cut_progress(result)
                progress = self.cut_progress
                if progress and progress.state == "completed":
                    self.observe_labels(progress)
                if (result.get("position") and not result.get("busy") and progress and progress.fresh and progress.state in
                        ("ready", "starting", "running", "pausing", "paused", "resuming", "cancelling")):
                    position = result["position"]
                    result["drawingPosition"] = {
                        "planId": progress.plan_id, "page": progress.page,
                        "x": progress.start_x + position["x"] - COORDINATE_OFFSET_MM,
                        "y": position["y"] - COORDINATE_OFFSET_MM,
                    }
                    if not self.demo_mode and (route_position := self._live_route_position(progress, result)):
                        result["routePosition"] = route_position
            result.update(self.cut_status())
        return result

    def reset_roll(self, data):
        if data.get("stopped") is not True:
            raise RequestError("Подтвердите остановку станка на штатном пульте.")
        self.controller.require_control(data.get("controlToken"))
        with self.upload_lock, self.workspace_lock:
            self.controller.require_coordinate_move_idle()
            roll = self.roll
            if not roll or data.get("planId") != roll.plan_id:
                raise RequestError("Рулонное задание изменилось.", HTTPStatus.CONFLICT)
            result = self.controller.read_live()
            self.observe_roll(result)
            run = result.get("run", {})
            transfer_error = run.get("state") == "error" and run.get("startedAt") is None
            if (not result.get("responded") or result.get("busy") or not result.get("position")
                    or result.get("motionInhibited") or not run.get("fresh")
                    or (not transfer_error and run.get("state") not in ("idle", "ready", "completed", "cancelled"))
                    or result.get("lastEvent", {}).get("code") in ACTIVE_CONTROLLER_EVENTS):
                raise RequestError("Контроллер не подтвердил остановку. Сверьте состояние станка на пульте.")
            self.controller.require_control(data.get("controlToken"))
            self.clear_cut()
            return self.cut_status()

    def clear_cut(self):
        self.close_label_run()
        self.roll = None
        self.loaded_plan = None
        self.cut_progress = None

    def close_label_run(self):
        if self.label_run:
            try:
                self.labels.abandon(self.label_run)
            except Exception:
                LOGGER.exception("Не удалось закрыть очередь бирок для раскроя")
            self.label_run = self.label_position = None

    def owns_drawing(self):
        return (self.loaded_plan is not None and self.cut_progress is None
                or any(cut is not None and cut.state not in ("completed", "cancelled")
                       for cut in (self.roll, self.cut_progress)))

    def verify_new_cut_idle(self, result):
        run = result.get("run", {})
        move = result.get("coordinateMove") or {}
        uncertain_upload = self.controller.upload_uncertain is True or result.get("uploadUncertain") is True
        owns_drawing = self.owns_drawing()
        transfer_error = run.get("state") == "error" and run.get("startedAt") is None and not owns_drawing
        safe_run = run.get("state") in (None, "idle", "ready", "completed", "cancelled") or transfer_error
        idle = (not result.get("busy") and not result.get("motionInhibited") and not result.get("externalMotion")
                and not result.get("stopAvailable") and not result.get("manualMovement")
                and self.controller.upload_in_progress is not True
                and result.get("lastEvent", {}).get("code") not in ACTIVE_CONTROLLER_EVENTS
                and move.get("state") in (None, "reached") and safe_run)
        if result.get("responded") is True:
            idle = idle and result.get("position") and run.get("fresh") is True and (not uncertain_upload or not owns_drawing)
        else:
            idle = idle and not self.demo_mode and not owns_drawing and not uncertain_upload
        if not idle:
            raise RequestError("Новый раскрой доступен после остановки и проверки текущего задания.", HTTPStatus.CONFLICT)

    def plan(self, job):
        position = self.simulator.snapshot()
        return build_plan(job, self.simulator.width, self.simulator.height, (position["x"], position["y"]))

    @staticmethod
    def workspace_drawing(draft):
        if draft is None:
            raise RequestError("Сохраните текущую раскладку перед загрузкой.", HTTPStatus.CONFLICT)
        excluded = set(draft["excluded"])
        optimization = draft["optimization"]
        job = {
            "paths": [path for index, path in enumerate(draft["paths"]) if index not in excluded],
            "optimize": optimization.get("routeApplied", True),
        }
        nesting = optimization.get("nesting")
        if nesting is not None:
            if nesting["widthMm"] is not None:
                job["materialWidthMm"] = nesting["widthMm"]
            job["materialMarginMm"] = nesting["marginMm"]
        return job, draft["source"]["filename"]

    def read_production(self, cut_id, mode):
        if isinstance(self.workspace, CompanionWorkspace):
            return self.workspace._request(f"/api/operator-workspace?cutId={key(cut_id)}&mode={mode}")
        return self.production.workspace(cut_id, mode)

    def production_progress(self, cut_id, action, mode, revision=None):
        if isinstance(self.workspace, CompanionWorkspace):
            return self.workspace.production_request("/api/operator-workspace", {
                "cutId": cut_id, "action": action, "mode": mode, "revision": revision,
            })
        return self.production.progress(cut_id, action, mode, revision)

    def current_drawing(self, cut_id=None, *, for_machine=False, for_export=False):
        if for_machine and cut_id != self.selected_cut:
            raise WorkspaceConflict("На плоттере открыт другой раскрой. Откройте нужный пункт заново.")
        if cut_id is None:
            return self.workspace_drawing(self.workspace.read()["workspace"])
        record = self.read_production(cut_id, "demo" if self.demo_mode else "operator")
        if for_machine and not self.demo_mode and record["status"] != "active":
            raise WorkspaceConflict("Пункт наряда недоступен для запуска.")
        draft = record["workspace"]
        if for_machine or for_export:
            if not self.demo_mode and "widthMm" in record and record["widthMm"] is None:
                raise WorkspaceConflict("Укажите ширину полотна и сохраните раскладку всех деталей.")
            self.verify_production_layout(draft, draft["optimization"].get("nesting", {}).get("widthMm"))
        job, filename = self.workspace_drawing(draft)
        if for_machine and isinstance(self.workspace, CompanionWorkspace) and not self.demo_mode:
            self.workspace.production_request("/api/operator-workspace", {"action": "verify", "mode": "operator",
                "cutId": cut_id, "cutRevision": record["cutRevision"]})
        job["cutContext"] = [cut_id, record["cutRevision"]]
        return job, filename

    def verify_production_layout(self, draft, width):
        if width is None or width > self.simulator.height:
            raise ValueError("Ширина полотна превышает ширину рабочего поля плоттера.")
        verify_arrangement(draft, width)
        job, filename = self.workspace_drawing(draft)
        plan = self.cut_plan(job, filename)
        if not plan["canPrepareXiao"]:
            raise ValueError(plan["issues"][0]["message"] if plan["issues"] else "Раскрой не содержит проходов.")
        material = plan["material"]
        return {**{field: material[field] for field in ("lengthMm", "areaMm2", "partsAreaMm2", "utilizationPercent")},
                **{field: plan[field] for field in ("cutDistanceMm", "travelDistanceMm", "estimatedWorkSeconds",
                                                    "cutSpeedMmS", "travelSpeedMmS")},
                "approximate": bool(material["openContourCount"])}

    def change_cut_context(self, data):
        cut_id = data.get("cutId")
        if cut_id is not None:
            key(cut_id)
        if self.demo_cut_id is not None and cut_id != self.demo_cut_id:
            raise WorkspaceConflict("Выбран другой материал демораскроя.")
        action = data.get("action")
        resume_only = data.get("resumeOnly", False)
        if type(resume_only) is not bool:
            raise ValueError("Проверьте режим возобновления демораскроя.")
        if action not in ("select", "finish", "release") or action != "select" and (cut_id is None or self.demo_mode):
            raise ValueError("Выберите допустимое действие для раскроя.")
        with request_lock(self.motion_start_lock, "Дождитесь текущего действия."), \
                request_lock(self.upload_lock, "Дождитесь завершения передачи."), self.workspace_lock:
            if resume_only and (not self.demo_mode or self.demo_cut_id is not None or action != "select"
                                or cut_id is None or cut_id != self.selected_cut):
                raise WorkspaceConflict("Прежняя демосессия больше не открыта для этого материала.")
            if action == "release":
                if cut_id != self.selected_cut or not self.can_release_cut():
                    raise WorkspaceConflict("Вернуть в очередь можно только открытый здесь раскрой, ещё не переданный на станок.")
                live = self.controller.read_live()
                if (live.get("responded") is not True or live.get("run", {}).get("state") != "idle"
                        or self.controller.upload_uncertain is True or live.get("uploadUncertain") is True):
                    raise WorkspaceConflict("Контроллер не подтвердил, что плоттер свободен.")
                self.verify_new_cut_idle(live)
                result = self.production.release_cut(cut_id, data.get("revision"))
                self.selected_cut = self.releasable_cut = None
                return {**result, **self.cut_status()}
            if action == "finish" and cut_id != self.selected_cut:
                record = self.read_production(cut_id, "operator")
                if record["status"] == "done":
                    return {**record, **self.cut_status()}
                raise WorkspaceConflict("Этот раскрой сейчас не открыт на плоттере.")
            if action == "select" and cut_id is not None and cut_id == self.selected_cut:
                result = self.production_progress(cut_id, "start", "demo" if self.demo_mode else "operator")
                return {**result, **self.cut_status()}
            # A roll which has started can still contain uncut material after a page finishes.
            if self.roll and self.roll.state not in ("completed", "cancelled") and self.roll.completed_pages:
                raise WorkspaceConflict("Завершите или сбросьте текущий раскрой на плоттере.")
            if action == "finish" and self.cut_progress and self.cut_progress.state not in ("completed", "cancelled"):
                raise WorkspaceConflict("Дождитесь окончания резки или отмените загруженный раскрой.")
            self.verify_new_cut_idle(self.controller.read_live())
            if action == "finish":
                result = self.production_progress(cut_id, "finish", "operator", data.get("revision"))
            elif cut_id:
                result = self.production_progress(cut_id, "start", "demo" if self.demo_mode else "operator")
            else:
                result = {}
            self.selected_cut = cut_id if action == "select" else None
            self.releasable_cut = cut_id if action == "select" and result.get("opened") else None
            self.clear_cut()
            return {**result, **self.cut_status()}

    def invalidate_loaded_plan(self, workspace):
        if self.loaded_plan is None or self.selected_cut is not None:
            return
        current = None
        if workspace is not None:
            job, filename = self.workspace_drawing(workspace)
            current = drawing_key(job, filename, self.simulator.width, self.simulator.height,
                                  self.controller.upload_page_add(), roll=self.roll is not None)
        if current != self.loaded_plan[1]:
            self.loaded_plan = None

    def cut_plan(self, job, filename, page_add=None, *, encoded_pages=None):
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise RequestError("Укажите имя текущего чертежа.")
        width, height = self.simulator.width, self.simulator.height
        plan = prepare_cut_plan(job, width, height, PREVIEW_PAGE_ADD_MM if page_add is None else page_add,
                                roll_pass_length_mm=ROLL_PASS_LENGTH_MM)
        plan["planId"] = drawing_key(job, filename, width, height, page_add, roll=plan["mode"] == "roll")
        if self.companion_mode or self.central_mode:
            plan["exportPlanId"] = drawing_key(job, filename, width, height, None, roll=True)
        plan["filename"] = filename
        plan["tool"] = CUT_TOOL
        plan["canPrepareXiao"] = bool(plan["pages"]) and not plan["issues"]
        plan["canUpload"] = False
        describe_section_continuations(plan, job)
        if page_add is None or not plan["canPrepareXiao"]:
            return plan
        for page in plan["pages"]:
            if not page["paths"] and not page.get("wasteTrim", {}).get("paths"):
                plan["uploadIssue"] = (f"На проходе {page['number']} нет резов. Сдвиньте детали ближе к началу "
                                       "раскладки или откройте раскладку в Xiao.")
                return plan
            try:
                encoded = encode_cut_section(page, width, height, page_add)
            except (PlotterError, JobEncodingError) as error:
                plan["uploadIssue"] = f"Проход {page['number']} нельзя загрузить. {error}"
                return plan
            ends = encoded[0].path_end_indices
            part_paths = len(page["paths"])
            page["pathEndIndices"] = list(ends[:part_paths])
            if "wasteTrim" in page:
                page["wasteTrim"]["pathEndIndices"] = list(ends[part_paths:])
            if encoded_pages is not None:
                encoded_pages.append(encoded)
        plan["canUpload"] = True
        return plan

    def prepare_xiao(self, plan_id, *, shared_export=False, cut_id=None):
        if not isinstance(plan_id, str) or re.fullmatch(r"[0-9a-f]{64}", plan_id) is None:
            raise RequestError("Сначала подготовьте раскладку для Xiao.")
        page_add = self.controller.upload_page_add()
        with self.workspace_lock:
            source, filename = self.current_drawing(cut_id, for_export=True)
            preview = self.cut_plan(source, filename, page_add)
            if preview["exportPlanId" if shared_export else "planId"] != plan_id:
                raise RequestError("Сохранённая раскладка изменилась. Подготовьте её заново.", HTTPStatus.CONFLICT)
            if not preview["canPrepareXiao"]:
                message = preview["issues"][0]["message"] if preview["issues"] else "В раскладке нет контуров."
                raise RequestError(message)

        # Xiao calculates its own section boundaries. Give it unbroken paths
        # whenever a browser section would split a contour.
        paths = source["paths"] if preview["seams"] else [
            path for page in preview["pages"] for path in page["paths"]
        ]
        width = max(self.simulator.width, preview["bounds"]["maxX"])
        plan = build_plan({"paths": paths}, width, self.simulator.height)
        return output_name(filename), export_dxf(plan)

    def roll_pass_length(self, page_add):
        allowance = PREVIEW_PAGE_ADD_MM if page_add is None else page_add
        return min(ROLL_PASS_LENGTH_MM, self.simulator.width - max(allowance, COORDINATE_OFFSET_MM))

    def nest(self, data):
        requested_width = data.get("widthMm")
        width = self.default_fabric_width if requested_width is None else number(
            requested_width, "Ширина полотна, мм", 1, self.simulator.height,
        )
        gap = number(data.get("gapMm", 10), "Зазор между деталями, мм", 0, 100)
        margin = number(data.get("marginMm", 10), "Отступ от края, мм", 0, 100)
        return nest_paths(
            data.get("paths"), width_mm=width, gap_mm=gap, margin_mm=margin, rotation=data.get("rotation", "none"),
            page_length_mm=self.roll_pass_length(self.controller.upload_page_add()), length_mm=data.get("lengthMm"),
        )

    def upload(self, data):
        with request_lock(self.upload_lock, "Дождитесь завершения передачи задания."):
            self.controller.require_coordinate_move_idle()
            return self.upload_page(data)

    def upload_page(self, data):
        control_token = data.get("controlToken")
        self.controller.require_control(control_token)
        auto_run_off = data.get("autoRunOff")
        if self.controller.auto_run_readback is None and auto_run_off is not True:
            raise RequestError("Перед загрузкой подтвердите, что Auto Run выключен на штатном пульте.")
        page_add = self.controller.upload_page_add()
        if page_add is None:
            raise RequestError("Сохранённый профиль Xiao не подходит для прямой передачи одного листа.")
        with self.workspace_lock:
            source, filename = self.current_drawing(data.get("cutId"), for_machine=True)
            encoded_pages = []
            preview = self.cut_plan(source, filename, page_add, encoded_pages=encoded_pages)
            if preview["planId"] != data.get("planId"):
                raise RequestError("Сохранённая раскладка изменилась. Подготовьте её заново.", HTTPStatus.CONFLICT)
            if preview["issues"]:
                raise RequestError(preview["issues"][0]["message"])
            if not preview["canUpload"]:
                raise RequestError(preview.get("uploadIssue", "Раскладка недоступна для прямой загрузки."))
            page_number = data.get("page", 1)
            if type(page_number) is not int or not 1 <= page_number <= len(preview["pages"]):
                raise RequestError("Выберите допустимый проход раскладки.")
            page = preview["pages"][page_number - 1]
            is_roll = preview["mode"] == "roll"
            previous = self.roll
            if previous and (not is_roll or previous.plan_id != preview["planId"]):
                raise RequestError("Сначала завершите или сбросьте предыдущее рулонное задание.", HTTPStatus.CONFLICT)
            if previous is None and not page["canStart"]:
                raise RequestError("Этот проход продолжает деталь с предыдущих проходов. Начните с прохода, "
                                   "на котором деталь ещё не начата.", HTTPStatus.CONFLICT)
            if previous is None and page_number != 1 and data.get("advanceConfirmed") is not True:
                raise RequestError("Подтвердите положение начала выбранного прохода на станке.", HTTPStatus.CONFLICT)
            if previous and previous.snapshot()["nextPage"] != page_number:
                raise RequestError("Этот проход пока недоступен. Проверьте ход рулонного задания.", HTTPStatus.CONFLICT)
            auto_advanced = self.demo_mode and previous and previous.state == "advance" and previous.material_advanced
            advance_confirmed = auto_advanced or data.get("advanceConfirmed") is True
            if preview["hasContinuations"] and not advance_confirmed:
                raise RequestError("Подтвердите положение начала прохода у нуля станка.")
            if previous and previous.state == "advance":
                if not advance_confirmed:
                    raise RequestError("Подтвердите протяжку ткани и положение рабочего нуля.")
                self.verify_roll_advance()
            elif previous:
                self.verify_ready_roll()
                previous = self.roll
            job, finish = encoded_pages[page_number - 1]
            self.releasable_cut = None
            self.loaded_plan = None
            previous_progress = self.cut_progress
            first_page = previous.first_page if previous else page_number
            completed_pages = previous.completed_pages if previous else 0
            previous_events = previous_progress.events if previous and previous_progress else ()
            self.cut_progress = CutProgress(preview["planId"], filename, page_number, len(preview["pages"]),
                                            first_page, job.point_count, page["startX"], page["endX"],
                                            completed_pages=completed_pages, events=previous_events, route=job)
            if is_roll:
                self.roll = RollRun(preview["planId"], page_number, len(preview["pages"]), first_page,
                                    page["feedAfterMm"], "transferring", completed_pages, job.point_count)
        try:
            result = self.controller.upload_job(job, finish, page_add, control_token=control_token, filename=filename,
                                                auto_run_off=auto_run_off)
        except ValueError:
            with self.workspace_lock:
                if is_roll:
                    self.roll = replace(self.roll, state="unknown") if self.controller.upload_uncertain else previous
                self.cut_progress = replace(self.cut_progress, state="unknown") if self.controller.upload_uncertain else previous_progress
            raise
        except Exception:
            with self.workspace_lock:
                if is_roll:
                    self.roll = replace(self.roll, state="unknown")
                self.cut_progress = replace(self.cut_progress, state="unknown")
            raise
        LOGGER.info("Передача листа: %s, подтверждено точек %s, завершение отправлено %s",
                    result["phase"], result["points"], result["commitSent"])
        with self.workspace_lock:
            if result.get("jobReceipt"):
                self.loaded_plan = (result["jobReceipt"], preview["planId"], page_number)
                self.cut_progress = replace(self.cut_progress, receipt=result["jobReceipt"], state="ready")
                self.cut_progress = self.record_cut_event(
                    self.cut_progress, "page_ready", f"Проход {page_number} загружен.",
                )
                if source.get("cutContext"):
                    try:
                        self.register_label_run(source, preview, previous, previous_progress)
                    except Exception:
                        self.label_run = None
                        LOGGER.exception("Не удалось подготовить бирки для загруженного раскроя")
            elif result.get("uploadUncertain"):
                self.cut_progress = replace(self.cut_progress, state="unknown")
            else:
                self.cut_progress = previous_progress
            if is_roll:
                if result.get("jobReceipt"):
                    self.roll = replace(self.roll, state="ready", baseline_event=result.get("baselineEvent", {}).get("code"))
                elif result.get("uploadUncertain"):
                    self.roll = replace(self.roll, state="unknown")
                else:
                    self.roll = previous
            result.update(filename=filename, planId=preview["planId"], page=page_number, **self.cut_status())
        return result

    def operate(self, data):
        if data.get("action") in ("pause-cut", "cancel-cut"):
            return self.operate_controller(data)
        with request_lock(self.motion_start_lock, "Дождитесь завершения текущего действия."):
            return self.operate_controller(data)

    def hold(self, data):
        action = data.get("action")
        args = (action, data.get("session"), data.get("axis"), data.get("direction"),
                data.get("token"), data.get("ready"), data.get("controlToken"))
        if action == "start":
            with request_lock(self.motion_start_lock, "Дождитесь завершения текущего действия."):
                return self.controller.hold(*args)
        return self.controller.hold(*args)

    def advance_roll_material(self, data, control_token):
        with request_lock(self.upload_lock, "Дождитесь завершения передачи задания."), self.workspace_lock:
            roll = self.roll
            if (roll is None or roll.state != "advance" or data.get("planId") != roll.plan_id
                    or type(data.get("page")) is not int or data["page"] != roll.page + 1):
                raise RequestError("Следующий проход ещё не готов к протяжке.", HTTPStatus.CONFLICT)
            self.controller.require_control(control_token)
            if (not self.demo_mode and (roll.head_positioning == "moving" or
                    (data.get("automatic") is True and roll.head_positioning != "pending"))):
                return {**self.cut_status(), "feedMm": roll.feed_after_mm}
            job, filename = self.current_drawing(data.get("cutId"), for_machine=True)
            page_add = self.controller.upload_page_add()
            current = drawing_key(job, filename, self.simulator.width, self.simulator.height, page_add, roll=True)
            if current != roll.plan_id:
                raise RequestError("Сохранённая раскладка изменилась. Подготовьте её заново.", HTTPStatus.CONFLICT)
            if roll.material_advanced:
                return {**self.cut_status(), "feedMm": roll.feed_after_mm}
            if not 0 < roll.feed_after_mm <= self.roll_pass_length(page_add):
                raise RequestError("Раскладка не содержит допустимого расстояния протяжки.", HTTPStatus.CONFLICT)
            self.verify_roll_advance(positioning=not self.demo_mode)
            if self.demo_mode:
                result = self.controller.advance_material(roll.feed_after_mm, control_token=control_token)
                self.roll = replace(roll, material_advanced=True)
            else:
                # The next pass is measured from this pass's X=0. Keep the head's current Y.
                result = self.controller.move_x(roll.feed_after_mm, control_token=control_token)
                move = result["coordinateMove"]
                target = (move["x"], move["y"]) if move["state"] == "moving" else None
                self.roll = replace(roll, head_positioning=move["state"], head_move_target=target)
            return {**self.cut_status(), "feedMm": roll.feed_after_mm, **result}

    def operate_controller(self, data):
        action, token = data.get("action"), data.get("controlToken")
        if action == "advance-roll":
            return self.advance_roll_material(data, token)
        if action in ("cut", "range", "pause-cut", "resume-cut", "acknowledge-run"):
            self.controller.require_control(token)
        args = (action, data.get("token"), data.get("ready"), data.get("jobConfirmed"), data.get("recoverUpload"), token)
        kwargs = {"job_receipt": data.get("jobReceipt"), "job_verified": data.get("jobVerified")}
        if action == "move-to":
            with request_lock(self.upload_lock, "Дождитесь завершения передачи задания."):
                result = self.controller.operate(*args, **kwargs, x=data.get("x"), y=data.get("y"))
            with self.workspace_lock:
                result.update(self.cut_status())
            return result
        if action not in ("cut", "range"):
            if action == "cancel-cut":
                kwargs["confirmed_stop"] = True
            result = self.controller.operate(*args, **kwargs)
            with self.workspace_lock:
                if action == "cancel-cut" and result.get("sent") and self.roll and self.roll.state == "ready":
                    roll = self.roll
                    if self.bind_panel_run(roll, result.get("run", {})) is roll:
                        self.roll = replace(roll, state="unknown")
                        self.loaded_plan = None
                self.observe_cut_progress(result)
                result.update(self.cut_status())
            return result
        if action == "cut" and data.get("jobVerified") is not True:
            raise RequestError("Сверьте загруженный чертёж на штатном пульте.")
        with self.workspace_lock:
            job, filename = self.current_drawing(data.get("cutId"), for_machine=True)
            roll = self.roll
            page_number = roll.page if roll else 1
            current = drawing_key(job, filename, self.simulator.width, self.simulator.height,
                                  self.controller.upload_page_add(), roll=roll is not None)
            if (self.loaded_plan != (data.get("jobReceipt"), current, page_number) or data.get("planId") != current
                    or roll and (roll.state != "ready" or roll.plan_id != current)):
                raise RequestError("Текущая раскладка ещё не загружена на станок.", HTTPStatus.CONFLICT)
            if roll and action == "cut":
                self.verify_ready_roll()
                roll = self.roll
            result = self.controller.operate(*args, **kwargs)
            if action == "cut" and result.get("sent") and self.cut_progress:
                started_at = result.get("run", {}).get("startedAt")
                self.cut_progress = replace(self.cut_progress, state="starting" if type(started_at) is int else "unknown",
                                            started_at=started_at, fresh=False)
            if roll and action == "cut" and result.get("sent"):
                started_at = result.get("run", {}).get("startedAt")
                self.roll = replace(roll, state="running" if type(started_at) is int else "unknown", started_at=started_at)
                self.loaded_plan = None
            result.update(self.cut_status())
            return result

    def reachability(self):
        if self.demo_mode:
            return {"reachable": True, "ip": None, "message": "Подключён виртуальный плоттер."}
        is_windows = os.name == "nt"
        arguments = ["ping.exe", "-n", "1", "-w", "850"] if is_windows else ["ping", "-c", "1", "-W", "1"]
        try:
            process = subprocess.run(
                [*arguments, self.machine_ip], capture_output=True, timeout=2.5,
                creationflags=subprocess.CREATE_NO_WINDOW if is_windows else 0,
            )
            reachable = bool(re.search(rb"\bttl[= ]\s*\d+", process.stdout, re.I))
            message = "Сетевой узел ответил на ping." if reachable else "Ответ на ping не получен."
        except (OSError, subprocess.TimeoutExpired):
            reachable = False
            message = "Проверка ping недоступна или истекло время ожидания."
        return {"reachable": reachable, "ip": self.machine_ip, "message": message}

@dataclass
class DemoCutScope:
    plotter: PlotterState
    last_used: float
    requests: int = 0


class PlotterServer(PlotterState, ThreadingHTTPServer):
    def __init__(self, port, workspace_path=WORKSPACE_PATH, *, profile_path=None, public_origin=None,
                 companion_origin=None, central_ca=None, companion_installer=None):
        if public_origin and companion_origin:
            raise ValueError("Выберите один режим сервера.")
        profile_path = Path(profile_path) if profile_path is not None else PROFILE
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        self.public_origin = https_origin(public_origin) if public_origin else None
        self.companion_installer = None
        if self.public_origin and APP_VERSION:
            self.companion_installer = Path(companion_installer) if companion_installer else ROOT.parent / COMPANION_INSTALLER
        self.companion_origin = https_origin(companion_origin) if companion_origin else None
        self.local_companion_ui = central_ca is not None
        if self.local_companion_ui and not self.companion_origin:
            raise ValueError("Для центра сертификации укажите адрес центрального сервера.")
        workspace = (CompanionWorkspace(Path(workspace_path), self.companion_origin, central_ca) if self.companion_origin
                     else WorkspaceStore(workspace_path, shared=bool(self.public_origin)))
        workspace_lock = Lock()
        PlotterState.__init__(self, profile, workspace, workspace_lock, central=bool(self.public_origin),
                              companion=bool(self.companion_origin))
        self.demo = PlotterState(profile, workspace, workspace_lock, demo=True, central=self.central_mode)
        self.demo_profile = profile
        self.demo_scopes = {}
        self.demo_scopes_lock = Lock()
        self.demo_saves = set()
        self.legacy_demo_selection = None
        self.demo_scopes_closed = False
        self.production = self.demo.production = ProductionStore(Path(workspace_path).with_name("production.sqlite3"))
        self.labels = self.demo.labels = LabelQueue(
            Path(workspace_path).with_name("labels.sqlite3"), self.label_bundle, local_spool=self.local_label_spooling,
            relay=self.label_auto_relay if isinstance(workspace, CompanionWorkspace) else None,
        )
        self.labels.resume_pending()
        self.production.configure_metrics(self.verify_production_layout)
        self.sketchup_design = os.name == "nt" and not (self.central_mode or self.companion_mode)
        self.sketchup_lock = Lock()
        self.sewing_pdf_lock = Lock()
        self.registry_archive_lock = Lock()
        self.fit_jobs = FitJobs()
        install_root = None
        if os.name == "nt" and self.local_companion_ui and os.environ.get("LOCALAPPDATA"):
            candidate = (Path(os.environ["LOCALAPPDATA"]) / "Plotter" / "Companion").resolve()
            if ROOT.parent == candidate and re.fullmatch(r"app-[a-f0-9]{32}", ROOT.name):
                install_root = candidate
        central_source = (workspace.opener, self.companion_origin) if install_root else None
        self.app_updates = AppUpdates(ROOT, APP_VERSION, install_root, central_source=central_source)
        ThreadingHTTPServer.__init__(self, (LOCAL_ADDRESS, port), Handler)
        self.hosts = {f"{host}:{self.server_port}" for host in (LOCAL_ADDRESS, "localhost")}
        if self.public_origin:
            self.hosts.add(urlsplit(self.public_origin).netloc)
            if urlsplit(self.public_origin).port is None:
                self.hosts.add(urlsplit(self.public_origin).netloc + ":443")

    @staticmethod
    def demo_scope_idle(scope):
        state = scope.plotter
        if scope.requests or not state.workspace_lock.acquire(blocking=False):
            return False
        try:
            if state.owns_drawing() or state.controller.control_is_armed():
                return False
            try:
                state.verify_new_cut_idle(state.controller.read_live())
            except RequestError:
                return False
            return True
        finally:
            state.workspace_lock.release()

    def evict_demo_scopes(self, now):
        evicted = []
        for cut_id, scope in sorted(self.demo_scopes.items(), key=lambda item: item[1].last_used):
            if now - scope.last_used < DEMO_CUT_IDLE_SECONDS and len(self.demo_scopes) < DEMO_CUT_CAPACITY:
                break
            if self.demo_scope_idle(scope):
                state = self.demo_scopes.pop(cut_id).plotter
                state.label_shutdown.set()
                evicted.append(state)
        return evicted

    def detach_empty_legacy_demo(self, cut_id):
        state = self.demo
        with ExitStack() as stack:
            for lock in (state.motion_start_lock, state.upload_lock, state.workspace_lock):
                if not lock.acquire(blocking=False):
                    return False
                stack.callback(lock.release)
            if state.selected_cut != cut_id:
                return True
            if state.roll or state.cut_progress or state.loaded_plan or state.controller.control_is_armed():
                return False
            try:
                state.verify_new_cut_idle(state.controller.read_live())
            except RequestError:
                return False
            state.selected_cut = state.releasable_cut = None
            return True

    def check_demo_cut_available(self, cut_id, *, takeover=False):
        if self.demo_scopes_closed:
            raise RequestError("Демостанок останавливается.", HTTPStatus.SERVICE_UNAVAILABLE)
        if (cut_id == self.legacy_demo_selection or cut_id == self.demo.selected_cut
                and not (takeover and self.detach_empty_legacy_demo(cut_id))):
            raise WorkspaceConflict("Этот материал открыт в прежней демосессии.", code="demo_legacy_cut")

    @contextmanager
    def select_legacy_demo(self, cut_id):
        key(cut_id)
        with self.demo_scopes_lock:
            scope = self.demo_scopes.get(cut_id)
            if (scope and (scope.plotter.selected_cut is None or monotonic() - scope.last_used >= DEMO_CUT_IDLE_SECONDS)
                    and self.demo_scope_idle(scope)):
                self.demo_scopes.pop(cut_id).plotter.label_shutdown.set()
                scope = None
            if scope:
                raise WorkspaceConflict("Этот материал открыт в другой демосессии.", code="demo_cut_in_use")
            if self.legacy_demo_selection is not None:
                raise WorkspaceConflict("Дождитесь открытия демораскроя.")
            self.legacy_demo_selection = cut_id
        try:
            yield
        finally:
            with self.demo_scopes_lock:
                self.legacy_demo_selection = None

    @contextmanager
    def scoped_demo(self, cut_id):
        evicted = []
        with self.demo_scopes_lock:
            scope = self.demo_scopes.get(cut_id)
            if scope:
                self.check_demo_cut_available(cut_id)
                scope.requests += 1
        if not scope:
            # Network validation on a companion must leave other virtual cutters accessible.
            self.read_production(cut_id, "demo")
            with self.demo_scopes_lock:
                scope = self.demo_scopes.get(cut_id)
                if scope:
                    self.check_demo_cut_available(cut_id)
                else:
                    if cut_id in self.demo_saves:
                        raise WorkspaceConflict("Дождитесь сохранения раскладки для этого материала.")
                    now = monotonic()
                    evicted = self.evict_demo_scopes(now)
                    if len(self.demo_scopes) >= DEMO_CUT_CAPACITY:
                        raise WorkspaceConflict("Все виртуальные станки заняты. Завершите один из демораскроев.")
                    self.check_demo_cut_available(cut_id, takeover=True)
                    state = PlotterState(self.demo_profile, self.workspace, Lock(), demo=True, central=self.central_mode,
                                         demo_cut_id=cut_id)
                    state.production, state.labels = self.production, self.labels
                    scope = self.demo_scopes[cut_id] = DemoCutScope(state, now)
                scope.requests += 1
        try:
            deadline = monotonic() + 0.1
            for state in evicted:
                if state.label_monitor:
                    state.label_monitor.join(timeout=max(0, deadline - monotonic()))
            yield scope.plotter
        finally:
            with self.demo_scopes_lock:
                scope.requests -= 1
                scope.last_used = monotonic()

    @contextmanager
    def guard_demo_workspace(self, cut_id, *, write=False, part_correction=False):
        key(cut_id)
        with self.demo_scopes_lock:
            scope = self.demo_scopes.get(cut_id)
            if write and cut_id in self.demo_saves:
                raise WorkspaceConflict("Дождитесь сохранения раскладки для этого материала.")
            if write:
                self.demo_saves.add(cut_id)
            if scope:
                scope.requests += 1
        try:
            scoped = scope.plotter if scope else None
            with ExitStack() as stack:
                if write and part_correction:
                    targets = [self.demo] if self.demo.selected_cut == cut_id else []
                    if scoped:
                        targets.append(scoped)
                    for target in targets:
                        stack.enter_context(request_lock(target.motion_start_lock, "Дождитесь текущего действия."))
                        stack.enter_context(request_lock(target.upload_lock, "Дождитесь завершения передачи задания."))
                if write:
                    stack.enter_context(self.workspace_lock)
                if scoped:
                    stack.enter_context(scoped.workspace_lock)
                if write and any(state and state.selected_cut == cut_id
                                 and (state.owns_drawing() or state.controller.upload_in_progress)
                                 for state in (self.demo, scoped)):
                    raise WorkspaceConflict("Раскладка используется виртуальным станком. Завершите или сбросьте раскрой.",
                                            code=WORKSPACE_IN_USE)
                yield
        finally:
            with self.demo_scopes_lock:
                if write:
                    self.demo_saves.remove(cut_id)
                if scope:
                    scope.requests -= 1

    def active_plotters(self):
        with self.demo_scopes_lock:
            return (self, self.demo, *(scope.plotter for scope in self.demo_scopes.values()))

    @contextmanager
    def guard_part_correction(self, cut_id, plotters):
        with ExitStack() as stack:
            for plotter in plotters:
                stack.enter_context(request_lock(plotter.motion_start_lock, "Дождитесь завершения текущего действия."))
                stack.enter_context(request_lock(plotter.upload_lock, "Дождитесь завершения передачи задания."))
            with self.workspace_lock:
                if any(plotter.selected_cut == cut_id and (plotter.owns_drawing() or plotter.controller.upload_in_progress)
                       for plotter in plotters):
                    raise WorkspaceConflict("Раскладка используется плоттером. Завершите или сбросьте раскрой.",
                                            code=WORKSPACE_IN_USE)
                yield

    def save_workspace(self, revision, draft, *, part_correction=False):
        if part_correction:
            with self.guard_part_correction(None, (self, self.demo)):
                return self.write_workspace(revision, draft)
        with self.workspace_lock:
            return self.write_workspace(revision, draft)

    def write_workspace(self, revision, draft):
        result = self.workspace.write(revision, draft)
        self.invalidate_loaded_plan(result["workspace"])
        self.demo.invalidate_loaded_plan(result["workspace"])
        return result

    def new_cut(self, plotter, data):
        if plotter.demo_cut_id is not None:
            raise WorkspaceConflict("Откройте самостоятельный раскрой перед очисткой.")
        if data.get("stopped") is not True:
            raise RequestError("Подтвердите, что плоттер остановлен.")
        revision = data.get("revision")
        if type(revision) is not int or revision < 0:
            raise RequestError("Неверная редакция рабочей области.")
        other = self if plotter is self.demo else self.demo
        transfers = "Дождитесь завершения передачи задания."
        movement = "Дождитесь завершения текущего действия."
        with (request_lock(self.motion_start_lock, movement), request_lock(self.demo.motion_start_lock, movement),
              request_lock(self.upload_lock, transfers), request_lock(self.demo.upload_lock, transfers)):
            with self.workspace_lock:
                if plotter.selected_cut is not None:
                    raise WorkspaceConflict("Вернитесь к самостоятельному раскрою перед очисткой.")
                if self.workspace.read()["revision"] != revision:
                    raise WorkspaceConflict(WORKSPACE_REVISION_MESSAGE,
                                            code=WORKSPACE_REVISION_CONFLICT if revision <= MAX_REVISION else None)
                if other.owns_drawing() or other.controller.upload_in_progress is True:
                    raise RequestError("Текущее задание открыто в другом режиме. Завершите его или сбросьте там.",
                                       HTTPStatus.CONFLICT)
            live = plotter.controller.read_live()
            with self.workspace_lock:
                plotter.verify_new_cut_idle(live)
                result = self.workspace.write(revision, None)
                plotter.clear_cut()
                other.clear_cut()
                return {**result, **plotter.cut_status()}

    def label_bundle(self, cut, mode, revision=None):
        if isinstance(self.workspace, CompanionWorkspace):
            return self.workspace.production_request("/api/label-source", {"cutId": cut, "mode": mode,
                                                                           "cutRevision": revision})
        from label_layout import store_label_context
        context = store_label_context(self.production, cut, mode)
        if revision is not None and revision != context["cutRevision"]:
            raise WorkspaceConflict("Раскладка изменилась с момента загрузки. Сверьте бирки с нарядом.")
        return {**context, **self.labels.templates()}

    def local_label_spooling(self):
        return platform == "win32" and (not self.public_origin
                                       or urlsplit(self.public_origin).hostname in (LOCAL_ADDRESS, "localhost"))

    def label_auto_relay(self, data):
        return self.workspace.production_request("/api/labels-auto-source", data)

    def server_close(self):
        with self.demo_scopes_lock:
            self.demo_scopes_closed = True
        plotters = self.active_plotters()
        for state in plotters:
            state.label_shutdown.set()
        deadline = monotonic() + 2
        for state in plotters:
            if state.label_monitor:
                state.label_monitor.join(timeout=max(0, deadline - monotonic()))
        self.labels.close()
        self.fit_jobs.close()
        for state in plotters:
            state.controller.stop_active_hold()
        super().server_close()

    def verify_update_idle(self):
        for plotter in self.active_plotters():
            if plotter.owns_drawing() or plotter.controller.control_is_armed():
                raise UpdateError("Завершите текущий раскрой и выключите управление станком.")
            try:
                result = plotter.read_live()
                plotter.verify_new_cut_idle(result)
                if result.get("uploadUncertain") or plotter.owns_drawing():
                    raise UpdateError("Сверьте состояние станка на штатном пульте перед обновлением.")
            except RequestError as error:
                raise UpdateError("Дождитесь остановки станка и попробуйте снова.") from error


class Handler(BaseHTTPRequestHandler):
    server: PlotterServer
    plotter: PlotterState
    server_version = "Plotter/1.0"
    sys_version = ""

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def do_GET(self):
        self.dispatch(self.get)

    def do_POST(self):
        self.dispatch(self.post)

    def do_OPTIONS(self):
        self.dispatch(self.preflight)

    def preflight(self, path, query):
        if (not self.server.companion_mode or self.server.local_companion_ui
                or self.headers.get("Access-Control-Request-Method") not in ("GET", "POST")):
            raise RequestError("Этот запрос недоступен.", HTTPStatus.METHOD_NOT_ALLOWED)
        requested = {name.strip().lower() for name in self.headers.get("Access-Control-Request-Headers", "").split(",") if name.strip()}
        if requested - {"content-type", "x-plotter-companion"}:
            raise RequestError("Недопустимые заголовки запроса.", HTTPStatus.FORBIDDEN)
        self.respond(b"", "text/plain", HTTPStatus.NO_CONTENT)

    def dispatch(self, route):
        try:
            host = self.headers.get("Host", "").lower()
            origin = self.headers.get("Origin")
            if self.server.local_companion_ui:
                allowed = ((not origin and self.command == "GET") or origin == f"http://{host}")
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    allowed = (allowed and self.command == "GET" and self.path == "/"
                               and self.headers.get("Sec-Fetch-Mode") == "navigate")
            elif self.server.companion_mode:
                allowed = origin == self.server.companion_origin
                if self.command == "POST":
                    allowed = allowed and self.headers.get("X-Plotter-Companion") == str(COMPANION_PROTOCOL)
            else:
                allowed = not origin or origin == (self.server.public_origin or f"http://{host}")
            if host not in self.server.hosts or not allowed:
                raise RequestError("Приложение принимает запросы с локального адреса.", HTTPStatus.FORBIDDEN)
            result = urlsplit(self.path)
            if result.netloc or result.scheme or not self.path.startswith("/"):
                raise RequestError("Недопустимый адрес запроса.")
            path = result.path
            self.plotter = self.server
            self.demo_cut_scope = None
            if (self.server.companion_mode and not self.server.local_companion_ui
                    and path not in HARDWARE_ROUTES | {"/api/new-cut"}
                    and not (path.startswith("/demo/api/") and path.removeprefix("/demo") in DIRECT_LABEL_ROUTES)):
                raise RequestError("Этот запрос обслуживает сервер раскладки.", HTTPStatus.NOT_FOUND)
            if path.startswith("/demo/api/"):
                self.plotter = self.server.demo
                path = path.removeprefix("/demo")
                if path in ("/api/open-in-xiao", "/api/stage", "/api/reveal"):
                    raise RequestError("В демо-режиме можно скачать DXF.", HTTPStatus.FORBIDDEN)
                demo_cut = self.headers.get("X-Plotter-Demo-Cut")
                if demo_cut is not None:
                    if (self.headers.get("Sec-Fetch-Site") in ("same-site", "cross-site")
                            or self.server.companion_mode and not self.server.local_companion_ui):
                        raise RequestError("Откройте демораскрой на этом сервере.", HTTPStatus.FORBIDDEN)
                    self.demo_cut_scope = key(demo_cut)
            elif self.server.central_mode and path in HARDWARE_ROUTES - DIRECT_LABEL_ROUTES - {
                    "/api/status", "/api/cut-plan", "/api/prepare-xiao", "/api/controller-live", "/api/cut-context", "/api/labels-pdf"}:
                raise RequestError("Для управления станком подключите помощник на компьютере оператора.", HTTPStatus.FORBIDDEN)
            if self.server.central_mode and path in ("/api/stage", "/api/reveal"):
                raise RequestError("Скачайте DXF на компьютер оператора.", HTTPStatus.FORBIDDEN)
            with ExitStack() as stack:
                if (self.server.app_updates.supported and self.command == "POST"
                        and path not in RESTART_SAFE_ACTIONS and path not in ("/api/app-update", "/api/controller-live")):
                    stack.enter_context(self.server.app_updates.protected_request())
                if self.demo_cut_scope and path in HARDWARE_ROUTES | {"/api/workspace", "/api/new-cut", "/api/command", "/api/plan"}:
                    self.plotter = stack.enter_context(self.server.scoped_demo(self.demo_cut_scope))
                route(path, result.query)
        except UpdateError as error:
            self.json_response({"error": str(error)}, HTTPStatus.CONFLICT)
        except RequestError as error:
            self.json_response({"error": str(error)}, error.status)
        except RegistryArchiveTooLarge as error:
            self.json_response({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except WorkspaceConflict as error:
            self.json_response({"error": str(error), **({"code": error.code} if error.code else {})}, HTTPStatus.CONFLICT)
        except RecordNotFound as error:
            self.json_response({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except NestingFitError as error:
            self.json_response({
                "error": str(error), "indices": error.indices,
                "usableWidthMm": error.usable_width_mm,
                "minimumPartWidthMm": error.minimum_part_width_mm,
                "minimumWidthMm": error.minimum_width_mm,
                "quarterTurnMinimumWidthMm": error.quarter_turn_minimum_width_mm,
                "quarterTurnFits": error.quarter_turn_fits,
                "blockedPartCount": error.blocked_part_count,
            }, HTTPStatus.UNPROCESSABLE_ENTITY)
        except ValueError as error:
            self.json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            LOGGER.exception("Ошибка обработки %s", self.path)
            self.json_response({"error": "Ошибка локального сервера. Подробности в журнале запуска."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def get(self, path, query):
        if path in MIME:
            name, content_type = MIME[path]
            self.respond((ROOT / "static" / name).read_bytes(), content_type)
        elif path == "/api/status":
            self.json_response({**self.plotter.status(), "companionRevision": self.server.app_updates.revision,
                                "localLabelPrinting": self.local_label_printing(), "scopedDemoCuts": True})
        elif path == "/api/app-update":
            self.json_response({**self.server.app_updates.status(), "companionDownload": self.companion_download_available()})
        elif path == "/api/release-notes":
            self.json_response({"version": APP_VERSION, "notes": read_release_notes(ROOT, APP_VERSION),
                                "companionDownload": self.companion_download_available()})
        elif path == "/api/companion-installer":
            self.download_companion_installer()
        elif path in ("/api/companion-update", "/api/companion-update.zip"):
            self.get_companion_update(path)
        elif path == "/api/demo":
            self.json_response(demo_drawing())
        elif path == "/api/workspace":
            result = (self.plotter.read_production(self.demo_cut_scope, "demo") if self.demo_cut_scope
                      else self.plotter.workspace.read())
            self.json_response(result)
        elif path == "/api/label-templates":
            self.json_response(self.server.workspace.production_request(path) if self.server.local_companion_ui
                               else self.server.labels.templates())
        elif path == "/api/label-printers":
            self.direct_label_command(path)
        elif path == "/api/cad/3d/fit":
            self.require_sketchup_design()
            identifier = parse_qs(query, max_num_fields=2).get("id", [""])[0]
            self.json_response(self.server.fit_jobs.status(identifier))
        elif path == "/api/cad/3d/model":
            self.require_sketchup_design()
            model_id = parse_qs(query, max_num_fields=2).get("id", [""])[0]
            contents = self.server.production.model(model_id)
            content_type = "application/json; charset=utf-8"
            if re.search(r"(?:^|,)\s*gzip\s*(?:,|$)", self.headers.get("Accept-Encoding", ""), re.I):
                self.response_headers(len(contents), content_type, HTTPStatus.OK, encoding="gzip")
                self.wfile.write(contents)
            else:
                self.respond(gzip.decompress(contents), content_type)
        elif path in PRODUCTION_ROUTES or path == "/api/order-analysis":
            self.get_production(path, query)
        elif path == "/api/registry-backup":
            self.get_registry_backup()
        elif path == "/api/sewing-map.pdf":
            self.get_sewing_pdf(query)
        elif path == "/api/part-preview.svg":
            self.get_part_preview(query)
        elif path == "/api/reachability":
            self.json_response(self.plotter.reachability())
        else:
            raise RequestError("Страница не найдена.", HTTPStatus.NOT_FOUND)

    def companion_download_available(self):
        installer = self.server.companion_installer
        return self.server.local_companion_ui or bool(installer and installer.is_file())

    def send_companion_file(self, path, content_type, filename=None):
        try:
            source = path.open("rb")
        except (FileNotFoundError, IsADirectoryError) as error:
            raise RequestError("Файл помощника недоступен.", HTTPStatus.NOT_FOUND) from error
        with source:
            size = os.fstat(source.fileno()).st_size
            self.response_headers(size, content_type, HTTPStatus.OK, filename, filename or "plotter.json")
            shutil.copyfileobj(source, self.wfile, 256 * 1024)

    def download_companion_installer(self):
        installer = self.server.companion_installer
        if installer:
            self.send_companion_file(installer, "application/octet-stream", COMPANION_INSTALLER)
            return
        if not self.server.local_companion_ui:
            raise RequestError("Установщик помощника недоступен.", HTTPStatus.NOT_FOUND)

        workspace = self.server.workspace
        try:
            source = workspace.opener.open(workspace.origin + "/api/companion-installer", timeout=20)
        except HTTPError as error:
            with error:
                if error.code == 404:
                    raise RequestError("Установщик ещё не размещён на центральном сервере.", HTTPStatus.NOT_FOUND) from error
            raise RequestError("Центральный сервер не выдал установщик.", HTTPStatus.BAD_GATEWAY) from error
        except OSError as error:
            raise RequestError("Нет связи с центральным сервером. Повторите скачивание.", HTTPStatus.BAD_GATEWAY) from error
        with source:
            size = source.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,10}", size) or not 0 < int(size) <= MAX_ARCHIVE:
                raise RequestError("Центральный сервер вернул неверный размер установщика.", HTTPStatus.BAD_GATEWAY)
            filename = source.headers.get_filename()
            if not isinstance(filename, str) or not re.fullmatch(r"Plotter-Companion-Setup-[0-9.]+-x64\.exe", filename):
                filename = "Plotter-Companion-Setup-x64.exe"
            self.response_headers(int(size), "application/octet-stream", HTTPStatus.OK, filename, filename)
            shutil.copyfileobj(source, self.wfile, 256 * 1024)

    def get_companion_update(self, path):
        installer = self.server.companion_installer
        if installer is None:
            raise RequestError("Обновление помощника недоступно.", HTTPStatus.NOT_FOUND)
        manifest = installer.parent / "companion-update.json"
        payload = installer.parent / COMPANION_UPDATE
        if not manifest.is_file() or not payload.is_file():
            raise RequestError("Обновление помощника недоступно.", HTTPStatus.NOT_FOUND)
        if path == "/api/companion-update":
            self.send_companion_file(manifest, "application/json; charset=utf-8")
        else:
            self.send_companion_file(payload, "application/zip", COMPANION_UPDATE)

    def post(self, path, query):
        if path == "/api/workspace" and self.demo_cut_scope:
            raise WorkspaceConflict("Сохраните раскладку в текущем материале демораскроя.")
        if path in ("/api/label-templates", "/api/label-preview", "/api/label-source"):
            self.label_design(path)
        elif path in ("/api/labels-status", "/api/labels-settings", "/api/labels-print",
                      "/api/labels-image", "/api/labels-pdf", "/api/labels-auto"):
            self.operator_labels(path)
        elif path in DIRECT_LABEL_ROUTES:
            self.direct_label_command(path, self.json_body(4096))
        elif path == "/api/labels-auto-source":
            if not self.server.central_mode or self.server.local_label_spooling():
                raise RequestError("Передача автоматических бирок доступна центральному демораскрою.", HTTPStatus.FORBIDDEN)
            data = self.json_body(4096)
            self.json_response(self.server.labels.relay_source(key(data.get("cutId")), data.get("relayKey"), data.get("station"),
                                                               data.get("take"), data.get("jobId"), data.get("status")))
        elif path == "/api/app-update":
            data = self.json_body(4096)
            self.server.app_updates.start(data.get("version"), self.server.verify_update_idle, revision=data.get("revision"))
            self.json_response(self.server.app_updates.status(), HTTPStatus.ACCEPTED)
        elif path in ("/api/import", "/api/cad/import"):
            name = parse_qs(query, max_num_fields=4).get("filename", [""])[0]
            if not name or len(name) > 255 or re.search(r"[/\\\x00-\x1f]", name):
                raise RequestError("Передайте имя файла DXF или DWG.")
            data = self.body(MAX_FILE_BYTES, "application/octet-stream")
            if path == "/api/cad/import":
                drawing = (self.server.workspace.production_request(path + "?filename=" + quote(name), raw=data)
                           if self.server.local_companion_ui else import_design(data, name))
            else:
                drawing = import_drawing(data, name)
                drawing["paths"] = annotate_notches(drawing["paths"])
            self.json_response(drawing)
        elif path in ("/api/cad/3d/import", "/api/cad/3d/model-surface", "/api/cad/3d/calculate", "/api/cad/3d/check-panel", "/api/cad/3d/preview-contour",
                      "/api/cad/3d/route-contour", "/api/cad/3d/project-contour", "/api/cad/3d/remaining",
                      "/api/cad/3d/selected-regions", "/api/cad/3d/edit-cut",
                      "/api/cad/3d/check-transfer", "/api/cad/3d/transfer",
                      "/api/cad/3d/fit", "/api/cad/3d/fit-cancel"):
            self.post_sketchup_design(path, query)
        elif path in ("/api/cad/paths", "/api/cad/edit"):
            data = self.json_body(MAX_WORKSPACE_BYTES)
            result = (prepare_design(data.get("entities")) if path.endswith("/paths") else
                      edit_design(data.get("entities"), data.get("operation"), data.get("options")))
            self.json_response(result)
        elif path in PRODUCTION_ROUTES:
            self.post_production(path)
        elif path == "/api/registry-backup":
            self.post_registry_backup()
        elif path == "/api/cut-context":
            data = self.json_body(4096)
            if self.plotter is self.server.demo and data.get("action") == "select" and data.get("cutId") is not None:
                with self.server.select_legacy_demo(data["cutId"]):
                    result = self.plotter.change_cut_context(data)
            else:
                result = self.plotter.change_cut_context(data)
            self.json_response(result)
        elif path == "/api/close-contours":
            data = self.json_body(MAX_WORKSPACE_BYTES)
            paths = drawing_paths(data.get("paths"))
            excluded = excluded_paths(data.get("excluded", []), len(paths))
            repaired = repair_closed_contours(paths, excluded, bridge_small_gaps=True)
            new_outlines = {index for index, (contour, sources) in enumerate(zip(repaired.paths, repaired.sources))
                            if contour["closed"] and not paths[sources[0]]["closed"]}
            self.json_response({"paths": annotate_notches(repaired.paths, new_outlines=new_outlines),
                                "closedCount": repaired.closed_count,
                                "sources": repaired.sources, "excluded": repaired.excluded})
        elif path == "/api/prepare-xiao":
            data = self.json_body(16 * 1024)
            open_in_xiao = data.get("openInXiao", False)
            if type(open_in_xiao) is not bool:
                raise RequestError("Укажите, нужно ли открыть подготовленный DXF в Xiao.")
            if open_in_xiao and (self.plotter.demo_mode or self.plotter.central_mode):
                raise RequestError("В демо-режиме можно скачать DXF.", HTTPStatus.FORBIDDEN)
            export_id = data.get("exportPlanId") if self.plotter.central_mode else None
            filename, contents = self.plotter.prepare_xiao(
                export_id if export_id is not None else data.get("planId"), shared_export=export_id is not None,
                cut_id=data.get("cutId"),
            )
            if open_in_xiao:
                staged = self.stage(filename, contents)
                self.json_response({"filename": staged.name, "path": str(staged), **self.open_in_xiao(staged)})
            else:
                self.respond(contents, "application/dxf", filename=filename)
        elif path == "/api/roll-reset":
            self.json_response(self.plotter.reset_roll(self.json_body(16 * 1024)))
        elif path == "/api/new-cut":
            if self.server.companion_mode and not self.server.local_companion_ui:
                data = self.json_body(16 * 1024)
                if data.get("stopped") is not True:
                    raise RequestError("Подтвердите, что плоттер остановлен.")
                with request_lock(self.server.motion_start_lock, "Дождитесь завершения текущего действия."), \
                        request_lock(self.server.upload_lock, "Дождитесь завершения передачи задания."):
                    self.server.verify_new_cut_idle(self.server.read_live())
                    self.server.clear_cut()
                    self.json_response({"idle": True, **self.server.cut_status()})
                return
            self.json_response(self.server.new_cut(self.plotter, self.json_body(16 * 1024)))
        elif path in ("/api/workspace", "/api/cut-plan", "/api/nest", "/api/nest-parts", "/api/command", "/api/plan", "/api/export", "/api/stage", "/api/reveal", "/api/open-in-xiao",
                      "/api/controller", "/api/controller-position", "/api/controller-job",
                      "/api/controller-hold", "/api/controller-operation", "/api/controller-feed-mode",
                      "/api/controller-upload", "/api/controller-live", "/api/control-session",
                      "/api/panel-snapshot", "/api/panel-compare"):
            limit = MAX_WORKSPACE_BYTES if path in ("/api/workspace", "/api/cut-plan", "/api/nest", "/api/nest-parts") else MAX_JSON
            if path in ("/api/panel-snapshot", "/api/panel-compare"):
                limit = 16 * 1024
            data = self.json_body(limit)
            if path in RESTART_SAFE_ACTIONS:
                action = data.get("action")
                with self.server.app_updates.protected_request(allow_restart=action == RESTART_SAFE_ACTIONS[path]):
                    if path == "/api/control-session":
                        result = self.plotter.controller.control_session(action, data.get("controlToken"), data.get("ready"))
                    elif path == "/api/controller-hold":
                        result = self.plotter.hold(data)
                    else:
                        result = self.plotter.operate(data)
                self.json_response(result)
            elif path == "/api/workspace":
                self.json_response(self.server.save_workspace(data.get("revision"), data.get("workspace"),
                                                               part_correction=data.get("partCorrection") is True))
            elif path == "/api/cut-plan":
                self.json_response(self.plotter.cut_plan(
                    data.get("job"), data.get("filename"), self.plotter.controller.upload_page_add(),
                ))
            elif path == "/api/nest":
                self.json_response(self.plotter.nest(data))
            elif path == "/api/nest-parts":
                self.json_response({"parts": identify_parts(data.get("paths"))})
            elif path == "/api/command":
                self.json_response(self.plotter.status(self.plotter.simulator.command(data)))
            elif path == "/api/controller":
                self.json_response(self.plotter.controller.read_status())
            elif path == "/api/controller-live":
                self.json_response(self.plotter.read_live())
            elif path == "/api/panel-snapshot":
                extended = data.get("extended", False)
                if type(extended) is not bool:
                    raise RequestError("Неверный режим диагностики.")
                self.json_response(self.plotter.controller.read_panel_snapshot(extended=extended))
            elif path == "/api/panel-compare":
                self.json_response({"candidates": compare_panel_samples(data.get("samples"))})
            elif path == "/api/controller-position":
                self.json_response(self.plotter.controller.read_position())
            elif path == "/api/controller-job":
                self.json_response(self.plotter.controller.read_job_index())
            elif path == "/api/controller-feed-mode":
                self.json_response(self.plotter.controller.read_feed_mode())
            elif path == "/api/controller-upload":
                result = self.plotter.upload(data)
                if result.get("jobReceipt"):
                    try:
                        result.update(self.remote_label_offer(data.get("cutId")))
                    except Exception:
                        LOGGER.exception("Не удалось получить разрешение передачи автоматических бирок")
                self.json_response(result)
            elif path == "/api/reveal":
                self.reveal(data.get("filename"))
            else:
                plan = self.plotter.plan(data.get("job"))
                if path == "/api/plan":
                    self.json_response(plan.summary())
                    return
                filename = output_name(data.get("filename"))
                contents = export_dxf(plan)
                if path == "/api/export":
                    self.respond(contents, "application/dxf", filename=filename)
                else:
                    staged = self.stage(filename, contents)
                    result = {"filename": staged.name, "path": str(staged), "folder": str(STAGING)}
                    if path == "/api/open-in-xiao":
                        result.update(self.open_in_xiao(staged))
                    self.json_response(result)
        else:
            raise RequestError("Команда не найдена.", HTTPStatus.NOT_FOUND)

    def require_sketchup_design(self):
        if not self.server.sketchup_design:
            raise RequestError("Работа с моделями SketchUp доступна в локальном Plotter для Windows.", HTTPStatus.NOT_FOUND)

    def label_design(self, path):
        from label_layout import editable_template, master_label_fields, render_label
        data = self.json_body(MAX_JSON)
        if self.server.local_companion_ui:
            self.json_response(self.server.workspace.production_request(path, data))
            return
        kind = data.get("kind")
        if path == "/api/label-templates":
            self.json_response(self.server.labels.save_template(kind, data.get("template"), data.get("revision")))
            return
        cut = key(data.get("cutId"))
        mode = data.get("mode", "preview" if path == "/api/label-preview" else "operator")
        if mode not in (("preview", "operator", "demo") if path == "/api/label-preview" else ("operator", "demo")):
            raise ValueError("Выберите режим раскроя.")
        bundle = self.server.label_bundle(cut, mode, data.get("cutRevision"))
        if path == "/api/label-source":
            self.json_response(bundle)
            return
        if kind not in ("part", "master"):
            raise ValueError("Выберите шаблон бирки.")
        templates, parts = bundle["templates"], bundle["parts"]
        chosen = data.get("partKey") or next(iter(parts), None)
        if kind == "part" and (not isinstance(chosen, str) or chosen not in parts):
            raise ValueError("В материале нет выбранной детали.")
        fields = parts[chosen] if kind == "part" else master_label_fields(bundle["master"], timestamp())
        image, warnings = render_label(editable_template(kind, data.get("template", templates[kind])), fields)
        self.json_response({"image": "data:image/png;base64," + b64encode(image).decode("ascii"), "warnings": warnings,
                            "parts": [{"key": k, "name": f["name"], "number": f["number"]} for k, f in parts.items()]})

    def operator_labels(self, path):
        from label_layout import label_pdf, render_label
        data = self.json_body(MAX_JSON)
        cut = key(data.get("cutId"))
        state = self.plotter
        mode = "demo" if state.demo_mode else "operator"
        queue = state.labels
        if path == "/api/labels-settings":
            # Drain any already-confirmed progress under the old setting first.
            active = state.selected_cut == cut and state.label_run and state.cut_progress
            if active and active.state not in ("ready", "advance", "completed", "cancelled"):
                state.read_live()
            with state.workspace_lock:
                progress = state.cut_progress
                if (data.get("enabled") is True and state.selected_cut == cut and progress
                        and progress.state not in ("ready", "advance", "completed", "cancelled") and not progress.fresh):
                    raise WorkspaceConflict("Для включения печати дождитесь подтверждения позиции плоттера.")
                queue.configure(cut, mode, data.get("enabled"))
        elif path == "/api/labels-print":
            queue.manual(cut, mode, data.get("runId"), data.get("keys"), data.get("requestId"))
        elif path == "/api/labels-pdf":
            ids, part_key = data.get("ids"), data.get("key")
            if ids is not None and part_key is not None:
                raise ValueError("Выберите деталь или бирки из ленты.")
            pages = (queue.pages(cut, mode, ids) if ids is not None
                     else queue.manual_pages(cut, mode, part_key, data.get("cutRevision")))
            self.respond(label_pdf(pages), "application/pdf", filename="labels.pdf", fallback_filename="labels.pdf")
            return
        elif path == "/api/labels-auto":
            # Older clients used this endpoint to download automatic PDFs.
            self.respond(b"", "text/plain", HTTPStatus.NO_CONTENT)
            return
        elif path == "/api/labels-image":
            page, = queue.pages(cut, mode, [data.get("id")])
            image, warnings = render_label(*page)
            self.json_response({"image": "data:image/png;base64," + b64encode(image).decode("ascii"), "warnings": warnings})
            return
        status = queue.status(cut, mode)
        controlled = path == "/api/labels-settings" and data.get("enabled") is True and data.get("relay") is True
        if state.demo_mode and state.selected_cut == cut and self.server.central_mode and data.get("controlToken"):
            try:
                state.controller.require_control(data["controlToken"])
            except ValueError:
                pass
            else:
                controlled = True
        status["autoRelay"] = False
        if controlled and status["enabled"]:
            status.update(self.remote_label_offer(cut))
        self.json_response(status)

    def remote_label_offer(self, cut):
        state = self.plotter
        if (not cut or cut != state.selected_cut or not state.demo_mode or not self.server.central_mode
                or self.server.local_label_spooling() or not state.label_run):
            return {}
        token = state.labels.relay_key(cut, state.label_run)
        return {"autoRelay": True, "enabled": True, "relayKey": token} if token else {}

    def local_label_printing(self):
        host = self.headers.get("Host", "").lower()
        return self.server.local_label_spooling() and host in {f"{LOCAL_ADDRESS}:{self.server.server_port}",
                                                               f"localhost:{self.server.server_port}"}

    def direct_label_command(self, path, data=None):
        if not self.local_label_printing():
            raise RequestError("Прямая печать доступна в локальном Plotter для Windows или через помощник.",
                               HTTPStatus.FORBIDDEN)
        queue = self.server.labels
        if path == "/api/labels-auto-relay":
            queue.register_relay(key(data.get("cutId")), data.get("relayKey"))
            result = {"registered": True}
        elif path == "/api/label-printers":
            try:
                result = queue.printer_settings() if data is None else queue.select_printer(data.get("printer"))
            except OSError as error:
                raise RequestError("Не удалось получить список принтеров Windows.", HTTPStatus.SERVICE_UNAVAILABLE) from error
        else:
            mode = data.get("mode")
            if data.get("confirmed") is not True:
                raise ValueError("Подтвердите печать одной бирки на выбранном принтере.")
            if mode not in ("operator", "demo") or self.plotter.demo_mode and mode != "demo":
                raise ValueError("Проверьте режим раскроя для печати бирки.")
            result = queue.print_part(key(data.get("cutId")), mode, data.get("cutRevision"), data.get("partKey"),
                                      data.get("printer"), data.get("requestId"))
        self.json_response(result)

    def post_sketchup_design(self, path, query):
        self.require_sketchup_design()
        if path.endswith("/import"):
            filename = parse_qs(query, max_num_fields=2).get("filename", [""])[0]
            data = self.body(MAX_SKP_BYTES, "application/octet-stream")
            with request_lock(self.server.sketchup_lock, "Дождитесь окончания работы с моделью."):
                model_id = self.server.production.stage_model(import_model(data, filename))
            self.json_response({"id": model_id}, HTTPStatus.CREATED)
            return

        from upholstery import (build_entities, cad_sync_geometry, calculate, check_cover_panel, check_transfer,
                                preview_contour, project_cad_contour, remaining_panels, route_contour, selected_panel_regions)

        extra_limit = MAX_MARKUP_BYTES if path.endswith(("/selected-regions", "/model-surface")) else (
            256 if path.endswith(("/project-contour", "/edit-cut")) else 32) * 1024
        data = self.json_body(MAX_MARKUP_BYTES + extra_limit)
        if path.endswith("/fit-cancel"):
            self.json_response(self.server.fit_jobs.cancel(data.get("id")))
            return
        state = data.get("state")
        markup(state)
        if path.endswith("/fit"):
            result = self.server.fit_jobs.start(self.server.production.model(data.get("id")), state)
            self.json_response(result, HTTPStatus.ACCEPTED)
            return
        with request_lock(self.server.sketchup_lock, "Дождитесь окончания работы с моделью."):
            model = json.loads(gzip.decompress(self.server.production.model(data.get("id"))))
            edges = state.get("modelEdges", [])
            if edges or path.endswith("/model-surface"):
                from model_surface import build_surface_model

                if path.endswith("/model-surface") and "previousEdges" in data:
                    check_surface_edit(state, data["previousEdges"], edges)
                model = build_surface_model(model, edges)
            if path.endswith("/model-surface"):
                self.json_response(model)
                return
            if path.endswith("/edit-cut"):
                from upholstery import edit_cover_cut

                edit = edit_cover_cut(model, state, data.get("panelId"), data.get("anchors"), data.get("kind"))
                next_state, checked = edit["state"], edit["calculated"]
                markup(next_state)
                checked["cadSync"] = cad_sync_geometry(data["id"], checked, next_state, transfer_manifest(next_state, checked))
                missing = set(edit["panelIds"]) - checked["cadSync"]["patterns"].keys()
                if missing:
                    issue = next((item["message"] for item in checked["cadSync"]["issues"] if item["id"] in missing), None)
                    raise ValueError(issue or "Не удалось построить лекало с этой линией.")
                self.json_response(edit)
                return
            if path.endswith("/project-contour"):
                panel_id = data.get("panelId")
                draft = project_cad_contour(model, state, panel_id, data.get("previous"), data.get("points"))
                checked = draft["calculated"]
                checked["cadSync"] = cad_sync_geometry(data["id"], checked, state, transfer_manifest(state, checked))
                if panel_id not in checked["cadSync"]["patterns"]:
                    issue = next((i["message"] for i in checked["cadSync"]["issues"] if i["id"] == panel_id), None)
                    raise ValueError(issue or "Не удалось проверить лекало после изменения контура.")
                self.json_response(draft)
                return
            if path.endswith("/route-contour"):
                self.json_response(route_contour(model, state, data.get("nodeId"), data.get("anchors")))
                return
            if path.endswith("/preview-contour"):
                self.json_response(preview_contour(model, state, data.get("nodeId"), data.get("anchors"), data.get("seed"),
                                                   surface_route=data.get("surfaceRoute") is True))
                return
            if path.endswith("/remaining"):
                self.json_response({"regions": remaining_panels(model, state, data.get("nodeId"))})
                return
            if path.endswith("/selected-regions"):
                self.json_response({"regions": selected_panel_regions(model, state, data.get("nodeId"), data.get("faces"))})
                return
            if path.endswith("/check-panel"):
                check_cover_panel(model, state, data.get("nodeId"), data.get("panelId"))
                self.json_response({"valid": True})
                return
            result = calculate(model, state)
            if path.endswith("/calculate") and "checkPlacement" in data:
                from upholstery import check_panel_placement

                check_panel_placement(result, data["checkPlacement"])
            if path.endswith("/calculate") and data.get("sync2d") is True:
                result["cadSync"] = cad_sync_geometry(data["id"], result, state, transfer_manifest(state, result))
            elif path.endswith("/check-transfer"):
                result = check_transfer(result, state, data.get("allowanceMm"), data.get("notchDepthMm"), data.get("panelIds"),
                                        panel_settings=data.get("panelSettings"))
            elif path.endswith("/transfer"):
                entities = build_entities(result, data.get("allowanceMm"), data.get("notchDepthMm"),
                                          state=state, panel_ids=data.get("panelIds"), panel_settings=data.get("panelSettings"))
                manifest = transfer_manifest(state, result)
                identifiers = {entity["groupPath"][1] for entity in entities if entity.get("groupPath")}
                result = {"entities": entities, "warnings": result["warnings"],
                          "patterns": {identifier: manifest[identifier] for identifier in identifiers}}
        self.json_response(result)

    def get_sewing_pdf(self, query):
        params = {key: values[0] for key, values in parse_qs(query, max_num_fields=4).items()}
        if self.server.local_companion_ui:
            contents = self.server.workspace.production_request("/api/sewing-map.pdf?" + urlencode(params), pdf=True)
            title = "Карта пошива"
        else:
            from sewing_document import cut_document, kit_document, standalone_document
            from sewing_pdf import render_sewing_pdf

            store = self.server.production
            with request_lock(self.server.sewing_pdf_lock, "Дождитесь построения карты пошива."):
                if params.get("kitId"):
                    width = float(params["widthMm"]) if "widthMm" in params else None
                    data = kit_document(store, params["kitId"], int(params.get("quantity", 1)), width)
                elif params.get("cutId"):
                    data = cut_document(store, params["cutId"], params.get("mode", "operator"))
                elif params.get("workspace") == "1":
                    data = standalone_document(store, self.plotter.workspace.read()["workspace"])
                else:
                    raise ValueError("Выберите комплект или раскрой для карты пошива.")
                name, parts, context, layout = data
                title = "Карта пошива · " + name
                contents = render_sewing_pdf(name, parts, context=context, workspace=layout)
                if len(contents) > MAX_WORKSPACE_BYTES:
                    raise ValueError("Карта пошива слишком велика. Скачайте её для меньшего количества комплектов.")
        self.response_headers(len(contents), "application/pdf", HTTPStatus.OK, title + ".pdf", "sewing-map.pdf")
        self.wfile.write(contents)

    def get_part_preview(self, query):
        from sewing_document import registry_part_document
        from sewing_pdf import render_part_preview_svg

        identifier = key(parse_qs(query, max_num_fields=1).get("id", [None])[0])
        if self.server.local_companion_ui:
            entry = self.server.workspace.production_request("/api/registry?" + urlencode({"id": identifier}))
        else:
            entry = self.server.production.registry(identifier)
        with request_lock(self.server.sewing_pdf_lock, "Дождитесь построения чертежа."):
            contents = render_part_preview_svg(registry_part_document(entry))
        if len(contents) > MAX_WORKSPACE_BYTES:
            raise ValueError("Чертёж детали слишком велик для предпросмотра.")
        self.respond(contents, "image/svg+xml; charset=utf-8")

    def get_registry_backup(self):
        filename = "plotter-registry.rar"
        with request_lock(self.server.registry_archive_lock, "Дождитесь окончания работы с резервной копией реестра."):
            if self.server.local_companion_ui:
                contents = self.server.workspace.production_request("/api/registry-backup", rar=True)
            else:
                contents = write_registry_archive(self.server.production.export_registry())
            self.response_headers(len(contents), RAR_MIME, HTTPStatus.OK, filename, filename)
            self.wfile.write(contents)

    def post_registry_backup(self):
        with request_lock(self.server.registry_archive_lock, "Дождитесь окончания работы с резервной копией реестра."):
            contents = self.body(MAX_REGISTRY_ARCHIVE, "application/octet-stream")
            if self.server.local_companion_ui:
                result = self.server.workspace.production_request("/api/registry-backup", raw=contents)
            else:
                result = self.server.production.restore_registry(read_registry_archive(contents))
        self.json_response(result)

    def get_production(self, path, query):
        params = {key: values[0] for key, values in parse_qs(query, max_num_fields=4).items()}
        if path == "/api/operator-workspace" and params.get("mode") == "demo":
            cut_id = params.get("cutId")
            with self.server.guard_demo_workspace(cut_id):
                result = self.server.read_production(cut_id, "demo")
            self.json_response(result)
            return
        if self.server.local_companion_ui:
            self.json_response(self.server.workspace.production_request(path + "?" + urlencode(params)))
            return
        store = self.server.production
        if path == "/api/registry":
            if params.get("mode") == "kit-layout":
                try:
                    width, amount = float(params.get("widthMm", "")), int(params.get("quantity", ""))
                except ValueError as error:
                    raise RequestError("Укажите ширину полотна и количество комплектов.") from error
                self.json_response(store.kit_layout(params.get("id"), width, amount))
            else:
                registry = store.registry(params.get("id"))
                if self.server.sketchup_design and not params.get("id"):
                    registry["sketchupDesign"] = True
                self.json_response(registry)
        elif path == "/api/order-analysis":
            period = analysis_period(params)
            self.json_response(order_analysis(store.orders(published_only=True)["orders"], period))
        elif path == "/api/orders":
            self.json_response(store.orders(params.get("id"), published_only=params.get("published") == "1"))
        else:
            self.json_response(store.workspace(params.get("cutId"), params.get("mode")))

    def post_production(self, path):
        data = self.json_body(MAX_WORKSPACE_BYTES)
        action = data.get("action")
        mode = data.get("mode")
        part_correction = data.get("partCorrection") is True
        if path == "/api/operator-workspace" and action == "save" and mode == "demo":
            with self.server.guard_demo_workspace(data.get("cutId"), write=True, part_correction=part_correction):
                result = (self.server.workspace.production_request(path, data) if self.server.local_companion_ui else
                          self.server.production.save_workspace(data, mode, self.server.verify_production_layout))
            self.json_response(result)
            return
        if self.server.local_companion_ui:
            if path == "/api/operator-workspace" and action == "save" and mode in ("operator", "demo"):
                plotter = self.server.demo if mode == "demo" else self.server
                guard = (self.server.guard_part_correction(data.get("cutId"), (plotter,))
                         if part_correction else self.server.workspace_lock)
                with guard:
                    if plotter.selected_cut == data.get("cutId") and plotter.owns_drawing():
                        raise WorkspaceConflict("Сначала освободите раскладку на плоттере.", code=WORKSPACE_IN_USE)
                    result = self.server.workspace.production_request(path, data)
            else:
                result = self.server.workspace.production_request(path, data)
            self.json_response(result)
            return
        store = self.server.production
        verify = self.server.verify_production_layout
        if path == "/api/registry" and action in ("save", "move"):
            result = store.save_registry(data, allow_3d=self.server.sketchup_design)
        elif path == "/api/registry" and action == "delete":
            result = store.delete_registry(data)
        elif path == "/api/registry" and action == "collect":
            result = store.collect(data.get("members"))
        elif path == "/api/registry" and action == "save-kit-layout":
            result = store.save_kit_layout(data, verify)
        elif path == "/api/orders" and action == "save":
            result = store.save_order(data)
        elif path == "/api/orders" and action == "update-meta":
            result = store.update_order_metadata(data)
        elif path == "/api/orders" and action in ("save-cut", "remove-cut"):
            result = store.save_cut(data, verify)
        elif path == "/api/orders" and action == "publish":
            result = store.publish_order(data, verify)
        elif path == "/api/orders" and action == "publish-cut":
            result = store.publish_cut(data, verify)
        elif path == "/api/orders" and action == "reopen":
            result = store.reopen_order(data)
        elif path == "/api/orders" and action == "delete":
            result = store.delete_order(data)
        elif path == "/api/operator-workspace" and action == "save":
            plotter = self.server.demo if mode == "demo" else self.server
            guard = (self.server.guard_part_correction(data.get("cutId"), (plotter,))
                     if part_correction else self.server.workspace_lock)
            with guard:
                if plotter.selected_cut == data.get("cutId") and plotter.owns_drawing():
                    raise WorkspaceConflict("Раскладка используется плоттером. Завершите или сбросьте раскрой.",
                                            code=WORKSPACE_IN_USE)
                result = store.save_workspace(data, mode, verify)
        elif (path == "/api/operator-workspace" and action in ("start", "finish") and mode in ("operator", "demo")
              and self.server.central_mode):
            result = store.progress(data.get("cutId"), action, mode, data.get("revision"))
        elif path == "/api/operator-workspace" and action == "verify" and mode == "operator":
            record = store.workspace(data.get("cutId"), "operator")
            if record["status"] != "active" or record["cutRevision"] != data.get("cutRevision"):
                raise WorkspaceConflict("Рабочая раскладка изменилась. Загрузите актуальную редакцию.")
            result = {"verified": True}
        else:
            raise RequestError("Неизвестное действие.")
        self.json_response(result)

    def stage(self, filename, contents):
        STAGING.mkdir(exist_ok=True)
        if STAGING.resolve() != STAGING:
            raise RequestError("Папка подготовки заданий должна находиться внутри приложения.")
        unique = uuid4().hex[:10]
        path = STAGING / f"{unique} - {filename}"
        temporary = STAGING / f"{unique}.tmp"
        try:
            with temporary.open("xb") as file:
                file.write(contents)
            temporary.rename(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def open_in_xiao(self, path):
        from xiao_desktop import open_dxf

        try:
            mode = open_dxf(self.server.xiao_path, path, (LOCALIZED_XIAO,))
        except (ValueError, OSError) as error:
            return {"opened": False, "message": f"{error} DXF сохранён. Откройте его в Xiao вручную."}
        if mode == "queued":
            message = "DXF передан в окно Xiao. Проверьте, что чертёж открылся."
        else:
            message = "Xiao запущен с подготовленным DXF."
        return {"opened": True, "message": message}

    def reveal(self, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[0-9a-f]{10} - [^/\\]+\.dxf", filename):
            raise RequestError("Выберите подготовленный DXF.")
        path = STAGING / filename
        if path.resolve() != path or not path.is_file():
            raise RequestError("Подготовленный файл не найден.", HTTPStatus.NOT_FOUND)
        if os.name != "nt":
            raise RequestError("Открытие папки доступно в Windows.")
        subprocess.Popen(["explorer.exe", f"/select,{path}"], creationflags=subprocess.CREATE_NO_WINDOW)
        self.json_response({"path": str(path)})

    def body(self, maximum, content_type):
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
            raise RequestError("Передайте обычное тело запроса без сжатия.")
        headers = Message()
        headers["Content-Type"] = self.headers.get("Content-Type", "")
        if headers.get_content_type() != content_type:
            raise RequestError("Неверный тип содержимого запроса.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,12}", length) or int(length) < 1:
            raise RequestError("Тело запроса пустое или размер не указан.")
        if int(length) > maximum:
            raise RequestError(f"Размер запроса превышает {maximum // (1024 * 1024)} МБ.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        data = self.rfile.read(int(length))
        if len(data) != int(length):
            raise RequestError("Получен неполный запрос.")
        return data

    def json_body(self, maximum=MAX_JSON):
        try:
            value = json.loads(self.body(maximum, "application/json").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
            raise RequestError("Некорректные параметры запроса.") from error
        if not isinstance(value, dict):
            raise RequestError("Параметры запроса должны быть объектом JSON.")
        if self.demo_cut_scope:
            if "cutId" in value and value["cutId"] != self.demo_cut_scope:
                raise WorkspaceConflict("Выбран другой материал демораскроя.")
            job = value.get("job")
            if isinstance(job, dict) and "cutContext" in job:
                context = job["cutContext"]
                if not isinstance(context, list) or not context or context[0] != self.demo_cut_scope:
                    raise WorkspaceConflict("Раскладка относится к другому материалу демораскроя.")
        return value

    def json_response(self, value, status=HTTPStatus.OK):
        self.respond(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def respond(self, contents, content_type, status=HTTPStatus.OK, filename=None, *, fallback_filename="plotter.dxf", extra_headers=()):
        self.response_headers(len(contents), content_type, status, filename, fallback_filename, extra_headers=extra_headers)
        self.wfile.write(contents)

    def response_headers(self, size, content_type, status, filename=None, fallback_filename="plotter.dxf", *, encoding=None, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin" if self.server.local_companion_ui else "no-referrer")
        connect = f"; connect-src 'self' http://127.0.0.1:{COMPANION_PORT}" if self.server.central_mode else ""
        font = "; font-src data:" if content_type == "image/svg+xml; charset=utf-8" else ""
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'" + connect + font)
        if (self.server.companion_mode and not self.server.local_companion_ui
                and self.headers.get("Origin") == self.server.companion_origin):
            self.send_header("Access-Control-Allow-Origin", self.server.companion_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Plotter-Companion")
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Plotter-Label-Id, X-Plotter-Label-Claim")
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename={fallback_filename}; filename*=UTF-8''{quote(filename, safe='')}")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()

    def log_request(self, code="-", size="-"):
        if str(code).startswith("5"):
            super().log_request(code, size)


def main():
    parser = argparse.ArgumentParser(description="Локальное веб-приложение для плоттера Xiao.")
    parser.add_argument("--port", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--central", action="store_true")
    mode.add_argument("--companion", action="store_true")
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--public-origin")
    parser.add_argument("--central-url")
    parser.add_argument("--central-ca", type=Path, help="сертификат центра сертификации для локальной панели")
    parser.add_argument("--open", action="store_true", help="открыть приложение в браузере")
    args = parser.parse_args()
    if args.central != bool(args.public_origin) or args.companion != bool(args.central_url):
        parser.error("Для --central задайте --public-origin, для --companion задайте --central-url.")
    if args.central_ca and not args.companion:
        parser.error("--central-ca используется с --companion.")
    if args.central and (not args.workspace or os.name == "nt"):
        parser.error("Для --central на Linux задайте общий файл через --workspace.")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("ezdxf").setLevel(logging.WARNING)
    port = args.port if args.port is not None else (COMPANION_PORT if args.companion else DEFAULT_PORT)
    path = args.workspace or (args.profile.parent / "workspace.json" if args.companion else WORKSPACE_PATH)
    with PlotterServer(port, path, profile_path=args.profile, public_origin=args.public_origin,
                       companion_origin=args.central_url, central_ca=args.central_ca) as server:
        if not server.companion_mode:
            Thread(target=server.production.backfill_metrics, name="order-metrics-backfill", daemon=True).start()
        url = f"http://{LOCAL_ADDRESS}:{server.server_port}"
        print(f"Плоттер запущен: {url}", flush=True)
        if args.open:
            Timer(0.2, webbrowser.open, args=[url]).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
