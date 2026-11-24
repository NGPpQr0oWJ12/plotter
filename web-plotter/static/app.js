"use strict";

const $ = (id) => document.getElementById(id);
const svg = $("drawing-svg");
const partLegend = $("part-legend");
const partLegendList = $("part-legend-list");
const partLegendSearch = $("part-legend-search");
const partLegendContent = $("part-legend-content");
const partLegendToggle = $("part-legend-toggle");
const svgNamespace = "http://www.w3.org/2000/svg";
const formatNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const formatFeedDistance = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const controllerDirections = {
  xMinus: { axis: "x", direction: -1 }, xPlus: { axis: "x", direction: 1 },
  yMinus: { axis: "y", direction: -1 }, yPlus: { axis: "y", direction: 1 },
};
const runLabels = {
  idle: "Ожидание", transferring: "Передача", ready: "Загружено", starting: "Запуск отправлен",
  running: "Раскрой", pausing: "Пауза отправлена", paused: "На паузе", resuming: "Продолжение отправлено",
  cancelling: "Отмена отправлена", completed: "Завершён", cancelled: "Отменён",
  feeding: "Протяжка", unknown: "Сверьте пульт", error: "Ошибка",
};
const activeRunStates = new Set(["starting", "running", "pausing", "paused", "resuming", "cancelling", "feeding", "unknown", "error"]);
const holdButtons = Array.from(document.querySelectorAll("[data-controller-hold]"));
const operationButtons = Array.from(document.querySelectorAll("[data-operation]"));
const placementFields = ["offset-x", "offset-y", "rotation", "size-width", "size-height"];
const DEFAULT_FABRIC_WIDTH_MM = 1380;
const MIN_RULER_STEP_MM = 5;
const RULER_SPACING_PX = 88;
const CLOSE_ZOOM_SPACING_PX = 60;
const ROTATION_SNAP_STEP = 45;
const MEASUREMENT_SNAP_PX = 14;
const MEASUREMENT_AXIS_THRESHOLD_PX = 5;
const MEASUREMENT_COLORS = ["#171717", "#535353", "#6c6c6c", "#343434", "#808080"];
const sectionColors = ["#373737", "#5a5a5a", "#444444", "#686868"];
const trackedHeadStates = new Set(["ready", "starting", "running", "pausing", "paused", "resuming", "cancelling"]);
const number = (value) => formatNumber.format(value);
const included = () => placed.filter((_, index) => !excluded.has(index));
const focused = () => document.visibilityState === "visible" && document.hasFocus();
const demoSwitch = $("demo-switch");
const newCutDialog = $("new-cut-dialog");
const rollResetDialog = $("roll-reset-dialog");
const moveToDialog = $("move-to-dialog");

let demoMode = readStoredBoolean("plotter.demoMode");
const companionAddress = "http://127.0.0.1:8767";
const companionRoutes = new Set(["/api/status", "/api/cut-plan", "/api/controller-live", "/api/control-session",
  "/api/controller-hold", "/api/controller-operation", "/api/controller-upload", "/api/roll-reset", "/api/cut-context"]);
let managedCut = null;
let workplaces = null;
let hardwareCutId = null;
const layoutVisible = () => !$("layout-workplace").hidden;
const layoutReadOnly = () => managedCut?.mode === "preview" || managedCut?.mode === "operator" && managedCut?.status === "done";
const kitLayout = () => managedCut?.mode === "kit";
const layoutPreparation = () => kitLayout() || managedCut?.mode === "design" || managedCut?.mode === "preview";
const systemRoutes = new Set(["/api/app-update", "/api/release-notes"]);
let centralMode = false;
let companionConnected = false;
let companionConnecting = false;
let demoAvailable = false;
let switchingMachineMode = false;
let machineModeEpoch = 0;
let appUpdate = null;
let appUpdateTarget = null;
let appUpdateSubmitting = false;
let appUpdateError = "";
let appUpdateRead = null;
let appUpdateTimer;
let appUpdateDeadline = 0;
let appUpdateDiscoveryReads = 0;
let releaseNotesRequest;
let machine = { ip: "—", width: 2500, height: 1650 };
let nestingBackend = "checking";
let live = { responded: false, busy: false, hold: {}, operations: {}, position: null };
let liveObserved = false;
let liveReading = null;
let liveQueued = false;
let liveTimer;
let liveEpoch = 0;
let control = null;
let controlTimer;
let controlRenewing = null;
let focusEpoch = 0;
let pendingCommand = null;
let stopPending = false;
let stopRevision = 0;
let coordinateMove = null;
let actuatorEpoch = null;
let actuatorRevision = -1;
let operationRefreshUntil = 0;
const materialOperations = new Set(["clamp", "raise-clamp", "vacuum", "vacuum-off"]);
let activeHold = null;
let uploadUncertain = false;
let currentUpload = null;
const uploadConfirmDialog = $("upload-confirm-dialog");
let uploadConfirmation = null;
let rollResetIntent = null;
let transferChecklistPlanId = null;
let activeRun = null;
let controllerRoll = null;
let rollPositioningAttempted = false;
let cutProgress = null;
let drawingProgressModel = null;
let completedDrawingPaths = new Set();
let completedDrawingParts = new Set();
let restoredRunStatistics = null;
let selectedCutPage = 1;
let pagePickerPlan = null;
let shownCutPage = null;
let previewCutPage = null;
let paintedSectionKey = "";
let followedSectionKey = "";
let headSample = null;
let headStaleTimer;
let trackedRun = readTrackedRun();
const runStatistics = new CutStatistics($("run-statistics"), formatDuration, number);

let source = null;
let placed = [];
let pathBoxes = [];
let drawingParts = new Map();
const partLegendRows = new Map();
const partLegendProducts = new Map();
let drawingPartsReady = true;
let partDiscovery = null;
let menuPartNumber = null;
let includedBounds = null;
let selection = new Set();
let selectedBounds = null;
let excluded = new Set();
const drawingUndo = [];
const drawingRedo = [];
let routeApplied = true;
let contourRepair = null;
let contourRepairProposal = null;
const contourCheckDialog = $("contour-check-dialog");
let nestingSettings = null;
let nestRequest = null;
let nestSelection = new Set();
let nestBlocking = [];
let jobError = "";
let isLoading = false;
let toastTimer;
let measureMode = false;
let measurement = null;
let savedMeasurements = new Map();
const measurementUndo = [];
const measurementRedo = [];
let measurementCloseHistory = null;
let measurePointer = null;
let measurementFrame = null;
let measurementHover = null;
let measurementCursor = null;
let drag = null;
let freeRotation = null;
let renderedPaths = [];
const notchMarkers = new Map();
const partLabels = new Map();
let fileDragDepth = 0;
// The camera uses SVG coordinates. From the operator's end, X goes up and Y goes left.
let baseCamera = null;
let camera = null;
let cameraMode = "work";
let cameraFrame = null;
let cameraViewport = null;
let workspaceRevision = null;
let restoringWorkspace = true;
let workspaceChange = 0;
let workspaceSaved = 0;
let saveTimer;
let savePromise = null;
let cutPlan = null;
let pendingCanvasPlan = null;
let cutPlanError = "";
let cutPlanEpoch = 0;
let cutPlanTimer;
let cutPlanRequest = null;
let keepViewForPlan = false;
let warningIndices = new Set();
let placementConflicts = new Set();
let sectionByPath = new Map();
let xiaoHandoff = null;
let xiaoResult = null;

const layoutStatistics = new CutStatistics($("layout-statistics"), formatDuration, number);
const simulation = new CutSimulation({
  projectPoint, element, number, statistics: layoutStatistics,
  createHeadMarker,
  markerScale: () => camera ? camera.width / Math.max(svg.clientWidth, 1) : 1,
  onPageChange: updateCanvasSection,
  onChange: () => { updateCanvasSection(); renderHead(); renderCutProgress(); renderControls(); },
  onProgress: renderCanvasStatus,
});
let clampLowered = readStoredBoolean(machineStorageKey("clampLowered"));
let vacuumEnabled = readStoredBoolean(machineStorageKey("vacuumEnabled"));
let stopNeedsVerification = false;

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const remainder = String(total % 60).padStart(2, "0");
  return hours ? [hours, String(minutes).padStart(2, "0"), remainder].join(":") : minutes + ":" + remainder;
}

function readTrackedRun() {
  try {
    return JSON.parse(sessionStorage.getItem(machineStorageKey("runStatistics")));
  } catch {
    return null;
  }
}

function snapshotRunPlan(plan, indexLimit, pageNumber = 1) {
  const page = plan?.pages?.[pageNumber - 1];
  if (!page || !Number.isFinite(plan.estimatedWorkSeconds)) return null;
  const { material, estimatedWorkSeconds, cutDistanceMm, travelDistanceMm } = plan;
  const stride = Math.max(1, Math.ceil(indexLimit / 512));
  const samples = [0];
  let index = 0;
  let seconds = 0;
  let position = [page.startX, 0];
  for (const path of page.paths) {
    const end = path.points.length + Number(path.closed);
    for (let vertex = 0; vertex < end; vertex++) {
      const point = path.points[vertex] ?? path.points[0];
      seconds += Math.hypot(point[0] - position[0], point[1] - position[1])
        / (vertex === 0 ? plan.travelSpeedMmS : plan.cutSpeedMmS);
      position = point;
      index++;
      if (index % stride === 0) samples.push(seconds);
    }
  }
  const preciseCounts = !Array.isArray(page.pathEndIndices) || page.pathEndIndices.length === page.paths.length
    && page.pathEndIndices.every((end, i) =>
      end - (page.pathEndIndices[i - 1] || 0) === page.paths[i].points.length + Number(page.paths[i].closed));
  if (index !== indexLimit || !preciseCounts) samples.length = 0;
  else if (index % stride) samples.push(seconds);
  return { material, estimatedWorkSeconds, cutDistanceMm, travelDistanceMm, indexLimit, stride, samples,
    pageStartSeconds: page.workStartSeconds ?? 0, pageSeconds: page.estimatedWorkSeconds ?? estimatedWorkSeconds };
}

function renderRunStatistics(run) {
  const matched = run.startedAt && trackedRun?.startedAt === run.startedAt && trackedRun.filename === run.filename;
  const tracking = cutProgress && cutProgress.planId === cutPlan?.planId ? cutProgress : null;
  const state = tracking?.state ?? run.state;
  const page = tracking ? cutPlan.pages[tracking.page - 1] : null;
  if (tracking && page && (restoredRunStatistics?.plan !== cutPlan || restoredRunStatistics.page !== tracking.page
      || restoredRunStatistics.indexLimit !== tracking.indexLimit)) {
    restoredRunStatistics = { plan: cutPlan, page: tracking.page, indexLimit: tracking.indexLimit,
      estimate: snapshotRunPlan(cutPlan, tracking.indexLimit || 0, tracking.page) };
  }
  const plan = tracking && page ? restoredRunStatistics?.estimate
    : matched ? trackedRun.plan : state === "ready" ? currentUpload?.statistics : null;
  runStatistics.setPlan(plan);
  let progress = null;
  let remaining = null;
  if (plan) {
    const initialSeconds = tracking ? cutPlan.pages[tracking.firstPage - 1]?.workStartSeconds ?? 0 : 0;
    const total = Math.max(0, plan.estimatedWorkSeconds - initialSeconds);
    const start = (plan.pageStartSeconds ?? 0) - initialSeconds;
    const pageSeconds = plan.pageSeconds ?? total;
    let elapsed = null;
    const pageFinished = tracking ? tracking.page < tracking.firstPage + tracking.completedPages
      : run.state === "completed" || run.completedPages === run.pages && run.pages > 0;
    const index = tracking ? tracking.index : run.fresh ? run.index : null;
    const indexLimit = tracking?.indexLimit ?? run.indexLimit;
    if (pageFinished) {
      elapsed = start + pageSeconds;
    } else if (["transferring", "ready", "starting"].includes(state)) {
      elapsed = start;
    } else if (Number.isInteger(index) && indexLimit === plan.indexLimit && indexLimit > 0) {
      const { stride, samples } = plan;
      if (samples.length) {
        const low = Math.floor(index / stride);
        const end = Math.min((low + 1) * stride, indexLimit);
        const fraction = end > low * stride ? (index - low * stride) / (end - low * stride) : 0;
        elapsed = start + samples[low] + fraction * ((samples[low + 1] ?? samples[low]) - samples[low]);
      } else {
        elapsed = start + pageSeconds * index / indexLimit;
      }
    }
    if (Number.isFinite(elapsed)) {
      progress = total ? elapsed / total : 1;
      remaining = Math.max(0, total - elapsed);
    }
  }
  runStatistics.update(progress, remaining);
}

function messageFrom(error) {
  return error.message || "Не удалось выполнить действие.";
}

function updateText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function updateAttribute(node, name, value) {
  if (node.getAttribute(name) !== value) node.setAttribute(name, value);
}

function updateControl(node, property, value) {
  if (node[property] !== value) node[property] = value;
}

async function checkedFetch(url, options = {}, useCompanion = companionRoutes.has(url)) {
  let response;
  try {
    const path = demoMode && url.startsWith("/api/") && !systemRoutes.has(url) ? "/demo" + url : url;
    response = !demoMode && centralMode && companionConnected && useCompanion
      ? await companionFetch(url, options) : await fetch(path, { cache: "no-store", ...options });
  } catch (error) {
    if (error instanceof TypeError) throw new Error("Локальный сервер недоступен. Проверьте, что он запущен.");
    throw error;
  }
  if (response.ok) return response;
  let details;
  try { details = await response.json(); } catch { /* An HTTP error can have an empty body. */ }
  const failure = new Error(details?.error || `Сервер вернул ошибку ${response.status}.`);
  failure.status = response.status;
  failure.details = details;
  throw failure;
}

function companionFetch(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.method === "POST") headers.set("X-Plotter-Companion", "1");
  return fetch(companionAddress + path, { cache: "no-store", credentials: "omit", redirect: "error",
    targetAddressSpace: "loopback", ...options, headers });
}

async function connectCompanion() {
  if (!centralMode || demoMode || companionConnected || companionConnecting || busy()) return;
  companionConnecting = true;
  const modeEpoch = machineModeEpoch;
  renderControls();
  try {
    const response = await companionFetch("/api/status", { signal: AbortSignal.timeout(3500) });
    if (!response.ok) throw new Error();
    const status = await response.json();
    if (modeEpoch !== machineModeEpoch || demoMode) return;
    if (!status.companionMode || status.companionProtocol !== 1) throw new Error();
    if (status.apiVersion < 22) {
      setMachineError("Обновите помощник Plotter до версии 2.0, чтобы открыть раскрой из наряда.");
      return;
    }
    if (status.machine.width !== machine.width || status.machine.height !== machine.height) {
      setMachineError("Размер рабочего поля в профиле помощника отличается от общей настройки.");
      return;
    }
    companionConnected = true;
    setMachineError();
    if (!await readConfiguration()) throw new Error();
    try { sessionStorage.setItem("plotter.companion", "true"); } catch { /* Storage is optional. */ }
    scheduleLiveRead(0);
  } catch {
    companionConnected = false;
    if (modeEpoch === machineModeEpoch) setMachineError("Нет связи с помощником. Проверьте, что он запущен и браузеру разрешён локальный доступ.");
  } finally {
    companionConnecting = false;
    renderControls();
  }
}

async function getJson(url, options, useCompanion) {
  return (await checkedFetch(url, options, useCompanion)).json();
}

function jsonRequest(body, signal) {
  return {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  };
}

function appUpdateSafe() {
  return !busy() && !drawingLocked({ editing: false }) && !workplaces?.hasPendingCadEdits?.() && !drag
    && !runInProgress() && !controlActive() && !live.busy
    && !live.stopAvailable && !needsMotionVerification() && !coordinateMoveBlocksControl()
    && !currentUpload && !uploadUncertain && !xiaoHandoff && activeRun?.state !== "transferring";
}

function appUpdateInProgress() {
  return appUpdate?.state === "preparing" || appUpdate?.state === "restarting";
}

function appUpdateRevision(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

function availableAppUpdate() {
  if (appUpdate?.supported !== true || typeof appUpdate.availableVersion !== "string" || !appUpdate.availableVersion) return null;
  const version = appUpdate.availableVersion;
  const revision = appUpdateRevision(appUpdate.availableRevision);
  return version !== appUpdate.version || revision > appUpdateRevision(appUpdate.revision) ? { version, revision } : null;
}

function showAppUpdateError(message) {
  appUpdateError = message;
  if (!$("release-notes-dialog").open) notifyError(message);
}

function updateCompanionDownload(available) {
  if (typeof available === "boolean") updateControl($("release-notes-download"), "hidden", !available);
}

function renderAppUpdate() {
  const version = $("app-version");
  const release = $("release-notes-button");
  const button = $("app-update");
  const status = $("app-update-status");
  const progress = appUpdateSubmitting || Boolean(appUpdateTarget) || appUpdateInProgress();
  const target = availableAppUpdate();
  const targetLabel = target?.revision ? `${target.version} (сборка ${target.revision})` : target?.version;
  const showUpdate = progress || Boolean(target);
  const progressLabel = appUpdate?.state === "restarting" ? "Перезапускаем…" : "Обновляем помощник…";
  const availableMessage = targetLabel ? `Доступно обновление помощника до версии ${targetLabel}.` : "";
  const blocked = target && !progress && !appUpdateSafe();
  const blockedMessage = blocked
    ? workplaces?.hasPendingCadEdits?.() ? "Сохраните чертёж и дождитесь завершения работы в редакторе."
      : "Завершите текущую операцию перед обновлением."
    : "";
  const message = [appUpdateError || appUpdate?.message, progress ? progressLabel : blockedMessage || availableMessage]
    .filter(Boolean).join(" ");
  updateText(version, appUpdate?.version ?? "");
  updateControl(version, "hidden", !appUpdate?.version);
  updateControl(version, "title", appUpdate?.revision ? `Сборка ${appUpdate.revision}` : "");
  updateText(release, progress ? progressLabel : target ? "Обновить" : "Релиз");
  release.classList.toggle("release-update-available", showUpdate);
  updateControl(release, "title", progress ? progressLabel : availableMessage);
  const actionLabel = targetLabel ? `Обновить помощник до ${targetLabel}` : "Обновить помощник";
  updateText(button, progress ? progressLabel : actionLabel);
  updateControl(button, "hidden", !showUpdate);
  updateControl(button, "disabled", progress || !target || Boolean(blocked));
  updateControl(button, "title", blockedMessage);
  updateControl($("release-notes-actions"), "hidden", !showUpdate && $("release-notes-download").hidden);
  updateText(status, message);
  updateControl(status, "hidden", !message);
}

async function openReleaseNotes() {
  const dialog = $("release-notes-dialog");
  if (dialog.open) return;
  releaseNotesRequest?.abort();
  const request = new AbortController();
  releaseNotesRequest = request;
  const title = $("release-notes-title");
  const status = $("release-notes-status");
  const list = $("release-notes-list");
  const content = $("release-notes-content");
  const installed = appUpdate?.version;
  updateText(title, installed ? `Релиз ${installed}` : "Релиз");
  list.replaceChildren();
  list.hidden = true;
  status.hidden = false;
  updateText(status, "Загружаем список изменений…");
  updateAttribute(content, "aria-busy", "true");
  dialog.showModal();
  renderAppUpdate();
  void readAppUpdate();
  try {
    const result = await getJson("/api/release-notes", { signal: AbortSignal.any([request.signal, AbortSignal.timeout(8000)]) });
    if (request.signal.aborted) return;
    updateCompanionDownload(result?.companionDownload);
    renderAppUpdate();
    if (typeof result?.version !== "string" || !Array.isArray(result.notes)) throw new Error();
    const notes = result.notes.filter((note) => typeof note === "string" && note.trim());
    updateText(title, `Релиз ${result.version}`);
    if (notes.length) {
      const items = notes.map((note) => {
        const item = document.createElement("li");
        item.textContent = note;
        return item;
      });
      list.replaceChildren(...items);
      list.hidden = false;
      status.hidden = true;
    } else updateText(status, "Для этой версии список изменений отсутствует.");
  } catch {
    if (!request.signal.aborted) updateText(status, "Не удалось загрузить список изменений. Откройте релиз ещё раз.");
  } finally {
    if (releaseNotesRequest === request) updateAttribute(content, "aria-busy", "false");
  }
}

function scheduleAppUpdateRead(delay = 60_000) {
  clearTimeout(appUpdateTimer);
  appUpdateTimer = setTimeout(() => void readAppUpdate(), delay);
}

function readAppUpdate() {
  if (appUpdateRead) return appUpdateRead;
  appUpdateRead = (async () => {
    const wasRestarting = appUpdate?.state === "restarting";
    try {
      const status = await getJson("/api/app-update", { signal: AbortSignal.timeout(3500) });
      updateCompanionDownload(status?.companionDownload);
      if (typeof status?.version !== "string") return;
      appUpdate = status;
      if (appUpdateTarget && status.version === appUpdateTarget.version
          && appUpdateRevision(status.revision) >= appUpdateTarget.revision) {
        if (workspaceChange === workspaceSaved && appUpdateSafe()) {
          location.reload();
          return;
        }
      } else if (appUpdateTarget && status.state === "failed") {
        appUpdateTarget = null;
        showAppUpdateError(status.message || "Не удалось обновить программу.");
      }
    } catch {
      // The server can be unavailable while Windows replaces it.
    } finally {
      appUpdateRead = null;
      if (appUpdateTarget && Date.now() >= appUpdateDeadline) {
        appUpdateTarget = null;
        showAppUpdateError("Не удалось подтвердить обновление. Проверьте, запущена ли программа.");
      }
      if (wasRestarting !== (appUpdate?.state === "restarting")) renderControls();
      else renderAppUpdate();
      const discovering = appUpdate?.supported && !appUpdate.availableVersion && !appUpdateInProgress()
        && ($("release-notes-dialog").open || appUpdateDiscoveryReads++ < 3);
      scheduleAppUpdateRead(appUpdateTarget || appUpdateInProgress() || discovering ? 1500 : 60_000);
    }
  })();
  return appUpdateRead;
}

async function applyAppUpdate() {
  const target = availableAppUpdate();
  if (!target || appUpdateSubmitting || appUpdateTarget || appUpdateInProgress() || !appUpdateSafe()) {
    renderAppUpdate();
    return;
  }
  appUpdateError = "";
  appUpdateSubmitting = true;
  renderAppUpdate();
  try {
    if (workspaceChange !== workspaceSaved && (kitLayout() || !await persistWorkspace())) {
      throw new Error("Сохраните рабочую область перед обновлением.");
    }
    if (!appUpdateSafe() || workspaceChange !== workspaceSaved) {
      throw new Error("Завершите текущую операцию перед обновлением.");
    }
    appUpdateTarget = target;
    appUpdateDeadline = Date.now() + 10 * 60_000;
    try {
      await checkedFetch("/api/app-update", jsonRequest(target, AbortSignal.timeout(8000)));
    } catch (error) {
      // The process may restart before its accepted response reaches the browser.
      if (error.status !== undefined) throw error;
      appUpdateDeadline = Date.now() + 30_000;
    }
  } catch (error) {
    appUpdateTarget = null;
    showAppUpdateError(messageFrom(error));
  } finally {
    appUpdateSubmitting = false;
    renderAppUpdate();
    scheduleAppUpdateRead(0);
  }
}

function notifyError(message) {
  clearTimeout(toastTimer);
  $("toast-text").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 7500);
}

function element(name, attributes = {}) {
  const node = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function rulerText(text, x, y, size, anchor = "middle", className = "ruler-text") {
  const label = element("text", { x, y, class: className, "text-anchor": anchor });
  label.style.fontSize = `${size}px`;
  label.textContent = text;
  return label;
}

function projectPoint(x, y) {
  // This mapping is its own inverse, so it also converts SVG points back to machine coordinates.
  return [-y, -x];
}

function colorCutSection(node, section) {
  const color = sectionColors[(section - 1) % sectionColors.length];
  node.style.setProperty("--section-color", color);
  node.dataset.cutSection = section;
  return node;
}

function gridSpacing(units) {
  const minimum = Math.max(2 * MIN_RULER_STEP_MM, units * RULER_SPACING_PX);
  const decade = 10 ** Math.floor(Math.log10(minimum));
  const multiplier = [1, 2, 2.5, 5].find((value) => value * decade >= minimum) ?? 10;
  const major = multiplier * decade;
  const minor = Math.max(MIN_RULER_STEP_MM, major / (multiplier === 2 ? 4 : 5));
  return { major, minor };
}

function renderTable() {
  if (!camera) return;
  const { width, height } = machine;
  const length = Math.max(width, includedBounds?.maxX || 0);
  const layer = $("table-layer");
  const units = camera.width / Math.max(svg.clientWidth, 1);
  const surface = svgBox({ minX: 0, minY: 0, maxX: length, maxY: height });
  const parts = [element("rect", { ...surface, class: "table-background" })];
  const materialWidth = nestingSettings?.widthMm ?? (cutWidthPending() ? 0 : height);
  const fabric = includedBounds && materialWidth > 0 ? Math.min(materialWidth, height) : 0;
  if (fabric) {
    parts.push(element("rect", { x: -fabric, y: -length, width: fabric, height: length, class: "material-surface" }));
  }
  const { major, minor } = gridSpacing(units);
  const firstX = Math.max(0, -camera.y - camera.height);
  const lastX = Math.min(length, -camera.y);
  const firstY = Math.max(0, -camera.x - camera.width);
  const lastY = Math.min(height, -camera.x);
  const verticalRuler = Math.min(0, Math.max(camera.x + 3 * units, camera.x + camera.width - 76 * units));
  const horizontalRuler = Math.min(0, camera.y + camera.height - 25 * units);
  const grid = [[], []];
  const ticks = [[], []];
  const labels = [];
  if (firstX <= lastX && firstY <= lastY) {
    if (verticalRuler < 0) ticks[1].push(`M${verticalRuler} ${-lastX}V${Math.min(-firstX, horizontalRuler)}`);
    if (horizontalRuler < 0) ticks[1].push(`M${-lastY} ${horizontalRuler}H${Math.min(-firstY, verticalRuler)}`);
    const step = minor;
    for (let y = Math.ceil(firstY / step) * step; y <= lastY; y += step) {
      const main = y % major === 0;
      const level = Number(main);
      grid[level].push(`M${-y} ${-lastX}V${-firstX}`);
      if (verticalRuler < 0 && -y > verticalRuler - 8 * units) continue;
      ticks[level].push(`M${-y} ${horizontalRuler}v${(main ? 7 : 3) * units}`);
      if (main && -y >= camera.x + 16 * units) labels.push(rulerText(number(y), -y, horizontalRuler + 21 * units, 12 * units));
    }
    for (let x = Math.max(step, Math.ceil(firstX / step) * step); x <= lastX; x += step) {
      const main = x % major === 0;
      const level = Number(main);
      grid[level].push(`M${-lastY} ${-x}H${-firstY}`);
      if (-x > horizontalRuler - 8 * units) continue;
      ticks[level].push(`M${verticalRuler} ${-x}h${(main ? 7 : 3) * units}`);
      if (main && -x >= camera.y + 9 * units) labels.push(rulerText(number(x), verticalRuler + 11 * units, -x + 4 * units, 12 * units, "start"));
    }
  }
  for (const [index, lines] of grid.entries()) {
    if (lines.length) parts.push(element("path", { d: lines.join(" "), class: index ? "major-line" : "minor-line",
      fill: "none", "vector-effect": "non-scaling-stroke" }));
  }
  parts.push(element("rect", { ...surface, class: "table-border", "vector-effect": "non-scaling-stroke" }));
  if (fabric) {
    parts.push(element("path", {
      d: `M${-fabric} 0V${-length}`, class: "material-edge", "vector-effect": "non-scaling-stroke",
    }));
  }

  const rulers = [];
  for (const [index, lines] of ticks.entries()) if (lines.length) rulers.push(element("path", {
    d: lines.join(" "), class: index ? "ruler-tick" : "ruler-tick minor", fill: "none", "vector-effect": "non-scaling-stroke",
  }));
  rulers.push(...labels);
  const axis = 25 * units;
  const tip = 3 * units;
  const axisPath = `M0 ${-axis}V0H${-axis} M${-tip} ${tip - axis}l${tip} ${-tip} ${tip} ${tip} M${tip - axis} ${-tip}l${-tip} ${tip} ${tip} ${tip}`;
  rulers.push(element("path", { d: axisPath, class: "axis-mark", "vector-effect": "non-scaling-stroke" }));
  rulers.push(rulerText("Y", -axis - 7 * units, 3 * units, 11 * units, "end", "axis-label"));
  rulers.push(rulerText("X", 0, -axis - 5 * units, 11 * units, "middle", "axis-label"));
  layer.replaceChildren(...parts);
  $("ruler-layer").replaceChildren(...rulers);
}

function canvasSection(plan = cutPlan) {
  if (plan?.mode !== "roll") return null;
  const tracked = trackedCanvasRun();
  const matching = tracked?.planId === plan.planId && !["completed", "cancelled", "error"].includes(tracked.state);
  const waiting = matching && tracked.state === "advance";
  const kind = simulation.active ? "simulation" : waiting ? "next" : matching ? "current" : "preview";
  const number = simulation.active ? simulation.visiblePage
    : waiting ? tracked.page + 1 : matching ? tracked.page : previewCutPage ?? shownCutPage ?? 1;
  const page = plan.pages[number - 1] ?? plan.pages[0];
  return page ? { page, kind } : null;
}

function trackedCanvasRun() {
  if (controllerRoll) return cutProgress?.planId === controllerRoll.planId ? cutProgress : controllerRoll;
  return cutProgress;
}

function canShowNextLaunchPass(section = canvasSection()) {
  return Boolean(cutPlan && section && section.page.number < cutPlan.pages.length
    && section.kind !== "current" && section.kind !== "next" && !currentUpload
    && !drawingLocked({ editing: false }) && !busy() && !cutPlacementPreviewActive());
}

function updateCanvasSection() {
  const plan = cutPlan ?? pendingCanvasPlan;
  const section = canvasSection(plan);
  const tools = $("canvas-section-tools");
  updateControl(tools, "hidden", !section);
  const passNavigation = $("launch-pass-navigation");
  updateControl(passNavigation, "hidden", !section || plan.pages.length < 2);
  if (section) {
    const page = section.page;
    updateText($("section-focus"), `Проход ${page.number} из ${plan.pages.length}`);
    updateText($("launch-view-page"), `На схеме: ${page.number} из ${plan.pages.length}`);
    updateControl($("launch-next-pass"), "disabled", !canShowNextLaunchPass(section));
    updateControl($("section-focus"), "disabled", !cutPlan);
    const locked = !cutPlan || section.kind === "current" || section.kind === "next";
    updateControl($("section-prev"), "disabled", locked || page.number === 1);
    updateControl($("section-next"), "disabled", locked || page.number === plan.pages.length);
  }
  const pageKey = section ? `${plan.planId}/${section.page.number}` : "";
  const paintKey = section ? `${pageKey}/${section.kind}/${Boolean(cutPlan)}` : "";
  if (paintKey === paintedSectionKey) return;
  paintedSectionKey = paintKey;
  if (pageKey !== followedSectionKey) {
    followedSectionKey = pageKey;
    if (cameraMode === "work" && !drag && !keepViewForPlan) {
      resizeCamera();
      return;
    }
  }
  renderPages();
}

function navigateCanvasSection(direction) {
  const section = canvasSection();
  if (section) openCanvasSection(section.page.number + direction);
}

function openCanvasSection(page) {
  const section = canvasSection();
  if (!section || !cutPlan.pages[page - 1]) return false;
  if (section.kind === "current" || section.kind === "next") {
    if (page !== section.page.number) return false;
    fitCamera();
    return true;
  }
  cameraMode = "work";
  if (simulation.active && section.page.number !== page) simulation.seek(page - 1, simulation.running);
  else {
    if (!simulation.active) previewCutPage = page;
    updateCanvasSection();
  }
  if (section.page.number === page) resizeCamera();
  scheduleWorkspaceSave({ viewOnly: true });
  return true;
}

function renderSeamMarkers(plan, selected, units, elements, tags) {
  const pages = plan.pages;
  const seamsByBoundary = new Map();
  for (const seam of plan.seams || []) {
    if (!seamsByBoundary.has(seam.x)) seamsByBoundary.set(seam.x, []);
    seamsByBoundary.get(seam.x).push(seam);
  }
  const boundaries = pages.slice(1).flatMap((page, index) => {
    const seams = page.incomingSeams ?? seamsByBoundary.get(page.startX) ?? [];
    const previous = pages[index];
    const y = -page.startX;
    return (seams.length || page.canStart === false) && y >= camera.y && y <= camera.y + camera.height
      ? [{ previous, page, seams, y, active: selected === previous || selected === page }] : [];
  });
  if (!boundaries.length) return;
  boundaries.sort((a, b) => b.active - a.active);
  const left = -Math.max(machine.height, includedBounds?.maxY ?? 0);
  const right = -Math.min(0, includedBounds?.minY ?? 0);
  for (const { previous, page, seams, y, active } of boundaries) {
    const activeClass = active ? " is-active" : "";
    const description = `Граница проходов ${previous.number} и ${page.number}. Раскрой детали продолжается на следующем проходе.`;
    const marker = element("g", { class: "page-seam-group" + activeClass, role: "img", "aria-label": description });
    const title = element("title");
    title.textContent = description;
    marker.append(title);
    elements.push(element("path", {
      d: `M${left} ${y}H${right}`, class: "page-seam-line" + activeClass, "vector-effect": "non-scaling-stroke",
    }));
    const points = [...new Set(seams.map(({ y: across }) => -across))]
      .filter((x) => x >= camera.x - 10 * units && x <= camera.x + camera.width + 10 * units);
    for (const [kind, radius] of [["halo", 10], ["dot", 7.5], ["core", 2.5]]) {
      for (const x of points) marker.append(element("circle", { cx: x, cy: y, r: radius * units, class: "page-seam-" + kind }));
    }
    tags.push(marker);
  }
}

function renderPages() {
  const layer = $("page-layer");
  const annotations = $("page-annotation-layer");
  const plan = cutPlan ?? pendingCanvasPlan;
  if (!camera || plan?.mode !== "roll") {
    layer.replaceChildren();
    annotations.replaceChildren();
    return;
  }
  const units = camera.width / Math.max(svg.clientWidth, 1);
  const elements = [];
  const tags = [];
  const section = canvasSection(plan);
  const selected = section?.page;
  const width = Math.min(plan.material?.widthMm ?? machine.height, machine.height);
  if (selected) {
    const highlight = element("rect", {
      x: -width, y: -selected.endX, width, height: selected.endX - selected.startX,
      class: `page-highlight is-${section.kind}`, "vector-effect": "non-scaling-stroke",
    });
    elements.push(colorCutSection(highlight, selected.number));
  }
  const tagWidth = 94;
  const tagHeight = 28;
  const dimensionX = -machine.height - 9 * units;
  const tagX = dimensionX - (tagWidth + 5) * units;
  const visibleTop = camera.y + 20 * units;
  const visibleBottom = camera.y + camera.height - 20 * units;
  const viewingLocked = section?.kind === "current" || section?.kind === "next";
  for (const page of plan.pages) {
    if (-page.startX < camera.y || -page.endX > camera.y + camera.height) continue;
    if (page.number > 1) {
      elements.push(element("path", {
        d: `M0 ${-page.startX}H${-machine.height}`, class: "page-boundary", "vector-effect": "non-scaling-stroke",
      }));
    }
    if (page.endX <= page.startX || tagX < camera.x + 6 * units
        || dimensionX > camera.x + camera.width || (page.endX - page.startX) / units < tagHeight + 5) continue;
    const y1 = -page.endX;
    const y2 = -page.startX;
    const center = Math.max(y1 + tagHeight * units / 2, visibleTop + tagHeight * units / 2);
    const bottom = Math.min(y2 - tagHeight * units / 2, visibleBottom - tagHeight * units / 2);
    if (center > bottom) continue;
    const y = Math.min(bottom, Math.max(center, (y1 + y2) / 2));
    const tick = 4 * units;
    tags.push(element("path", {
      d: `M${dimensionX} ${y1}V${y2} M${dimensionX - tick} ${y1}h${2 * tick} M${dimensionX - tick} ${y2}h${2 * tick} M${dimensionX} ${y}h${-5 * units}`,
      class: "page-dimension", "vector-effect": "non-scaling-stroke",
    }));
    const length = `${number(page.endX - page.startX)} мм`;
    const tag = element("g", {
      class: `page-tag${page === selected ? " is-active" : ""}`,
      transform: `translate(${tagX} ${y - tagHeight * units / 2}) scale(${units})`,
      ...(cutPlan && (!viewingLocked || page === selected) ? {
        role: "button", tabindex: 0, "data-canvas-page": page.number,
        "aria-label": `Открыть проход ${page.number}, ${length}`,
      } : {}),
    });
    tag.append(
      element("rect", { width: tagWidth, height: tagHeight, rx: 4, class: "page-tag-surface" }),
      rulerText(`Проход ${page.number}`, 7, 11, 11, "start", "page-tag-title"),
      rulerText(length, 7, 23, 10, "start", "page-tag-size"),
    );
    tags.push(colorCutSection(tag, page.number));
  }
  renderSeamMarkers(plan, selected, units, elements, tags);
  layer.replaceChildren(...elements);
  annotations.replaceChildren(...tags);
}

function createHeadMarker() {
  const marker = element("g", { class: "tool-head", "aria-hidden": "true", "pointer-events": "none" });
  marker.append(
    element("path", { d: "M-13 0h5m16 0h5M0 -13v5m0 16v5", class: "head-cross", "vector-effect": "non-scaling-stroke" }),
    element("circle", { r: 7, class: "head-halo", "vector-effect": "non-scaling-stroke" }),
    element("circle", { r: 2.5, class: "head-marker" }),
  );
  return marker;
}

const headMarker = createHeadMarker();
headMarker.classList.add("live-head");
headMarker.setAttribute("hidden", "");
$("head-layer").append(headMarker);

function liveHeadFrame() {
  if (pendingCommand === "upload" || live.externalMotion) return null;
  const tracked = cutProgress;
  if (tracked && trackedHeadStates.has(tracked.state)) {
    if (tracked.planId !== cutPlan?.planId) return null;
    return { key: `${tracked.planId}/${tracked.page}`, cutting: tracked.state !== "ready" };
  }
  if (runInProgress() || controllerRoll || (tracked && tracked.pages > 1)) return null;
  if (currentUpload && !tracked) return null;
  return { key: "table", cutting: false };
}

function observeHeadPosition(position, drawingPosition) {
  const frame = liveHeadFrame();
  if (!frame) {
    headSample = null;
    return;
  }
  const point = frame.key === "table" ? position : drawingPosition;
  if (frame.key !== "table" && (!point || `${point.planId}/${point.page}` !== frame.key)) {
    if (headSample?.key === frame.key) headSample.confirmed = false;
    return;
  }
  if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return;
  headSample = { key: frame.key, point: projectPoint(point.x, point.y), at: performance.now(), confirmed: true };
}

function renderHead(now = performance.now()) {
  const frame = liveHeadFrame();
  const age = headSample ? Math.max(0, now - headSample.at) : Infinity;
  const freshFor = frame?.cutting ? 1200 : 2800;
  const visibleFor = 4000;
  const stale = !headSample?.confirmed || !live.positionFresh || live.busy || !live.responded || age >= freshFor;
  const hidden = !camera || simulation.active || !frame || headSample?.key !== frame.key || age >= visibleFor;
  headMarker.toggleAttribute("hidden", hidden);
  headMarker.classList.toggle("is-stale", stale);
  clearTimeout(headStaleTimer);
  if (!hidden) {
    const [x, y] = headSample.point;
    const units = camera.width / Math.max(svg.clientWidth, 1);
    updateAttribute(headMarker, "transform", `translate(${x} ${y}) scale(${units})`);
    headStaleTimer = setTimeout(renderHead, Math.max(1, (stale ? visibleFor : freshFor) - age));
  }
}

function drawCamera() {
  if (cameraFrame !== null) {
    cancelAnimationFrame(cameraFrame);
    cameraFrame = null;
  }
  if (!camera) return;
  updateAttribute(svg, "viewBox", `${camera.x} ${camera.y} ${camera.width} ${camera.height}`);
  renderTable();
  renderPages();
  renderHead();
  renderPartLabels();
  renderFreeRotation();
  renderManualGuides();
  renderMeasurement(true);
  simulation.render();
}

function scheduleCameraDraw() {
  if (cameraFrame !== null) return;
  cameraFrame = requestAnimationFrame(() => {
    cameraFrame = null;
    drawCamera();
  });
}

function newCamera(content, overlayInsets) {
  const { clientWidth: viewWidth, clientHeight: viewHeight } = svg;
  const leftInset = Math.max(Math.min(120, viewWidth * .32), overlayInsets.left);
  const rightInset = Math.max(Math.min(80, viewWidth * .32), overlayInsets.right);
  const insetScale = Math.min(1, viewWidth * .65 / (leftInset + rightInset));
  const left = leftInset * insetScale;
  const right = rightInset * insetScale;
  const top = Math.min(32, viewHeight * .22);
  const bottom = Math.min(34, viewHeight * .2);
  const units = Math.max(content.width / (viewWidth - left - right),
    content.height / (viewHeight - top - bottom));
  const width = viewWidth * units;
  const height = viewHeight * units;
  return {
    x: content.x + (content.width - width + (right - left) * units) / 2,
    y: content.y + (content.height - height + (bottom - top) * units) / 2,
    width, height,
  };
}

function overlayWidth(id) {
  const panel = $(id);
  return panel && getComputedStyle(panel).position === "absolute" ? panel.offsetWidth + 28 : 0;
}

function resizeCamera({ keepView = false } = {}) {
  if (!svg.clientWidth || !svg.clientHeight) return;
  const { clientWidth: width, clientHeight: height } = svg;
  const oldViewport = cameraViewport;
  cameraViewport = { width, height };
  const overlayInsets = {
    left: overlayWidth("launch-controls"),
    right: overlayWidth("canvas-controls"),
  };
  const content = {
    minX: Math.min(0, includedBounds?.minX || 0), minY: Math.min(0, includedBounds?.minY || 0),
    maxX: Math.max(machine.width, includedBounds?.maxX || 0), maxY: Math.max(machine.height, includedBounds?.maxY || 0),
  };
  const previousBase = baseCamera;
  if (keepView && camera) {
    if (oldViewport && (oldViewport.width !== width || oldViewport.height !== height)) {
      const units = camera.width / oldViewport.width;
      const newWidth = width * units, newHeight = height * units;
      camera = { x: camera.x + (camera.width - newWidth) / 2, y: camera.y + (camera.height - newHeight) / 2,
        width: newWidth, height: newHeight };
      const baseUnits = baseCamera.width / oldViewport.width;
      baseCamera = { ...baseCamera, width: width * baseUnits, height: height * baseUnits };
    }
    drawCamera();
    return;
  }
  baseCamera = newCamera(svgBox(content), overlayInsets);
  if (cameraMode === "work") {
    const page = canvasSection()?.page;
    const start = page?.startX ?? content.minX;
    const end = page?.endX ?? machine.width;
    camera = newCamera(svgBox({ ...content, minX: start, maxX: end }), overlayInsets);
  } else if (cameraMode === "overview" || !camera) {
    camera = { ...baseCamera };
  } else {
    const zoomScale = camera.width / previousBase.width;
    const zoomWidth = baseCamera.width * zoomScale;
    const zoomHeight = baseCamera.height * zoomScale;
    camera = {
      x: camera.x + (camera.width - zoomWidth) / 2,
      y: camera.y + (camera.height - zoomHeight) / 2,
      width: zoomWidth,
      height: zoomHeight,
    };
  }
  drawCamera();
}

function fitCamera(mode = "work") {
  cameraMode = mode;
  resizeCamera();
  scheduleWorkspaceSave({ viewOnly: true });
}

function hasRollLayout() {
  return cutPlan?.mode === "roll" || includedBounds?.maxX > machine.width;
}

function zoom(factor, px = 0.5, py = 0.5, deferPaint = false) {
  if (!camera || !svg.clientWidth) return;
  const roll = hasRollLayout();
  const fineScale = MIN_RULER_STEP_MM * svg.clientWidth / (CLOSE_ZOOM_SPACING_PX * baseCamera.width);
  const minimumScale = Math.min(0.04, fineScale);
  const scale = Math.max(minimumScale, Math.min(roll ? 1 : 2.4, (camera.width / baseCamera.width) / factor));
  if (roll && scale === 1 && factor < 1) {
    if (cameraMode !== "overview") fitCamera("overview");
    return;
  }
  const width = baseCamera.width * scale;
  const height = baseCamera.height * scale;
  if (Math.abs(width - camera.width) < 1e-9) return;
  cameraMode = "manual";
  camera = {
    x: camera.x + (camera.width - width) * px, y: camera.y + (camera.height - height) * py, width, height,
  };
  if (deferPaint) scheduleCameraDraw();
  else drawCamera();
  scheduleWorkspaceSave({ viewOnly: true });
}

function screenFraction(event) {
  const box = svg.getBoundingClientRect();
  return [(event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height];
}

function drawingPoint(event) {
  const [px, py] = screenFraction(event);
  return projectPoint(camera.x + px * camera.width, camera.y + py * camera.height);
}

function setMeasureMode(enabled, closeHistory = null) {
  if (enabled && (!placed.length || drawingLocked())) return;
  cancelCanvasGesture();
  endFreeRotation();
  closePartMenu();
  measureMode = enabled;
  measurementCloseHistory = closeHistory;
  clearMeasurement();
  svg.classList.toggle("measuring", enabled);
  for (const [id, active] of [["select-tool", !enabled], ["measure-tool", enabled]]) {
    $(id).setAttribute("aria-pressed", String(active));
    $(id).classList.toggle("selected", active);
  }
  renderMeasurement();
  renderHistoryControls();
}

function dismissMeasurement() {
  setMeasureMode(false, savedMeasurements.size ? "closed" : null);
  svg.focus({ preventScroll: true });
}

function clearMeasurement() {
  const pointer = measurePointer;
  measurePointer = null;
  measurement = null;
  measurementHover = null;
  measurementCursor = null;
  if (pointer && svg.hasPointerCapture(pointer.id)) svg.releasePointerCapture(pointer.id);
  if (measurementFrame !== null) {
    cancelAnimationFrame(measurementFrame);
    measurementFrame = null;
  }
}

function clearSavedMeasurements() {
  savedMeasurements.clear();
  measurementUndo.length = 0;
  measurementRedo.length = 0;
  measurementCloseHistory = null;
  clearMeasurement();
  renderMeasurementList();
}

function rememberMeasurements(label) {
  measurementUndo.push({ items: new Map(savedMeasurements), label });
  if (measurementUndo.length > 10) measurementUndo.shift();
  measurementRedo.length = 0;
  measurementCloseHistory = null;
}

function hasMeasurementCloseStep(redo) {
  return measurementCloseHistory === (redo ? "reopened" : "closed");
}

function stepEditHistory(redo = false) {
  if (drawingLocked()) return;
  if (hasMeasurementCloseStep(redo)) {
    setMeasureMode(!redo, redo ? "closed" : "reopened");
    svg.focus({ preventScroll: true });
    return;
  }
  if (!measureMode) return stepDrawingHistory(redo);
  const from = redo ? measurementRedo : measurementUndo;
  const to = redo ? measurementUndo : measurementRedo;
  if (!from.length) return;
  measurementCloseHistory = null;
  const { items, label } = from.pop();
  to.push({ items: savedMeasurements, label });
  savedMeasurements = items;
  clearMeasurement();
  renderMeasurementList();
  updateText($("measurement-announcement"), redo ? `Повторено: ${label}.` : `Отменено: ${label}.`);
  svg.focus({ preventScroll: true });
}

function findMeasurementPoint(point, paths, pathBoxes, tolerance) {
  const [x, y] = point;
  if (!Number.isFinite(tolerance) || tolerance < 0) return { point: [x, y], snapped: false };
  let bestPoint = null;
  let bestDistance = tolerance * tolerance;
  let bestIsVertex = false;
  let bestIsNotch = false;
  let bestEdge = null;

  for (let index = 0; index < paths.length; index++) {
    const { points, closed, notch } = paths[index];
    if (points.length < 2) continue;
    const box = pathBoxes?.[index];
    if (box) {
      const dx = Math.max(box.minX - x, x - box.maxX, 0);
      const dy = Math.max(box.minY - y, y - box.maxY, 0);
      if (dx * dx + dy * dy > bestDistance) continue;
    }

    for (let i = closed ? 0 : 1; i < points.length; i++) {
      const [ax, ay] = points.at(i - 1);
      const [bx, by] = points[i];
      const dx = bx - ax, dy = by - ay;
      const span = dx * dx + dy * dy;
      const along = span ? ((x - ax) * dx + (y - ay) * dy) / span : 0;
      const at = Math.max(0, Math.min(1, along));
      const sx = at === 1 ? bx : ax + dx * at;
      const sy = at === 1 ? by : ay + dy * at;
      const distance = (x - sx) ** 2 + (y - sy) ** 2;
      const isVertex = at === 0 || at === 1;
      if (distance < bestDistance || (distance === bestDistance && (!bestPoint || notch && !bestIsNotch
          || Boolean(notch) === bestIsNotch && isVertex && !bestIsVertex))) {
        bestPoint = [sx, sy];
        bestDistance = distance;
        bestIsVertex = isVertex;
        bestIsNotch = Boolean(notch);
        bestEdge = [points.at(i - 1), points[i]];
      }
    }
  }
  return bestPoint ? { point: bestPoint, snapped: true, notch: bestIsNotch, edge: bestEdge } : { point, snapped: false };
}

function measurementPoint(event, draft = null) {
  const raw = drawingPoint(event);
  const units = camera.width / svg.clientWidth;
  const match = event.altKey ? { point: raw, snapped: false }
    : findMeasurementPoint(raw, placed, pathBoxes, MEASUREMENT_SNAP_PX * units);
  if (!draft) return match;
  if (!event.shiftKey) {
    draft.lockedCoordinate = null;
    return match;
  }
  const anchor = draft.start.point;
  const dx = Math.abs(raw[0] - anchor[0]), dy = Math.abs(raw[1] - anchor[1]);
  if (draft.lockedCoordinate === null && Math.hypot(dx, dy) >= MEASUREMENT_AXIS_THRESHOLD_PX * units) {
    draft.lockedCoordinate = dx >= dy ? 1 : 0;
  }
  const axis = draft.lockedCoordinate ?? (dx >= dy ? 1 : 0);
  const point = [...match.point];
  point[axis] = anchor[axis];
  return match.snapped && Math.abs(match.point[axis] - point[axis]) > 1e-6
    ? { ...match, point, target: match.point } : { ...match, point };
}

function startMeasurement(event) {
  if (measurePointer || savedMeasurements.size >= MEASUREMENT_COLORS.length) return;
  if (measurementFrame !== null) {
    cancelAnimationFrame(measurementFrame);
    measurementFrame = null;
  }
  measurementHover = null;
  measurementCursor = event;
  const second = Boolean(measurement);
  if (!second) {
    const color = MEASUREMENT_COLORS.findIndex((_, index) => !savedMeasurements.has(index));
    measurement = { start: measurementPoint(event), end: null, color, lockedCoordinate: null };
  } else measurement.end = measurementPoint(event, measurement);
  measurePointer = { id: event.pointerId, x: event.clientX, y: event.clientY, second, moved: false };
  svg.setPointerCapture(event.pointerId);
  renderMeasurement();
}

function moveMeasurementPointer(event) {
  if (savedMeasurements.size >= MEASUREMENT_COLORS.length) return;
  if (measurePointer) {
    if (measurePointer.id !== event.pointerId) return;
    measurePointer.moved ||= Math.hypot(event.clientX - measurePointer.x, event.clientY - measurePointer.y) >= 4;
    if (!measurePointer.moved && !measurePointer.second) return;
  }
  measurementCursor = event;
  if (measurementFrame !== null) return;
  measurementFrame = requestAnimationFrame(() => {
    measurementFrame = null;
    updateMeasurementPreview();
  });
}

function updateMeasurementPreview() {
  if (!measureMode || drag || !measurementCursor || savedMeasurements.size >= MEASUREMENT_COLORS.length) return;
  measurementHover = measurementPoint(measurementCursor, measurement);
  if (measurement) measurement.end = measurementHover;
  renderMeasurement();
}

function updateMeasurementModifiers(event) {
  if (!measureMode || !["Shift", "Alt"].includes(event.key)) return;
  if (!event.shiftKey && measurement) measurement.lockedCoordinate = null;
  if (!measurementCursor || (measurePointer && !measurePointer.second && !measurePointer.moved)) return;
  const { clientX, clientY } = measurementCursor;
  measurementCursor = { clientX, clientY, shiftKey: event.shiftKey, altKey: event.altKey };
  updateMeasurementPreview();
}

function finishMeasurement(event) {
  const pointer = measurePointer;
  measurePointer = null;
  if (svg.hasPointerCapture(pointer.id)) svg.releasePointerCapture(pointer.id);
  if (measurementFrame !== null) {
    cancelAnimationFrame(measurementFrame);
    measurementFrame = null;
  }
  if (!measurement) return;
  const completed = pointer.second || pointer.moved;
  if (completed) {
    measurement.end = measurementPoint(event, measurement);
    rememberMeasurements("добавление размера");
    savedMeasurements.set(measurement.color, measurement);
    const description = measurementLabel(measurement);
    clearMeasurement();
    renderMeasurementList();
    updateText($("measurement-announcement"), `Добавлен размер ${description}.`);
    return;
  }
  measurementHover = null;
  measurementCursor = null;
  renderMeasurement();
}

function measurementLabel({ start, end }) {
  const dx = end.point[0] - start.point[0], dy = end.point[1] - start.point[1];
  return `${formatFeedDistance.format(Math.hypot(dx, dy))} мм`;
}

function renderMeasurementList() {
  const list = $("measurement-list");
  updateText($("measurement-count"), `${savedMeasurements.size} / ${MEASUREMENT_COLORS.length}`);
  for (const row of [...list.children]) {
    if (!savedMeasurements.has(Number(row.dataset.measurementId))) row.remove();
  }
  for (const [index, [id, item]] of [...savedMeasurements].entries()) {
    const current = list.querySelector(`[data-measurement-id="${id}"]`);
    if (current) {
      if (current !== list.children[index]) list.insertBefore(current, list.children[index] ?? null);
      continue;
    }
    const row = document.createElement("li");
    row.dataset.measurementId = id;
    row.style.setProperty("--measurement-color", MEASUREMENT_COLORS[id]);
    const label = document.createElement("span");
    label.className = "measurement-list-value";
    label.textContent = measurementLabel(item);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-button measurement-remove";
    remove.dataset.removeMeasurement = id;
    remove.setAttribute("aria-label", `Удалить размер ${id + 1}: ${label.textContent}`);
    remove.title = "Удалить размер";
    const icon = element("svg", { class: "icon", "aria-hidden": "true" });
    icon.append(element("use", { href: "#i-trash" }));
    remove.append(icon);
    row.append(label, remove);
    list.insertBefore(row, list.children[index] ?? null);
  }
  updateControl(list, "hidden", savedMeasurements.size === 0);
  updateControl($("measurement-clear-all"), "hidden", savedMeasurements.size === 0);
  renderMeasurement(true);
  renderHistoryControls();
}

function removeMeasurement(button) {
  const id = Number(button.dataset.removeMeasurement);
  const item = savedMeasurements.get(id);
  if (!item) return;
  const row = button.closest("li");
  const next = (row.nextElementSibling ?? row.previousElementSibling)?.querySelector("button");
  rememberMeasurements("удаление размера");
  savedMeasurements.delete(id);
  renderMeasurementList();
  updateText($("measurement-announcement"), `Удалён размер ${measurementLabel(item)}.`);
  (next ?? svg).focus({ preventScroll: true });
}

function removeAllMeasurements() {
  if (!measureMode || !savedMeasurements.size) return;
  rememberMeasurements("удаление всех размеров");
  savedMeasurements.clear();
  clearMeasurement();
  renderMeasurementList();
  updateText($("measurement-announcement"), "Удалены все размеры. Чтобы вернуть их, нажмите Ctrl+Z.");
  svg.focus({ preventScroll: true });
}

function measurementPin(x, y, { snapped, notch }, units) {
  return element("circle", { cx: x, cy: y, r: (notch ? 5.5 : 4.5) * units,
    class: `measurement-pin${snapped ? " is-snapped" : ""}${notch ? " is-notch" : ""}`, "vector-effect": "non-scaling-stroke" });
}

function measurementMarks(item, units, committed = false) {
  const { start, end, color } = item;
  const attributes = { style: `--measurement-color:${MEASUREMENT_COLORS[color]}` };
  if (committed) attributes["data-measurement-id"] = color;
  const group = element("g", attributes);
  const [x1, y1] = projectPoint(...start.point);
  if (end) {
    const [x2, y2] = projectPoint(...end.point);
    const dx = Math.abs(x2 - x1), dy = Math.abs(y2 - y1);
    if (dx && dy) group.append(element("path", { d: `M${x1} ${y1}H${x2}V${y2}`,
      class: `measurement-components${start.notch || end.notch ? " is-notch" : ""}`, "vector-effect": "non-scaling-stroke" }));
    if (end.target) {
      const [tx, ty] = projectPoint(...end.target);
      group.append(element("line", { x1: x2, y1: y2, x2: tx, y2: ty,
        class: `measurement-projection${end.notch ? " is-notch" : ""}`, "vector-effect": "non-scaling-stroke" }),
      measurementPin(tx, ty, end, units));
    }
    for (const className of ["measurement-halo", "measurement-line"]) {
      group.append(element("line", { x1, y1, x2, y2, class: className, "vector-effect": "non-scaling-stroke" }));
    }
    if (Math.hypot(dx, dy) > 56 * units) {
      group.append(rulerText(measurementLabel(item), (x1 + x2) / 2 + (dx < dy ? 18 * units : 0), (y1 + y2) / 2 - 12 * units,
        17 * units, dx < dy ? "start" : "middle", "measurement-caption"));
    }
    group.append(measurementPin(x2, y2, end.target ? { snapped: false } : end, units));
  }
  group.append(measurementPin(x1, y1, start, units));
  return group;
}

function renderMeasurement(redrawSaved = false) {
  const layer = $("measurement-preview");
  updateControl($("measurement-panel"), "hidden", !measureMode);
  $("measurement-layer").toggleAttribute("hidden", !measureMode);
  const units = camera ? camera.width / Math.max(svg.clientWidth, 1) : 1;
  if (redrawSaved) {
    $("measurement-committed").replaceChildren(...[...savedMeasurements.values()].map((item) => measurementMarks(item, units, true)));
  }
  if (!measureMode || !camera) {
    if (layer.firstChild) layer.replaceChildren();
    updateText($("measurement-announcement"), "");
    return;
  }
  const markers = [];
  const { start, end } = measurement ?? {};
  if (measurementHover?.snapped) {
    const [x, y] = projectPoint(...(measurementHover.target ?? measurementHover.point));
    const reach = 34 * units;
    const [[ax, ay], [bx, by]] = measurementHover.edge.map((point) => projectPoint(...point));
    const span = Math.hypot(bx - ax, by - ay);
    const tone = measurementHover.notch ? " is-notch" : "";
    if (span) {
      const along = Math.hypot(x - ax, y - ay);
      const from = Math.max(0, along - 20 * units) / span;
      const to = Math.min(span, along + 20 * units) / span;
      markers.push(element("line", { x1: ax + (bx - ax) * from, y1: ay + (by - ay) * from,
        x2: ax + (bx - ax) * to, y2: ay + (by - ay) * to, class: `measurement-snap-edge${tone}`,
        "vector-effect": "non-scaling-stroke" }));
    }
    markers.push(element("path", { d: `M${x - reach} ${y}H${x + reach} M${x} ${y - reach}V${y + reach}`,
      class: `measurement-snap-guide${tone}`, "vector-effect": "non-scaling-stroke" }));
    if (!measurement) markers.push(measurementPin(x, y, measurementHover, units));
  }
  const hint = $("measurement-hint"), distance = $("measurement-distance");
  updateControl(hint, "hidden", Boolean(end));
  updateControl(distance, "hidden", !end);
  updateText(hint, start ? "Укажите вторую точку" : savedMeasurements.size >= MEASUREMENT_COLORS.length
    ? "Удалите один размер, чтобы добавить новый" : "Укажите две точки или протяните линию");
  if (end) {
    updateText(distance, measurementLabel(measurement));
    distance.style.setProperty("--measurement-color", MEASUREMENT_COLORS[measurement.color]);
  }
  if (start) markers.push(measurementMarks(measurement, units));
  layer.replaceChildren(...markers);
}

function bounds(paths) {
  const result = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (const path of paths) {
    for (const [x, y] of path.points) {
      result.minX = Math.min(x, result.minX);
      result.minY = Math.min(y, result.minY);
      result.maxX = Math.max(x, result.maxX);
      result.maxY = Math.max(y, result.maxY);
    }
  }
  return Number.isFinite(result.minX) ? result : null;
}

function materialLimits() {
  const margin = nestingSettings?.marginMm ?? 0;
  return {
    minX: margin, minY: margin,
    maxX: nestingSettings?.lengthMm == null ? Infinity : nestingSettings.lengthMm - margin,
    maxY: Math.min((nestingSettings?.widthMm ?? machine.height) - margin, machine.height - 0.01),
  };
}

function outsideMaterial(box, limits = materialLimits()) {
  return Boolean(box && (box.minX < limits.minX - placementEpsilon || box.minY < limits.minY - placementEpsilon
    || box.maxX > limits.maxX + placementEpsilon || box.maxY > limits.maxY + placementEpsilon));
}

function fieldNumber(id) {
  const input = $(id);
  return input.value && input.validity.valid ? input.valueAsNumber : NaN;
}

function mapPath(path, transform) {
  return { ...path, points: path.points.map(transform) };
}

function translatePath(path, dx, dy) {
  return mapPath(path, ([x, y]) => [x + dx, y + dy]);
}

function drawingLocked({ editing = true } = {}) {
  return (editing && (layoutReadOnly() || restoringWorkspace)) || isLoading || nestRequest !== null
    || contourRepair !== null || appUpdate?.state === "restarting"
    || ["upload", "cut", "advance-roll", "new-cut", "open-layout", "save-kit-layout"].includes(pendingCommand)
    || (!layoutPreparation() && runInProgress());
}

function placementPreviewActive() {
  return Boolean(drag?.moved && ["move", "rotate"].includes(drag.type));
}

function cutPlacementPreviewActive() {
  return placementPreviewActive() && Boolean(drag.placement?.moving.length);
}

function runInProgress() {
  return Boolean(live.externalMotion || activeRun?.startedAt && activeRunStates.has(activeRun.state));
}

function selectAllContours() {
  if (placed.length && !drawingLocked()) setSelection(new Set(placed.keys()));
}

function rememberDrawing(label) {
  measurementCloseHistory = null;
  drawingUndo.push(drawingHistoryState(label));
  if (drawingUndo.length > 10) drawingUndo.shift();
  drawingRedo.length = 0;
}

function drawingHistoryState(label) {
  return { placed, excluded, selection, nestingSettings, routeApplied, label };
}

function editSelection(transform, label, { keepRotation = false } = {}) {
  if (!selectedBounds || drawingLocked() || !drawingPartsReady) return;
  rememberDrawing(label);
  placed = placed.map((path, index) => selection.has(index) ? transform(path, index) : path);
  updateDrawing({ keepRotation, changedIndices: selection });
}

function moveSelection(dx, dy) {
  if (dx || dy) editSelection((path) => translatePath(path, dx, dy), "перемещение");
}

function rotationTransform(radians, [x, y]) {
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  return ([u, v]) => [x + (u - x) * cosine - (v - y) * sine, y + (u - x) * sine + (v - y) * cosine];
}

function normalizeAngle(angle) {
  const wrapped = angle % 360;
  if (Math.abs(wrapped) < 1e-9 || Math.abs(wrapped) > 360 - 1e-9) return 0;
  return wrapped < 0 ? wrapped + 360 : wrapped;
}

function selectedAngle() {
  if (!selection.size) return null;
  const [first] = selection;
  const angle = placed[first].partAngle ?? 0;
  for (const index of selection) {
    if (Math.abs((placed[index].partAngle ?? 0) - angle) > 1e-7) return null;
  }
  return angle;
}

function rotateSelectionBy(delta, pivot = null) {
  if (!selectedBounds || drawingLocked() || !drawingPartsReady) return;
  delta = normalizeAngle(delta);
  if (!delta) return;
  const { minX, minY } = selectedBounds;
  const rotate = rotationTransform(delta * Math.PI / 180, pivot ?? [minX, minY]);
  if (pivot) {
    editSelection((path) => ({ ...mapPath(path, rotate), partAngle: normalizeAngle((path.partAngle ?? 0) + delta) }),
      "поворот", { keepRotation: true });
    return;
  }
  const rotated = bounds([...selection].map((index) => mapPath(placed[index], rotate)));
  editSelection((path) => ({ ...translatePath(mapPath(path, rotate), minX - rotated.minX, minY - rotated.minY),
    partAngle: normalizeAngle((path.partAngle ?? 0) + delta) }), "поворот");
}

function applyPlacement(id) {
  if (!selectedBounds || drawingLocked()) return;
  const value = fieldNumber(id);
  if (!Number.isFinite(value) || Math.abs(value) > 1e9) {
    notifyError("Введите допустимое число.");
    syncPlacementFields();
    return;
  }
  const { minX, minY, maxX, maxY } = selectedBounds;
  if (id === "offset-x") moveSelection(value - minX, 0);
  if (id === "offset-y") moveSelection(0, value - minY);
  if (id === "rotation") rotateSelectionBy(value - (selectedAngle() ?? 0));
  if (id === "size-width" || id === "size-height") {
    if (kitLayout()) return;
    const current = id === "size-width" ? maxY - minY : maxX - minX;
    if (value < 0.001 || current < 0.001) {
      notifyError("Введите размер от 0,001 мм.");
      syncPlacementFields();
      return;
    }
    const factor = value / current;
    if (factor !== 1) editSelection((path) => mapPath(path, ([x, y]) => [minX + (x - minX) * factor, minY + (y - minY) * factor]), "масштабирование");
  }
  syncPlacementFields();
}

function deleteSelection() {
  if (kitLayout() || !selection.size || drawingLocked() || !drawingPartsReady) return;
  cancelCanvasGesture();
  rememberDrawing("удаление");
  const remaining = [];
  const remainingExcluded = new Set();
  for (const [index, path] of placed.entries()) {
    if (selection.has(index)) continue;
    if (excluded.has(index)) remainingExcluded.add(remaining.length);
    remaining.push(path);
  }
  placed = remaining;
  excluded = remainingExcluded;
  selection = new Set();
  updateDrawing();
}

function toggleSelectionExcluded() {
  if (kitLayout() || !selection.size || drawingLocked() || !drawingPartsReady) return;
  const restore = [...selection].every((index) => excluded.has(index));
  rememberDrawing(restore ? "возврат контуров" : "исключение");
  excluded = new Set(excluded);
  for (const index of selection) {
    if (restore) excluded.delete(index);
    else excluded.add(index);
  }
  updateDrawing();
}

function pathData(path) {
  const xy = path.points.map(([x, y]) => {
    const [u, v] = projectPoint(x, y);
    return `${u.toFixed(4)} ${v.toFixed(4)}`;
  }).join(" L ");
  return `M ${xy}${path.closed ? " Z" : ""}`;
}

function renderPaths() {
  const layer = $("path-layer");
  const notches = $("notch-layer");
  const limits = materialLimits();
  const warnings = expandToWholeParts(warningIndices);
  for (const [index, path] of placed.entries()) {
    let group = layer.children[index];
    if (!group) {
      group = element("g", { "data-index": index });
      group.append(
        element("path", { "vector-effect": "non-scaling-stroke" }),
        element("path", { class: "cut-hit", "vector-effect": "non-scaling-stroke" }),
      );
      layer.append(group);
    }
    const classes = ["cut-path"];
    if (path.closed) classes.push("closed");
    if (path.notch) classes.push("notch");
    if (selection.has(index)) classes.push("selected");
    if (excluded.has(index)) classes.push("excluded");
    const partBox = drawingParts.get(path.partNumber)?.activeBox ?? pathBoxes[index];
    if (!excluded.has(index) && (outsideMaterial(partBox, limits) || warnings.has(index)
        || placementConflicts.has(partProgressKey(index)))) classes.push("outside");
    if (completedDrawingPaths.has(index)) classes.push("is-cut-complete");
    if (completedDrawingParts.has(partProgressKey(index))) classes.push("is-part-complete");
    const section = sectionByPath.get(index);
    if (section === null) classes.push("crosses-sections");
    if (section && group.dataset.cutSection !== String(section)) colorCutSection(group, section);
    if (!section && group.hasAttribute("data-cut-section")) {
      group.removeAttribute("data-cut-section");
      group.style.removeProperty("--section-color");
    }
    const [shape, hit] = group.children;
    updateAttribute(shape, "class", classes.join(" "));
    if (renderedPaths[index] !== path) {
      const route = pathData(path);
      updateAttribute(shape, "d", route);
      updateAttribute(hit, "d", route);
    }
    const marker = notchMarkers.get(index);
    if (path.notch) {
      const visible = marker ?? element("path", { class: "notch-marker", "vector-effect": "non-scaling-stroke" });
      updateAttribute(visible, "d", shape.getAttribute("d"));
      visible.classList.toggle("excluded", excluded.has(index));
      if (!marker) {
        notchMarkers.set(index, visible);
        notches.append(visible);
      }
    } else if (marker) {
      marker.remove();
      notchMarkers.delete(index);
    }
  }
  while (layer.children.length > placed.length) layer.lastElementChild.remove();
  for (const [index, marker] of notchMarkers) if (index >= placed.length) {
    marker.remove();
    notchMarkers.delete(index);
  }
  renderedPaths = placed.slice();
  renderPartHighlight();
}

function syncPlacementFields() {
  const box = selectedBounds;
  const values = box ? [box.minX, box.minY, selectedAngle(), box.maxY - box.minY, box.maxX - box.minX] : [];
  for (const [index, id] of placementFields.entries()) {
    $(id).value = values[index] == null ? "" : String(Number(values[index].toFixed(3)));
  }
}

function svgBox(box) {
  const [x, y] = projectPoint(box.maxX, box.maxY);
  return { x, y, width: box.maxY - box.minY, height: box.maxX - box.minX };
}

function indexDrawingParts() {
  drawingParts = new Map();
  drawingPartsReady = true;
  for (const [index, path] of placed.entries()) {
    const id = path.partNumber;
    if (!id) {
      drawingPartsReady = false;
      continue;
    }
    const box = pathBoxes[index];
    if (!drawingParts.has(id)) {
      drawingParts.set(id, { number: id, indices: [], box: { ...box }, activeBox: null, rotation: "" });
    }
    const part = drawingParts.get(id);
    part.indices.push(index);
    part.rotation = path.partRotation === "quarter" ? "any" : path.partRotation ?? part.rotation;
    part.name ||= path.partName;
    part.product ||= path.productName;
    part.box.minX = Math.min(part.box.minX, box.minX);
    part.box.minY = Math.min(part.box.minY, box.minY);
    part.box.maxX = Math.max(part.box.maxX, box.maxX);
    part.box.maxY = Math.max(part.box.maxY, box.maxY);
    if (!excluded.has(index)) {
      part.activeBox = part.activeBox ? unionPlacementBoxes(part.activeBox, box) : box;
    }
  }
  renderPartLegend();
}

function partMembers(index) {
  return drawingParts.get(placed[index]?.partNumber)?.indices ?? [index];
}

function partProgressKey(index) {
  return placed[index]?.partNumber ?? -(index + 1);
}

function makeDrawingProgressModel(plan, includedIndices) {
  const totals = new Map();
  const pathsByPage = new Map();
  const pathTotals = new Map();
  const pathsByPart = new Map();
  for (const page of plan.pages) {
    const sections = [];
    for (const [ordinal, path] of page.paths.entries()) {
      const index = includedIndices[path.index];
      if (index === undefined) continue;
      const key = partProgressKey(index);
      totals.set(key, (totals.get(key) || 0) + 1);
      pathTotals.set(index, (pathTotals.get(index) || 0) + 1);
      if (!pathsByPart.has(key)) pathsByPart.set(key, new Set());
      pathsByPart.get(key).add(index);
      sections.push({ index, key, path, end: page.pathEndIndices?.[ordinal], trace: "" });
    }
    pathsByPage.set(page.number, sections);
  }
  return { plan, totals, pathsByPage, pathTotals, pathsByPart, signature: "", count: 0, total: totals.size };
}

function renderCutProgress() {
  const model = drawingProgressModel;
  const tracking = model && cutProgress?.planId === model.plan.planId ? cutProgress : null;
  const firstPage = tracking?.firstPage ?? 1;
  const lastDone = firstPage + (tracking?.completedPages ?? 0) - 1;
  const currentSections = model?.pathsByPage.get(tracking?.page) || [];
  let prefix = 0;
  let upper = currentSections.length;
  while (prefix < upper) {
    const mid = (prefix + upper) >>> 1;
    const end = currentSections[mid].end;
    if (Number.isInteger(end) && tracking.index >= end) prefix = mid + 1;
    else upper = mid;
  }
  const current = currentSections[prefix];
  const verifiedVertices = Number.isInteger(current?.end)
    ? Math.max(0, current.path.points.length + Number(current.path.closed) - (current.end - tracking.index))
    : 0;
  const signature = tracking ? `${tracking.page}/${firstPage}/${lastDone}/${prefix}/${verifiedVertices}` : "none";
  const layer = $("live-trace-layer");
  if (model ? model.signature !== signature
    : completedDrawingPaths.size || completedDrawingParts.size || layer.firstElementChild) {
    const nextPaths = new Set();
    const nextParts = new Set();
    const verifiedTrace = [];
    if (model && tracking) {
      const done = new Map();
      const cutPaths = new Map();
      const sessionParts = new Set();
      for (const [pageNumber, sections] of model.pathsByPage) {
        if (pageNumber < firstPage) continue;
        for (const [ordinal, section] of sections.entries()) {
          const { index, key, path } = section;
          sessionParts.add(key);
          if (pageNumber <= lastDone || pageNumber === tracking.page && ordinal < prefix) {
            done.set(key, (done.get(key) || 0) + 1);
            cutPaths.set(index, (cutPaths.get(index) || 0) + 1);
            section.trace ||= pathData(path);
            verifiedTrace.push(section.trace);
          }
        }
      }
      // The controller's proof excludes the next target. Draw only the visited prefix of the open trace.
      if (verifiedVertices > 1) {
        verifiedTrace.push(pathData({ points: current.path.points.slice(0, verifiedVertices), closed: false }));
      }
      for (const [index, count] of cutPaths) if (count === model.pathTotals.get(index)) nextPaths.add(index);
      for (const [key, count] of done) if (count === model.totals.get(key)) nextParts.add(key);
      model.count = nextParts.size;
      model.total = sessionParts.size;
      model.signature = signature;
    } else if (model) {
      model.count = 0;
      model.signature = signature;
    }
    const trace = layer.firstElementChild;
    if (verifiedTrace.length) {
      const route = verifiedTrace.join(" ");
      if (trace) updateAttribute(trace, "d", route);
      else layer.append(element("path", { d: route, class: "cut-trace", "vector-effect": "non-scaling-stroke" }));
    } else if (trace) trace.remove();
    const groups = $("path-layer").children;
    const changed = new Set();
    for (const paths of [completedDrawingPaths, nextPaths]) {
      for (const index of paths) if (completedDrawingPaths.has(index) !== nextPaths.has(index)) changed.add(index);
    }
    if (model) for (const parts of [completedDrawingParts, nextParts]) {
      for (const key of parts) if (completedDrawingParts.has(key) !== nextParts.has(key)) {
        for (const index of model.pathsByPart.get(key) || []) changed.add(index);
      }
    }
    const labelsChanged = completedDrawingParts.size !== nextParts.size || [...nextParts].some((key) => !completedDrawingParts.has(key));
    completedDrawingPaths = nextPaths;
    completedDrawingParts = nextParts;
    for (const index of changed) {
      const shape = groups[index]?.firstElementChild;
      shape?.classList.toggle("is-cut-complete", nextPaths.has(index));
      shape?.classList.toggle("is-part-complete", nextParts.has(partProgressKey(index)));
    }
    if (labelsChanged) renderPartLabels();
  }
  $("live-trace-layer").toggleAttribute("hidden", simulation.active);
  if (!simulation.active) $("canvas-cut-status").hidden = true;
  return tracking ? { count: model.count, total: model.total } : null;
}

function renderCanvasStatus(mode, page, total, detail, complete = false) {
  const chip = $("canvas-cut-status");
  chip.hidden = false;
  chip.dataset.mode = mode;
  chip.classList.toggle("is-complete", complete);
  for (const [id, value] of [
    ["canvas-cut-mode", "Симуляция"],
    ["canvas-cut-page", `Проход ${page} из ${total}`],
    ["canvas-cut-count", detail],
  ]) {
    const field = $(id);
    if (field.textContent !== value) field.textContent = value;
  }
}

function expandToWholeParts(indices) {
  const result = new Set();
  const seen = new Set();
  for (const index of indices) {
    const path = placed[index];
    if (!path || path.partNumber && seen.has(path.partNumber)) continue;
    if (path.partNumber) seen.add(path.partNumber);
    for (const member of partMembers(index)) result.add(member);
  }
  return result;
}

function identifyDrawingParts() {
  if (drawingPartsReady && placed.every((path) => typeof path.notch === "boolean")) return Promise.resolve();
  if (partDiscovery?.paths === placed) return partDiscovery.promise;
  const paths = placed;
  const request = { paths };
  partDiscovery = request;
  request.promise = (async () => {
    try {
      const result = await getJson("/api/nest-parts", jsonRequest({ paths }, AbortSignal.timeout(30_000)));
      if (paths !== placed) return;
      const numbered = new Array(paths.length).fill(null);
      for (const { number: id, indices, rotation, notchIndices } of result.parts) {
        const notches = new Set(notchIndices);
        for (const index of indices) {
          numbered[index] = { ...paths[index], partNumber: id,
            ...(rotation ? { partRotation: rotation } : {}),
            ...(Array.isArray(notchIndices) ? { notch: notches.has(index) } : {}) };
        }
      }
      if (numbered.some((path) => !path)) throw new Error("Сервер вернул неполный список деталей.");
      placed = numbered;
      indexDrawingParts();
      selection = expandToWholeParts(selection);
      refreshPlacementWarnings();
      resetCutPlan({ keepView: true });
      updateSelection();
      scheduleWorkspaceSave();
    } finally {
      if (partDiscovery === request) {
        partDiscovery = null;
        renderControls();
      }
    }
  })();
  return request.promise;
}

function partRotationText(rotation) {
  if (rotation === "any") return "поворот разрешён";
  if (rotation === "none") return "поворот запрещён";
  return "поворот по общим настройкам";
}

function renderPartHighlight() {
  const outlines = new Map();
  const shapes = $("path-layer").children;
  for (const index of selection) {
    const shape = shapes[index]?.firstElementChild;
    if (!shape) continue;
    const classes = `part-highlight${shape.classList.contains("outside") ? " is-outside" : ""}${excluded.has(index) ? " is-excluded" : ""}`;
    if (!outlines.has(classes)) outlines.set(classes, []);
    outlines.get(classes).push(shape.getAttribute("d"));
  }
  $("part-highlight-layer").replaceChildren(...Array.from(outlines, ([classes, paths]) =>
    element("path", { d: paths.join(" "), class: classes, "vector-effect": "non-scaling-stroke" })));
}

function selectedPartNumbers() {
  return new Set(Array.from(selection, (index) => placed[index]?.partNumber));
}

function setPartLegendExpanded(expanded) {
  const label = expanded ? "Свернуть детали" : "Развернуть детали";
  updateAttribute(partLegendToggle, "aria-expanded", String(expanded));
  updateAttribute(partLegendToggle, "aria-label", label);
  updateControl(partLegendToggle, "title", label);
  updateText($("part-legend-toggle-label"), expanded ? label : "Детали");
  updateControl($("part-legend-heading"), "hidden", !expanded);
  updateControl(partLegendContent, "hidden", !expanded);
  partLegend.classList.toggle("is-collapsed", !expanded);
}

function renderPartLegend() {
  partLegend.hidden = !drawingParts.size;
  partLegendRows.clear();
  partLegendProducts.clear();
  const query = partLegendSearch.value.trim().toLocaleLowerCase().replace(/^№\s*/, "");
  const partsByProduct = new Map();
  for (const part of [...drawingParts.values()].sort((a, b) => a.number - b.number)) {
    const product = part.product || "";
    if (!partsByProduct.has(product)) partsByProduct.set(product, []);
    partsByProduct.get(product).push(part);
  }
  const lists = document.createDocumentFragment();
  for (const [product, parts] of partsByProduct) {
    const visible = parts.filter((part) => !query || `${part.number} ${part.name || ""} ${product}`.toLocaleLowerCase().includes(query));
    if (!visible.length) continue;
    const list = document.createElement("ul");
    const addButton = () => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      item.append(button);
      list.append(item);
      return button;
    };
    if (product) {
      const button = addButton();
      button.className = "part-legend-product";
      button.textContent = product;
      button.onclick = () => selectLegendParts(parts);
      partLegendProducts.set(button, parts);
    }
    for (const part of visible) {
      const button = addButton();
      const number = document.createElement("strong");
      number.className = "part-legend-number";
      number.textContent = `№${part.number}`;
      button.append(number);
      if (part.name) button.append(document.createTextNode(part.name));
      button.onclick = () => selectLegendParts([part]);
      partLegendRows.set(part.number, button);
    }
    lists.append(list);
  }
  if (query && !partLegendRows.size) {
    const empty = document.createElement("p");
    empty.textContent = "Детали не найдены";
    lists.append(empty);
  }
  updateText($("part-legend-count"), query ? `${partLegendRows.size} / ${drawingParts.size}` : String(drawingParts.size));
  partLegendList.replaceChildren(lists);
  updatePartLegendSelection();
}

function updatePartLegendSelection(reveal = false) {
  if (partLegend.hidden) return;
  const active = selectedPartNumbers();
  const revealRow = reveal && !partLegendContent.hidden && active.size === 1;
  if (revealRow && partLegendSearch.value && !partLegendRows.has(active.values().next().value)) {
    partLegendSearch.value = "";
    renderPartLegend();
  }
  for (const [number, row] of partLegendRows) updateAttribute(row, "aria-pressed", String(active.has(number)));
  for (const [row, parts] of partLegendProducts) {
    updateAttribute(row, "aria-pressed", String(parts.every((part) => active.has(part.number))));
  }
  if (!revealRow) return;
  const row = partLegendRows.get(active.values().next().value)?.getBoundingClientRect();
  if (!row) return;
  const list = partLegendList.getBoundingClientRect();
  if (row.bottom > list.bottom) partLegendList.scrollTop += row.bottom - list.bottom;
  else if (row.top < list.top) partLegendList.scrollTop += row.top - list.top;
}

function selectLegendParts(parts) {
  if (drag || drawingLocked() && !layoutReadOnly() && !runInProgress()) return;
  setSelection(new Set(parts.flatMap((part) => part.indices)));
  if (!camera || !selectedBounds || runInProgress() || simulation.active) return;
  const box = svgBox(selectedBounds);
  const insets = { left: overlayWidth("launch-controls"), right: overlayWidth("canvas-controls") };
  const units = camera.width / Math.max(1, svg.clientWidth);
  if (box.x >= camera.x + insets.left * units && box.x + box.width <= camera.x + camera.width - insets.right * units
      && box.y >= camera.y && box.y + box.height <= camera.y + camera.height) return;
  const focus = newCamera({ ...box, width: Math.max(1, box.width), height: Math.max(1, box.height) }, insets);
  const width = Math.max(camera.width, focus.width), height = Math.max(camera.height, focus.height);
  cameraMode = "manual";
  camera = { x: focus.x - (width - focus.width) / 2, y: focus.y - (height - focus.height) / 2, width, height };
  drawCamera();
}

partLegendSearch.addEventListener("input", renderPartLegend);
partLegendToggle.addEventListener("click", () => setPartLegendExpanded(partLegendContent.hidden));
partLegend.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && selection.size && event.target !== partLegendSearch) setSelection(new Set());
});

function renderPartLabels() {
  const layer = $("part-label-layer");
  if (drag?.labels) return;
  if (!camera || !svg.clientWidth || !drawingParts.size) {
    layer.replaceChildren();
    partLabels.clear();
    return;
  }
  const units = camera.width / svg.clientWidth;
  const selectedParts = selectedPartNumbers();
  const visible = [];
  for (const part of drawingParts.values()) {
    const { minX, maxX, minY, maxY } = part.box;
    const center = [(minX + maxX) / 2, (minY + maxY) / 2];
    const [x, y] = projectPoint(...center);
    const px = (x - camera.x) / units;
    const py = (y - camera.y) / units;
    if (px < 0 || px > svg.clientWidth || py < 0 || py > svg.clientHeight) continue;
    visible.push({ part, px, py, center, area: (maxX - minX) * (maxY - minY), active: selectedParts.has(part.number) });
  }
  visible.sort((a, b) => b.active - a.active || Number(partLabels.has(b.part.number)) - Number(partLabels.has(a.part.number))
    || Boolean(b.part.rotation) - Boolean(a.part.rotation) || b.area - a.area);
  const occupied = [], selectedOccupied = [];
  const shown = new Set();
  for (const { part, px, py, center, active } of visible) {
    const region = active ? selectedOccupied : occupied;
    if (region.length >= 200) continue;
    const label = `№${part.number}`;
    const complete = completedDrawingParts.has(part.number);
    const width = 12 + label.length * 8 + (part.rotation || complete ? 17 : 0);
    const left = px - width / 2;
    if (region.some((box) => Math.abs(box.y - py) < 25 && left < box.right + 3 && left + width > box.left - 3)) continue;
    region.push({ left, right: left + width, y: py });
    shown.add(part.number);
    const classes = ["part-label", part.rotation, active ? "is-active" : "",
      part.indices.every((index) => excluded.has(index)) ? "is-excluded" : "", complete ? "is-complete" : ""];
    let entry = partLabels.get(part.number);
    if (!entry) {
      const node = element("g", { "data-part-number": part.number });
      entry = { node };
      partLabels.set(part.number, entry);
      layer.append(node);
    }
    const { node } = entry;
    const marker = complete ? "complete" : part.rotation;
    if (entry.marker !== marker || entry.name !== part.name || !node.firstChild) {
      const title = element("title");
      title.textContent = `${label} · ${part.name || "Деталь"} · ${complete ? "Рез завершён" : partRotationText(part.rotation)}`;
      const text = element("text", { x: 6, y: 15.5 });
      text.textContent = label;
      node.replaceChildren(title, element("rect", { width, height: 22, rx: 5, class: "part-label-bg" }), text);
      if (complete) {
        node.append(element("path", { class: "part-check", d: `M${width - 16} 11l3 3 5 -6` }));
      } else if (part.rotation) {
        node.append(element("use", { class: "icon", href: part.rotation === "none" ? "#i-lock" : "#i-turn-cw",
          x: width - 17, y: 5, width: 12, height: 12 }));
      }
      entry.marker = marker;
      entry.name = part.name;
    }
    entry.center = center;
    entry.units = units;
    entry.halfWidth = width / 2;
    updateAttribute(node, "class", classes.join(" "));
    positionPartLabel(entry, entry.center);
  }
  for (const [number, entry] of partLabels) if (!shown.has(number)) {
    entry.node.remove();
    partLabels.delete(number);
  }
}

function positionPartLabel({ node, units, halfWidth }, point) {
  const [x, y] = projectPoint(...point);
  updateAttribute(node, "transform", `translate(${x - halfWidth * units} ${y - 11 * units}) scale(${units})`);
}

function updateSelection(revealLegend = false) {
  selectedBounds = bounds([...selection].map((index) => placed[index]));
  const allExcluded = selection.size > 0 && [...selection].every((index) => excluded.has(index));
  const exclude = $("exclude-button");
  exclude.textContent = allExcluded ? "Вернуть" : "Исключить";
  exclude.title = allExcluded ? "Вернуть выделенные контуры в задание" : "Исключить выделенные контуры из задания";
  syncPlacementFields();
  renderPartHighlight();
  renderPartLabels();
  updatePartLegendSelection(revealLegend);
}

function setSelection(indices) {
  indices = expandToWholeParts(indices);
  if (indices.size === selection.size && [...indices].every((index) => selection.has(index))) {
    updatePartLegendSelection(true);
    return;
  }
  endFreeRotation();
  const groups = $("path-layer").children;
  for (const index of selection) if (!indices.has(index)) groups[index]?.firstElementChild.classList.remove("selected");
  for (const index of indices) if (!selection.has(index)) groups[index].firstElementChild.classList.add("selected");
  selection = indices;
  updateSelection(true);
  renderSelectionControls(drawingLocked());
}


function contourBoxesTouch(a, b) {
  const clearance = 0.001;
  return a.minX <= b.maxX + clearance && b.minX <= a.maxX + clearance
    && a.minY <= b.maxY + clearance && b.minY <= a.maxY + clearance;
}

function sameContourPoint(a, b) {
  return a[0] === b[0] && a[1] === b[1];
}

function contourPointKey(point) {
  return point[0] + "," + point[1];
}

function joinComponents(records) {
  const visited = new Set();
  const components = [];
  for (const first of records) {
    if (!first.ends || visited.has(first)) continue;
    const members = [first];
    const nodes = new Set();
    let branching = false;
    let closed = true;
    visited.add(first);
    for (const record of members) {
      for (const node of record.ends) {
        if (nodes.has(node)) continue;
        nodes.add(node);
        if (node.links.length > 2) branching = true;
        if (node.links.length === 1) closed = false;
        for (const { record: neighbour } of node.links) {
          if (visited.has(neighbour)) continue;
          visited.add(neighbour);
          members.push(neighbour);
        }
      }
    }
    if (!branching && members.length > 1) components.push({ first, members, nodes, closed });
  }
  return components;
}

function sortedPointBoundary(nodes, axis, value, inclusive) {
  let lo = 0, hi = nodes.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const coordinate = nodes[mid].point[axis];
    if (coordinate < value || (inclusive && coordinate === value)) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function blockJunctionsInsideSegments(records, nodes, budget) {
  const byKey = new Map(Array.from(nodes, (node) => [contourPointKey(node.point), node]));
  const sorted = [0, 1].map((axis) => Array.from(nodes).sort((a, b) => a.point[axis] - b.point[axis]));
  const epsilon = 1e-9;
  for (const { path, ends } of records) {
    const { points, closed } = path;
    for (let i = 0; i < points.length; i++) {
      // A vertex from another segment or a closed path makes this junction ambiguous.
      if (!ends || (i > 0 && i < points.length - 1)) {
        const node = byKey.get(contourPointKey(points[i]));
        if (node) node.blocked = true;
      }

      if (i === 0 && !closed) continue;
      const a = i === 0 ? points.at(-1) : points[i - 1];
      const b = points[i];
      if (sameContourPoint(a, b)) continue;
      const minX = Math.min(a[0], b[0]) - epsilon;
      const maxX = Math.max(a[0], b[0]) + epsilon;
      const minY = Math.min(a[1], b[1]) - epsilon;
      const maxY = Math.max(a[1], b[1]) + epsilon;
      const fromX = sortedPointBoundary(sorted[0], 0, minX, false);
      const toX = sortedPointBoundary(sorted[0], 0, maxX, true);
      const fromY = sortedPointBoundary(sorted[1], 1, minY, false);
      const toY = sortedPointBoundary(sorted[1], 1, maxY, true);
      const useX = toX - fromX <= toY - fromY;
      const candidates = sorted[useX ? 0 : 1];
      const from = useX ? fromX : fromY;
      const to = useX ? toX : toY;
      budget.remaining -= to - from;
      if (budget.remaining < 0) return false;

      const dx = b[0] - a[0], dy = b[1] - a[1];
      const tolerance = epsilon * Math.max(Math.abs(dx), Math.abs(dy));
      for (let j = from; j < to; j++) {
        const node = candidates[j];
        if (node.blocked || sameContourPoint(node.point, a) || sameContourPoint(node.point, b)) continue;
        const [x, y] = node.point;
        if (x < minX || x > maxX || y < minY || y > maxY) continue;
        if (Math.abs(dx * (y - a[1]) - dy * (x - a[0])) <= tolerance) node.blocked = true;
      }
    }
  }
  return true;
}

function combineContourChain({ first, closed, members }) {
  const connection = (record, side) => record.ends[side].links.find((link) => link.record !== record);
  let start = { record: first, side: 0 };
  if (!closed) {
    // Walk from the first source contour towards the beginning to keep its direction.
    for (let next = connection(start.record, start.side); next; next = connection(start.record, start.side)) {
      start = { record: next.record, side: 1 - next.side };
    }
  }

  const points = [];
  let current = start;
  for (let i = 0; i < members.length; i++) {
    const { record, side } = current;
    const source = record.path.points;
    for (let j = i === 0 ? 0 : 1; j < source.length; j++) {
      points.push(source[side === 0 ? j : source.length - 1 - j]);
    }
    current = connection(record, 1 - side);
  }
  if (closed) {
    points.pop(); // The closed flag draws the original last segment back to the first point.
    if (points.length < 3 || new Set(points.map(contourPointKey)).size !== points.length) return null;
  }
  return { ...first.path, points, closed };
}

function joinTouchingContours(paths) {
  const unchanged = { paths, joined: 0, skipped: false };
  if (paths.length < 2) return unchanged;

  const records = paths.map((path, index) => ({ path, index, ends: null, box: null }));
  const layers = new Map();
  for (const record of records) {
    const layer = record.path.layer || "0";
    if (!layers.has(layer)) layers.set(layer, { records: [], nodes: new Map() });
    const group = layers.get(layer);
    group.records.push(record);

    const { points, closed } = record.path;
    if (record.path.notch || closed || points.length < 2 || sameContourPoint(points[0], points.at(-1))) continue;
    record.ends = [points[0], points.at(-1)].map((point, side) => {
      const key = contourPointKey(point);
      if (!group.nodes.has(key)) group.nodes.set(key, { point, links: [], blocked: false });
      const node = group.nodes.get(key);
      node.links.push({ record, side });
      return node;
    });
  }

  const budget = { remaining: 1_000_000 };
  const replacements = new Map();
  const removed = new Set();
  for (const group of layers.values()) {
    const components = joinComponents(group.records);
    if (!components.length) continue;
    const nodes = new Set();
    for (const component of components) {
      for (const node of component.nodes) nodes.add(node);
    }
    if (!blockJunctionsInsideSegments(group.records, nodes, budget)) return { ...unchanged, skipped: true };

    for (const component of components) {
      if (Array.from(component.nodes).some((node) => node.blocked)) continue;
      if (component.members.some(({ path }) => path.partNumber !== component.first.path.partNumber)) continue;
      const merged = combineContourChain(component);
      if (!merged) continue;
      const rotation = drawingParts.get(merged.partNumber)?.rotation;
      if (rotation) merged.partRotation = rotation;

      // Merging interleaved source paths can otherwise move an exterior cut ahead of a hole.
      const first = component.first.index;
      const members = new Set(component.members.map((record) => record.index));
      let last = first;
      for (const index of members) last = Math.max(last, index);
      const box = bounds([merged]);
      let conflicted = false;
      for (let index = first + 1; index < last; index++) {
        if (--budget.remaining < 0) return { ...unchanged, skipped: true };
        if (members.has(index)) continue;
        const record = records[index];
        record.box ??= bounds([record.path]);
        if (contourBoxesTouch(box, record.box)) {
          conflicted = true;
          break;
        }
      }
      if (conflicted) continue;

      replacements.set(first, merged);
      for (const index of members) {
        if (index !== first) removed.add(index);
      }
    }
  }

  if (!removed.size) return unchanged;
  return {
    paths: paths.flatMap((path, index) => removed.has(index) ? [] : [replacements.get(index) ?? path]),
    joined: removed.size,
    skipped: false,
  };
}

function applyContourOptimization() {
  if (drawingLocked()) return;
  const join = previewContourJoin();
  const optimize = $("optimize-route").checked;
  if (!join?.joined && optimize === routeApplied) return;
  rememberDrawing(join?.joined ? "соединение контуров" : "настройку маршрута");
  routeApplied = optimize;
  if (join?.joined) {
    replaceJoinedContours(join.paths);
    optimizationJoined = true;
  }
  updateDrawing();
}

function replaceJoinedContours(paths) {
  const selectedPaths = new Set([...selection].map((index) => placed[index]));
  const selectedParts = new Set([...selectedPaths].map(({ partNumber }) => partNumber).filter(Boolean));
  const excludedPaths = new Set([...excluded].map((index) => placed[index]));
  const allSelected = selection.size === placed.length;
  let cursor = 0;
  placed = placed.flatMap((path) => {
    if (excludedPaths.has(path)) return [path];
    return cursor < paths.length ? [paths[cursor++]] : [];
  });
  selection = new Set();
  excluded = new Set();
  for (const [index, path] of placed.entries()) {
    if (allSelected || selectedPaths.has(path) || selectedParts.has(path.partNumber)) selection.add(index);
    if (excludedPaths.has(path)) excluded.add(index);
  }
}

function stepDrawingHistory(redo = false) {
  const from = redo ? drawingRedo : drawingUndo;
  const to = redo ? drawingUndo : drawingRedo;
  if (!from.length || drawingLocked()) return;
  measurementCloseHistory = null;
  cancelCanvasGesture();
  const state = from.pop();
  to.push(drawingHistoryState(state.label));
  const priorNesting = nestingSettings;
  ({ placed, excluded, selection, nestingSettings, routeApplied } = state);
  if (fixedDesignWidth() && nestingSettings?.widthMm !== managedCut.widthMm) {
    nestingSettings = { ...(nestingSettings ?? priorNesting), widthMm: managedCut.widthMm, widthExplicit: true };
  }
  updateDrawing();
  if (!contourDialog.open) svg.focus({ preventScroll: true });
}

function matchesContourRepair(snapshot) {
  return snapshot.source === source && snapshot.paths === placed && snapshot.excluded === excluded
    && snapshot.planEpoch === cutPlanEpoch;
}

function contourCheckAvailable() {
  return !kitLayout() && !drawingLocked() && !busy() && !live.stopAvailable
    && activeRun?.state !== "transferring" && !xiaoHandoff && placed.length > excluded.size;
}

function updateContourCheckControls() {
  const available = contourCheckAvailable();
  updateControl($("contour-check-button"), "disabled", !available);
  if (!contourCheckDialog.open) return;
  if (contourRepairProposal && !matchesContourRepair(contourRepairProposal)) {
    contourRepairProposal = null;
    setContourCheckStatus("Чертёж изменился. Запустите проверку ещё раз.");
  }
  updateControl($("contour-check-scan"), "disabled", !available);
  updateControl($("contour-check-apply"), "disabled", !available || !contourRepairProposal);
}

function setContourCheckStatus(message) {
  updateText($("contour-check-status"), message);
}

async function scanDrawingContours() {
  if (!contourCheckDialog.open || !contourCheckAvailable()) return;
  cancelCanvasGesture();
  const snapshot = { source, paths: placed, excluded, planEpoch: cutPlanEpoch };
  contourRepairProposal = null;
  const request = new AbortController();
  contourRepair = request;
  setContourCheckStatus("Проверяем стыки контуров…");
  renderControls();
  try {
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]);
    const result = await getJson("/api/close-contours",
      jsonRequest({ paths: snapshot.paths, excluded: [...snapshot.excluded] }, signal));
    if (contourRepair !== request || !contourCheckDialog.open) return;
    signal.throwIfAborted();
    if (!matchesContourRepair(snapshot)) {
      setContourCheckStatus("Чертёж изменился. Запустите проверку ещё раз.");
      return;
    }
    if (result.closedCount > 0) contourRepairProposal = { ...snapshot, result };
    const checked = `Проверено контуров: ${number(placed.length - excluded.size)}.`;
    const skipped = excluded.size ? ` Пропущено исключённых контуров: ${number(excluded.size)}.` : "";
    const findings = result.closedCount > 0 ? ` Контуров, которые можно замкнуть: ${number(result.closedCount)}.`
      : " Контуров, которые можно уверенно замкнуть, не найдено.";
    setContourCheckStatus(checked + skipped + findings);
  } catch (error) {
    if (contourRepair !== request || !contourCheckDialog.open) return;
    setContourCheckStatus(error.status === 404 ? "Перезапустите локальную программу плоттера для проверки контуров." : messageFrom(error));
  } finally {
    if (contourRepair === request) {
      contourRepair = null;
      renderControls();
    }
  }
}

function applyContourRepair() {
  const proposal = contourRepairProposal;
  if (!contourCheckDialog.open || !contourCheckAvailable() || !proposal) return;
  contourRepairProposal = null;
  if (!matchesContourRepair(proposal)) {
    setContourCheckStatus("Чертёж изменился. Запустите проверку ещё раз.");
    updateContourCheckControls();
    return;
  }
  const { result } = proposal;
  rememberDrawing("восстановление замкнутых контуров");
  selection = new Set(result.sources.flatMap((indices, index) => indices.some((old) => selection.has(old)) ? [index] : []));
  placed = result.paths;
  excluded = new Set(result.excluded);
  updateDrawing();
  setContourCheckStatus(`Замкнуто контуров: ${number(result.closedCount)}.`);
}

function setNestStatus(message, blocking = [], error = true, offerRotation = false) {
  nestBlocking = blocking;
  $("nest-feedback").hidden = !message;
  $("nest-feedback").classList.toggle("is-error", error);
  $("nest-status").textContent = message;
  $("nest-show-blocking").hidden = !blocking.length;
  $("nest-try-rotation").hidden = !offerRotation;
  $("nest-recheck").hidden = nestingBackend !== "outdated" && nestingBackend !== "unavailable";
}

function showNestingBackendStatus() {
  if (nestingBackend === "checking") setNestStatus("Проверяем версию программы…", [], false);
  if (nestingBackend === "ready") setNestStatus(hasNestingPaths() ? "" : emptyNestingMessage());
  if (nestingBackend === "outdated") {
    setNestStatus("Запущена старая версия автораскладки. Когда станок остановлен, закройте окно запуска плоттера, снова откройте «Запустить плоттер.cmd» и нажмите «Проверить версию снова».");
  }
  if (nestingBackend === "unavailable") setNestStatus("Программа не отвечает. Проверьте, открыто ли окно запуска плоттера.");
}

async function checkNestingBackend() {
  nestingBackend = "checking";
  showNestingBackendStatus();
  updateOptimizationControls();
  try {
    const status = await getJson("/api/status", { signal: AbortSignal.timeout(3500) });
    nestingBackend = status.machine?.optimizedNesting === true ? "ready" : "outdated";
  } catch {
    nestingBackend = "unavailable";
  }
  if (nestDialog.open) {
    showNestingBackendStatus();
    updateOptimizationControls();
  }
}

function defaultFabricWidthMm() {
  return Math.min(machine.height, machine.defaultFabricWidthMm ?? DEFAULT_FABRIC_WIDTH_MM);
}

function fixedDesignWidth() {
  return managedCut?.mode === "design" && Number.isFinite(managedCut.widthMm);
}

function cutWidthPending() {
  if (managedCut?.widthMm === undefined || kitLayout()) return false;
  return !Number.isFinite(nestingSettings?.widthMm) || (managedCut.widthMm === null && managedCut.mode !== "demo");
}

function applyNestingSettings() {
  const width = $("nest-width");
  const pending = cutWidthPending();
  width.readOnly = kitLayout() || managedCut?.mode === "design" && managedCut.widthMm !== null;
  width.required = pending && !width.readOnly;
  width.max = String(machine.height);
  width.placeholder = String(defaultFabricWidthMm());
  const savedWidth = nestingSettings?.widthMm;
  const oldDefault = nestingSettings?.widthExplicit == null && savedWidth === machine.height;
  width.value = kitLayout() || fixedDesignWidth() ? managedCut.widthMm
    : pending ? savedWidth ?? "" : !oldDefault && savedWidth != null ? savedWidth : defaultFabricWidthMm();
  $("nest-length").value = nestingSettings?.lengthMm ?? "";
  $("nest-gap").value = nestingSettings?.gapMm ?? 10;
  $("nest-margin").value = nestingSettings?.marginMm ?? 10;
  $("nest-rotation").value = ["any", "quarter"].includes(nestingSettings?.rotation) ? "any" : "none";
}

function nestingIndices() {
  const selected = nestSelection.size ? expandToWholeParts(nestSelection) : null;
  return placed.flatMap((_, index) => !excluded.has(index) && (!selected || selected.has(index)) ? [index] : []);
}

function hasNestingPaths() {
  return nestSelection.size
    ? [...nestSelection].some(index => placed[index] && !excluded.has(index))
    : placed.length > excluded.size;
}

function emptyNestingMessage() {
  return nestSelection.size ? "В выделении нет деталей, включённых в раскрой." : "Нет деталей, включённых в раскрой.";
}

async function applyNesting() {
  if (drawingLocked() || !nestDialog.open || nestingBackend !== "ready" || !$("nest-form").reportValidity()) return;
  const indices = [];
  const paths = [];
  const length = $("nest-length");
  const settings = {
    widthMm: $("nest-width").value ? $("nest-width").valueAsNumber : defaultFabricWidthMm(),
    lengthMm: length.value ? length.valueAsNumber : null,
    gapMm: $("nest-gap").valueAsNumber,
    marginMm: $("nest-margin").valueAsNumber,
    rotation: $("nest-rotation").value,
  };
  if (2 * settings.marginMm >= settings.widthMm) {
    setNestStatus("Оставьте положительную ширину между отступами от краёв.");
    return;
  }
  const abort = new AbortController();
  const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(90_000)]);
  nestRequest = abort;
  cancelCanvasGesture();
  setNestStatus("Рассчитываем расположение деталей…", [], false);
  updateOptimizationControls();
  renderControls();
  try {
    await identifyDrawingParts();
    if (signal.aborted) return;
    for (const index of nestingIndices()) {
      const path = placed[index];
      const rotation = drawingParts.get(path.partNumber)?.rotation;
      paths.push(rotation ? { ...path, partRotation: rotation } : path);
      indices.push(index);
    }
    if (!paths.length) {
      setNestStatus(emptyNestingMessage());
      return;
    }
    const layout = await getJson("/api/nest", jsonRequest({ paths, ...settings }, signal));
    if (abort.signal.aborted) return;
    if (runInProgress() || activeRun?.state === "transferring") {
      setNestStatus("Раскладка недоступна во время работы станка.");
      return;
    }
    if (!Array.isArray(layout.paths) || layout.paths.length !== paths.length || !Array.isArray(layout.placements)
        || !Number.isFinite(layout.partCount) || !Number.isFinite(layout.lengthMm)) {
      throw new Error("Сервер вернул неполную раскладку.");
    }
    if (settings.lengthMm !== null && (layout.lengthMm - settings.lengthMm > 1e-7 || layout.pageCount === 0)) {
      throw new Error("Полученная раскладка не подходит для заданной длины полотна и разбиения на проходы.");
    }
    const next = placed.slice();
    const nextExcluded = new Set(excluded);
    indices.forEach((index, i) => { next[index] = layout.paths[i]; });
    if (nestSelection.size) {
      const laidOut = new Set(indices);
      for (const index of placed.keys()) if (!laidOut.has(index)) nextExcluded.add(index);
    }
    const moves = new Map();
    for (const move of layout.placements) {
      for (const member of move.indices) moves.set(paths[member].partNumber, move);
    }
    for (const index of excluded) {
      const path = placed[index];
      const move = moves.get(path.partNumber);
      if (!move) continue;
      const [member] = move.indices;
      const [oldX, oldY] = paths[member].points[0];
      const [newX, newY] = layout.paths[member].points[0];
      const radians = move.angle * Math.PI / 180;
      const cosine = Math.cos(radians);
      const sine = Math.sin(radians);
      next[index] = mapPath(path, ([x, y]) => {
        const dx = x - oldX;
        const dy = y - oldY;
        return [newX + cosine * dx - sine * dy, newY + sine * dx + cosine * dy];
      });
      next[index].partNumber = move.partNumber;
      next[index].partAngle = normalizeAngle((path.partAngle ?? 0) + move.angle);
    }
    rememberDrawing("автораскладку");
    placed = next;
    excluded = nextExcluded;
    nestingSettings = { ...settings, widthExplicit: Boolean(managedCut) || settings.widthMm !== defaultFabricWidthMm() };
    nestRequest = null;
    nestDialog.close();
    updateDrawing();
    fitCamera();
  } catch (error) {
    if (abort.signal.aborted) return;
    const source = error.details?.indices;
    const blocking = Array.isArray(source)
      ? [...new Set(source.map((index) => indices[index]).filter(Number.isInteger))] : [];
    const minimum = error.details?.minimumWidthMm;
    let message = messageFrom(error);
    if (error.status === 404) {
      nestingBackend = "outdated";
      showNestingBackendStatus();
      return;
    }
    if (error.name === "TimeoutError") message = "Расчёт занял слишком много времени.";
    if (error.status === 422 && Number.isFinite(minimum)) {
      showNestingFitError(error.details, blocking, settings);
    } else {
      setNestStatus(message, blocking);
    }
  } finally {
    if (nestRequest === abort) nestRequest = null;
    updateOptimizationControls();
    renderControls();
  }
}

function showNestingFitError(details, blocking, settings) {
  const { widthMm, marginMm, rotation } = settings;
  const usable = details.usableWidthMm ?? widthMm - 2 * marginMm;
  const blockedCount = details.blockedPartCount ?? blocking.length;
  const count = blockedCount ? `Не помещается деталей: ${number(blockedCount)}. ` : "";
  const wholeMm = (value) => number(Math.ceil(value - 1e-7));
  let message = `${count}Между отступами доступно ${number(usable)} мм. При выбранном повороте нужна ширина полотна от ${wholeMm(details.minimumWidthMm)} мм.`;
  const offerRotation = rotation === "none" && details.quarterTurnFits === true;
  if (rotation === "none" && Number.isFinite(details.quarterTurnMinimumWidthMm)) {
    const needed = details.quarterTurnMinimumWidthMm;
    message += offerRotation
      ? " На этом полотне детали помещаются, если разрешить поворот."
      : ` При общем разрешении поворота потребуется полотно от ${wholeMm(needed)} мм.`;
  }
  setNestStatus(message, blocking, true, offerRotation);
}

function buildJob() {
  const paths = included();
  if (!paths.length) throw new Error("Добавьте хотя бы один контур к заданию.");
  return {
    paths, ...(machine.apiVersion >= 14 ? { optimize: routeApplied } : {}),
    materialWidthMm: Math.min(nestingSettings?.widthMm ?? machine.height, machine.height),
    materialMarginMm: nestingSettings?.marginMm ?? 0,
  };
}

function setSaveError(message = "") {
  const status = $("workspace-save");
  updateText(status, message);
  updateControl(status, "hidden", !message);
  status.classList.toggle("failed", Boolean(message));
}

function workspaceSnapshot(paths = placed, excludedIndices = excluded) {
  if (!source) return null;
  return {
    source: { filename: source.filename, warnings: source.warnings || [] },
    paths,
    excluded: [...excludedIndices],
    view: {
      zoom: camera && baseCamera ? camera.width / baseCamera.width : 1,
      focusX: camera ? camera.x + camera.width / 2 : 0,
      focusY: camera ? camera.y + camera.height / 2 : 0,
      grid: true,
      cameraMode,
      ...(previewCutPage ? { section: previewCutPage } : {}),
    },
    optimization: {
      route: routeApplied, routeApplied, join: $("optimize-join").checked,
      ...(nestingSettings ? { nesting: nestingSettings } : {}),
    },
  };
}

function scheduleWorkspaceSave({ viewOnly = false } = {}) {
  if (layoutReadOnly() || restoringWorkspace || pendingCommand === "new-cut" || !source) return;
  if (kitLayout()) {
    if (!viewOnly) workspaceChange++;
    renderKitSave();
    return;
  }
  workspaceChange++;
  clearTimeout(saveTimer);
  if (workspaceRevision === null) return;
  saveTimer = setTimeout(() => void persistWorkspace(), 250);
}

function writeWorkspace(workspace, partCorrection = false) {
  if (kitLayout()) {
    const { kitId: id, widthMm, quantity } = managedCut;
    return getJson("/api/registry", jsonRequest({
      action: "save-kit-layout", id, widthMm, quantity, revision: workspaceRevision, workspace,
    }, AbortSignal.timeout(30_000)));
  }
  return getJson(managedCut ? "/api/operator-workspace" : "/api/workspace", jsonRequest({
    revision: workspaceRevision, workspace,
    ...(managedCut ? { action: "save", cutId: managedCut.id, mode: managedCut.mode } : {}),
    ...(partCorrection ? { partCorrection: true } : {}),
  }, AbortSignal.timeout(30_000)));
}

function updateManagedCutMetadata(response) {
  if (!managedCut || kitLayout()) return false;
  const revised = managedCut.cutRevision !== response.cutRevision;
  const widthChanged = Object.hasOwn(response, "widthMm") && managedCut.widthMm !== response.widthMm;
  managedCut.cutRevision = response.cutRevision;
  if (widthChanged) managedCut.widthMm = response.widthMm;
  return revised || widthChanged;
}

function persistWorkspace() {
  clearTimeout(saveTimer);
  if (savePromise) return savePromise;
  if (workspaceRevision === null || pendingCommand === "new-cut") return Promise.resolve(false);
  if (workspaceSaved === workspaceChange) return Promise.resolve(true);
  savePromise = (async () => {
    try {
      while (workspaceSaved !== workspaceChange) {
        const change = workspaceChange;
        const response = await writeWorkspace(workspaceSnapshot());
        if (kitLayout()) {
          managedCut.saved = true;
        } else if (updateManagedCutMetadata(response)) {
          resetCutPlan({ keepView: true });
        }
        workspaceRevision = response.revision;
        workspaceSaved = change;
      }
      setSaveError();
      return true;
    } catch (error) {
      if (error.status === 409) workspaceRevision = null;
      setSaveError(kitLayout() || error.status !== 409 ? "Не сохранено: " + messageFrom(error)
        : "Другая вкладка сохранила правки. Перезагрузите страницу.");
      return false;
    } finally {
      savePromise = null;
    }
  })();
  return savePromise;
}

function renderKitSave() {
  const button = $("kit-layout-save");
  const visible = kitLayout() && layoutVisible();
  updateControl(button, "hidden", !visible);
  if (!visible) return;
  const saving = pendingCommand === "save-kit-layout";
  const saved = managedCut.saved && workspaceChange === workspaceSaved;
  updateText(button, saving ? "Сохраняем…" : saved ? "Раскладка сохранена" : "Сохранить раскладку");
  updateControl(button, "disabled", saved || busy() || drawingLocked() || workspaceRevision === null || !drawingPartsReady);
}

async function saveKitLayout() {
  if (!kitLayout() || $("kit-layout-save").disabled || drag) return;
  if (workspaceChange === workspaceSaved) workspaceChange++;
  setPending("save-kit-layout");
  try {
    await persistWorkspace();
  } finally {
    setPending(null);
  }
}

function applyWorkspace(response) {
  workspaceRevision = response.revision;
  workspaceChange = workspaceSaved = 0;
  const saved = response.workspace;
  clearSavedMeasurements();
  selection = new Set();
  partLegendSearch.value = "";
  partLegendList.scrollTop = 0;
  setPartLegendExpanded(true);
  drawingUndo.length = drawingRedo.length = 0;
  source = saved?.source || null;
  placed = saved?.paths || [];
  excluded = new Set(saved?.excluded || []);
  routeApplied = saved?.optimization.routeApplied ?? true;
  $("optimize-join").checked = saved?.optimization.join ?? true;
  nestingSettings = saved?.optimization.nesting || null;
  cameraMode = saved?.view.cameraMode ?? "work";
  updateOptimizationControls();
  updateDrawing({ edited: false });
  previewCutPage = saved?.view.section ?? null;
  if (saved && baseCamera && cameraMode === "manual") {
    if (hasRollLayout() && saved.view.zoom > 1) {
      cameraMode = "overview";
      resizeCamera();
    } else {
      const width = baseCamera.width * saved.view.zoom;
      const height = baseCamera.height * saved.view.zoom;
      camera = { x: saved.view.focusX - width / 2, y: saved.view.focusY - height / 2, width, height };
      drawCamera();
    }
  }
}

function resetCutPlan({ keepView = false } = {}) {
  clearTimeout(cutPlanTimer);
  cutPlanEpoch++;
  cutPlanRequest?.controller.abort();
  cutPlanRequest = null;
  const widthPending = cutWidthPending();
  pendingCanvasPlan = !widthPending && keepView && includedBounds ? (cutPlan?.pages.length ? cutPlan : pendingCanvasPlan) : null;
  cutPlan = null;
  keepViewForPlan = keepView;
  if (!keepView) previewCutPage = null;
  cutPlanError = "";
  xiaoResult = null;
  closeUploadConfirmation();
  warningIndices = new Set();
  sectionByPath = new Map();
  drawingProgressModel = null;
  renderCutProgress();
  simulation.close();
  renderPaths();
  renderPages();
  if (source && includedBounds && !widthPending) cutPlanTimer = setTimeout(() => void requestCutPlan(), 250);
}

function indexCutSections(plan, includedIndices) {
  const byPath = new Map();
  if (plan.mode !== "roll") return byPath;
  const byPart = new Map();
  const assign = (map, key, section) => {
    if (!map.has(key)) map.set(key, section);
    else if (map.get(key) !== section) map.set(key, null);
  };
  for (const page of plan.pages) {
    for (const path of page.paths) {
      const index = includedIndices[path.index];
      if (index === undefined) continue;
      assign(byPath, index, page.number);
      const part = placed[index].partNumber;
      if (part) assign(byPart, part, page.number);
    }
  }
  for (const index of byPath.keys()) {
    if (byPart.get(placed[index].partNumber) === null) byPath.set(index, null);
  }
  return byPath;
}

function requestCutPlan() {
  if (cutWidthPending()) return Promise.resolve(null);
  if (cutPlan) return Promise.resolve(cutPlan);
  const epoch = cutPlanEpoch;
  if (cutPlanRequest?.epoch === epoch) return cutPlanRequest.promise;
  const includedIndices = placed.flatMap((_, index) => excluded.has(index) ? [] : [index]);
  if (!source || !includedIndices.length) return Promise.resolve(null);
  const controller = new AbortController();
    const promise = (async () => {
    try {
      const plan = await getJson("/api/cut-plan", jsonRequest({
        job: { ...buildJob(), ...(managedCut && !kitLayout() ? { cutContext: [managedCut.id, managedCut.cutRevision] } : {}) },
        filename: source.filename,
      }, AbortSignal.any([controller.signal, AbortSignal.timeout(30_000)])), !layoutPreparation());
      if (epoch !== cutPlanEpoch) return null;
      cutPlan = plan;
      pendingCanvasPlan = null;
      if (plan.planId !== transferChecklistPlanId) {
        transferChecklistPlanId = plan.planId;
        resetTransferChecklist();
      }
      cutPlanError = plan.issues.map(({ message }) => message).join(" ");
      if (previewCutPage > plan.pages.length || plan.mode !== "roll") previewCutPage = null;
      warningIndices = new Set(plan.issues.flatMap(({ indices }) => indices.map((index) => includedIndices[index])));
      sectionByPath = indexCutSections(plan, includedIndices);
      drawingProgressModel = makeDrawingProgressModel(plan, includedIndices);
      renderPaths();
      updateCanvasSection();
      renderRun();
      return plan;
    } catch (error) {
      if (epoch === cutPlanEpoch) {
        cutPlanError = messageFrom(error);
        pendingCanvasPlan = null;
      }
      return null;
    } finally {
      if (cutPlanRequest?.epoch === epoch) cutPlanRequest = null;
      if (epoch === cutPlanEpoch) {
        keepViewForPlan = false;
        renderLayoutStatistics();
        renderLayoutActions();
      }
    }
  })();
  cutPlanRequest = { epoch, promise, controller };
  return promise;
}

function updateDrawing({ edited = true, keepView = edited, keepRotation = false, changedIndices = null, save = edited } = {}) {
  cancelCanvasGesture();
  if (!keepRotation) endFreeRotation();
  closePartMenu();
  if (!placed.length && (savedMeasurements.size || measurementUndo.length || measurementRedo.length)) clearSavedMeasurements();
  if (measureMode && !placed.length) setMeasureMode(false);
  else if (edited && measurement) {
    clearMeasurement();
    renderMeasurement();
  }
  const affectsCut = !changedIndices || [...changedIndices].some((index) => !excluded.has(index));
  if (edited && affectsCut) clearCurrentUpload();
  if (changedIndices) for (const index of changedIndices) pathBoxes[index] = bounds([placed[index]]);
  else pathBoxes = placed.map((path) => bounds([path]));
  indexDrawingParts();
  selection = expandToWholeParts(selection);
  svg.classList.toggle("has-drawing", placed.length > 0);
  $("empty-canvas").hidden = placed.length > 0;
  if (affectsCut) {
    const previousLength = Math.max(machine.width, includedBounds?.maxX ?? 0);
    includedBounds = null;
    for (const [index, box] of pathBoxes.entries()) if (!excluded.has(index)) {
      includedBounds = includedBounds ? unionPlacementBoxes(includedBounds, box) : box;
    }
    refreshPlacementWarnings();
    resetCutPlan({ keepView });
    if (!changedIndices || previousLength !== Math.max(machine.width, includedBounds?.maxX ?? 0)) resizeCamera({ keepView });
  } else renderPaths();
  if (changedIndices && keepRotation) renderFreeRotation();
  updateSelection();
  if (save) scheduleWorkspaceSave();
  if (changedIndices) {
    renderSelectionControls(drawingLocked());
    if (affectsCut) renderLayoutStatistics();
    renderLayoutActions();
  } else renderControls();
  void identifyDrawingParts().catch(() => {});
}

async function loadDrawing(file) {
  if (!file || drawingLocked() || !layoutVisible() || managedCut) return;
  if (!/\.(dxf|dwg)$/i.test(file.name)) {
    notifyError("Выберите файл DXF или DWG.");
    return;
  }
  cancelCanvasGesture();
  clearCurrentUpload();
  isLoading = true;
  renderControls();
  try {
    const imported = await getJson("/api/import?filename=" + encodeURIComponent(file.name), {
      method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" },
    });
    const box = bounds(imported.paths);
    if (!box) throw new Error("В файле нет контуров.");
    clearSavedMeasurements();
    source = imported;
    partLegendSearch.value = "";
    setPartLegendExpanded(true);
    placed = imported.paths.map((path) => translatePath(path, 20 - box.minX, 20 - box.minY));
    excluded = new Set();
    selection = new Set();
    routeApplied = true;
    drawingUndo.length = 0;
    drawingRedo.length = 0;
    updateDrawing({ keepView: false });
    fitCamera();
  } catch (error) {
    notifyError(messageFrom(error));
  } finally {
    isLoading = false;
    $("file-input").value = "";
    renderControls();
  }
}

function setMachineError(message = "") {
  const label = $("machine-feedback");
  updateText(label, message);
  updateControl(label, "hidden", !message);
}

function busy() {
  return Boolean(switchingMachineMode || pendingCommand || stopPending || activeHold || appUpdate?.state === "restarting");
}

function controlActive() {
  return Boolean(control && performance.now() < control.expiresAt);
}

function readStoredBoolean(key) {
  try {
    return sessionStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function machineStorageKey(name) {
  return demoMode ? "plotter.demo." + name : "plotter." + name;
}

function setClampLowered(lowered) {
  clampLowered = lowered;
  try {
    sessionStorage.setItem(machineStorageKey("clampLowered"), String(lowered));
    sessionStorage.removeItem(machineStorageKey("manualFeed"));
  } catch {
    // Browser storage is optional.
  }
}

function setPending(command) {
  if (command) setMachineError();
  pendingCommand = command;
  liveEpoch++;
  if (command) clearTimeout(liveTimer);
  renderHead();
  renderControls();
  if (!command) scheduleLiveRead(0);
}

function setVacuumEnabled(enabled) {
  vacuumEnabled = enabled;
  try {
    sessionStorage.setItem(machineStorageKey("vacuumEnabled"), String(enabled));
  } catch {
    // Browser storage is optional.
  }
}

function applyActuatorState(state) {
  if (!state || typeof state.epoch !== "string" || !Number.isInteger(state.revision) || state.revision < 0) return;
  if (state.epoch === actuatorEpoch && state.revision <= actuatorRevision) return;
  actuatorEpoch = state.epoch;
  actuatorRevision = state.revision;
  if (typeof state.clampLowered === "boolean") setClampLowered(state.clampLowered);
  if (typeof state.vacuumEnabled === "boolean") setVacuumEnabled(state.vacuumEnabled);
}

function clearCurrentUpload() {
  currentUpload = null;
}

function resetTransferChecklist() {
  const autoRun = $("manual-auto-run-off");
  autoRun.checked = autoRun.defaultChecked;
}

function updateControllerRoll(roll) {
  const previous = controllerRoll;
  const sameTransition = previous?.state === "advance" && roll?.state === "advance"
    && roll.planId === previous.planId && roll.nextPage === previous.nextPage;
  if (!sameTransition) rollPositioningAttempted = false;
  if (roll?.state === "advance" && !sameTransition) {
    resetTransferChecklist();
  }
  controllerRoll = roll;
}

function applyJobStatus(response) {
  if (Object.hasOwn(response, "selectedCut")) hardwareCutId = response.selectedCut;
  if (Object.hasOwn(response, "coordinateMove")) {
    coordinateMove = response.coordinateMove;
  }
  applyActuatorState(response.actuators);
  if (Object.hasOwn(response, "cutProgress")) cutProgress = response.cutProgress;
  if (Object.hasOwn(response, "roll")) updateControllerRoll(response.roll);
  if (typeof response.uploadUncertain === "boolean") uploadUncertain = response.uploadUncertain;
  if (response.run?.startedFromPanel && response.run.startedAt && currentUpload?.statistics
      && currentUpload.receipt === response.run.startedReceipt && currentUpload.indexLimit === response.run.indexLimit) {
    rememberRunStatistics(response.run, currentUpload.statistics);
    clearCurrentUpload();
  }
  const lost = response.uploadUncertain === true || response.jobLoaded === false
    || (response.responded === false && !response.busy);
  const hasReceipt = Object.hasOwn(response, "jobReceipt");
  const indexChanged = currentUpload && Number.isInteger(response.indexLimit)
    && response.indexLimit !== currentUpload.indexLimit;
  if (lost || indexChanged || (hasReceipt && response.jobReceipt !== currentUpload?.receipt)) clearCurrentUpload();
  if (response.run) activeRun = response.run;
  else if (response.responded === false && activeRun) {
    activeRun = { ...activeRun, fresh: false };
    if (runInProgress() && !response.busy) activeRun.state = "unknown";
  }
}

function coordinateMoveBlocksControl() {
  return coordinateMove?.state === "moving" || coordinateMove?.state === "unconfirmed";
}

function needsMotionVerification() {
  return stopNeedsVerification || coordinateMove?.state === "unconfirmed";
}

function canSwitchMachineMode() {
  return (demoAvailable || demoMode) && liveObserved
    && !busy() && !runInProgress() && activeRun?.state !== "transferring"
    && !live.stopAvailable && !live.busy && !coordinateMoveBlocksControl() && !needsMotionVerification() && !xiaoHandoff;
}

async function toggleDemoMode() {
  if (!canSwitchMachineMode()) return;
  switchingMachineMode = true;
  disarmControl();
  clearTimeout(liveTimer);
  liveEpoch++;
  machineModeEpoch++;
  demoMode = !demoMode;
  try {
    sessionStorage.setItem("plotter.demoMode", String(demoMode));
  } catch { /* Session storage is optional. */ }
  machine = { ip: "—", width: machine.width, height: machine.height };
  live = { responded: false, busy: false, hold: {}, operations: {}, position: null };
  liveObserved = false;
  headSample = null;
  operationRefreshUntil = 0;
  coordinateMove = null;
  actuatorEpoch = null;
  actuatorRevision = -1;
  clampLowered = readStoredBoolean(machineStorageKey("clampLowered"));
  vacuumEnabled = readStoredBoolean(machineStorageKey("vacuumEnabled"));
  stopNeedsVerification = false;
  uploadUncertain = false;
  activeRun = null;
  controllerRoll = null;
  cutProgress = null;
  restoredRunStatistics = null;
  trackedRun = readTrackedRun();
  pagePickerPlan = null;
  transferChecklistPlanId = null;
  resetTransferChecklist();
  resetCutPlan();
  setMachineError();
  renderLive();
  try {
    await readConfiguration();
  } finally {
    switchingMachineMode = false;
    renderLive();
    scheduleLiveRead(0);
  }
}

function operationAllowed(action, allowPending = false) {
  if (kitLayout()) return false;
  if (action === "cancel-cut") {
    const observedStop = live.stopAvailable ?? runInProgress();
    const canStop = activeRun?.state !== "cancelling"
      && (observedStop || Boolean(activeRun?.startedAt && activeRunStates.has(activeRun.state)));
    return !stopPending && canStop && (controlActive() || machine.stopDuringExternalRun === true);
  }
  if (!controlActive() || stopPending || activeHold || (!allowPending && pendingCommand)) return false;
  if (materialOperations.has(action) && !(machine.apiVersion >= 12)) return false;
  if (action === "pause-cut" || action === "resume-cut") {
    if (action === "resume-cut" && (stopNeedsVerification || coordinateMoveBlocksControl())) return false;
    return activeRun?.fresh && activeRun.state === (action === "pause-cut" ? "running" : "paused");
  }
  if (action === "acknowledge-run") {
    return runInProgress() && ["unknown", "error"].includes(activeRun?.state) && live.responded && !live.busy;
  }
  if (stopNeedsVerification || coordinateMoveBlocksControl()) return false;
  if (["clamp", "raise-clamp"].includes(action) && machine.clampControl !== true) return false;
  if (["vacuum", "vacuum-off"].includes(action) && machine.independentVacuumControl !== true) return false;
  if (runInProgress() || activeRun?.state === "transferring") return false;
  if (!live.responded || live.busy) return false;
  if (!live.positionFresh && action !== "home") return false;
  if (live.operations[action] === false) return false;
  if (action === "move-to") return machine.coordinateMove === true && live.operations[action] === true
    && !live.stopAvailable && activeRun?.followupFeedPending !== true && !switchingMachineMode && !appUpdateInProgress();
  if (action !== "cut") return true;
  return Boolean(!cutWidthPending() && !jobError && !drawingLocked() && !cutPlacementPreviewActive() && !simulation.active
    && currentUpload && cutPlan?.planId === currentUpload.planId && $("job-verified").checked);
}

function uploadPageNumber(plan = cutPlan) {
  if (cutWidthPending() || !plan?.canUpload) return null;
  if (controllerRoll) return controllerRoll.planId === plan.planId ? controllerRoll.nextPage : null;
  const page = plan.mode === "roll" && machine.apiVersion >= 17 ? selectedCutPage : 1;
  return plan.pages[page - 1]?.canStart === false ? null : page;
}

function needsPagePositionConfirmation(page) {
  if (demoMode && machine.automaticRollFeed === true && controllerRoll?.materialAdvanced
      && controllerRoll.planId === cutPlan?.planId && controllerRoll.nextPage === page) return false;
  return Boolean(page && (cutPlan?.hasContinuations || controllerRoll?.state === "advance" || !controllerRoll && page > 1));
}

function needsManualAutoRunConfirmation() {
  return machine.directRoll === true && machine.autoRunReadback === false;
}

function renderPagePicker(plan, roll, disabled) {
  const picker = $("job-page");
  const visible = Boolean(plan?.mode === "roll" && plan.canUpload && machine.apiVersion >= 17);
  updateControl($("job-page-field"), "hidden", !visible);
  if (visible && cutPlan && pagePickerPlan !== plan.planId) {
    const preserved = plan.pages[selectedCutPage - 1];
    selectedCutPage = preserved && preserved.canStart !== false ? preserved.number
      : plan.pages.find((page) => page.canStart !== false)?.number ?? 1;
    pagePickerPlan = plan.planId;
    const total = plan.pages.length;
    for (const [index, page] of plan.pages.entries()) {
      const label = `Проход ${page.number} из ${total}${page.canStart === false ? " · продолжение" : ""}`;
      if (picker.options[index]) updateControl(picker.options[index], "text", label);
      else picker.add(new Option(label, page.number));
    }
    if (picker.options.length > total) picker.options.length = total;
  }
  const next = visible ? roll?.nextPage ?? roll?.page ?? selectedCutPage : null;
  if (visible) {
    updateControl(picker, "value", String(next));
    for (const option of picker.options) {
      updateControl(option, "disabled", !controllerRoll && plan.pages[Number(option.value) - 1]?.canStart === false);
    }
    updateControl(picker, "disabled", disabled || Boolean(controllerRoll));
    updateControl(picker, "title", controllerRoll ? "В открытом сеансе следующий проход выбирается по ходу раскроя" : "");
  }
  shownCutPage = next;
  updateCanvasSection();
}

function waitingForAutomaticAdvance() {
  return demoMode && machine.automaticRollFeed === true && controllerRoll?.state === "advance"
    && controllerRoll.planId === cutPlan?.planId && !controllerRoll.materialAdvanced;
}

function automaticRollPositioningAllowed() {
  return !demoMode && machine.rollHeadPositioning === true && controllerRoll?.state === "advance"
    && (controllerRoll.headPositioning ?? "pending") === "pending" && !rollPositioningAttempted
    && controllerRoll.planId === cutPlan?.planId
    && !layoutPreparation() && !cutWidthPending() && !drawingLocked() && !cutPlacementPreviewActive()
    && !simulation.active && !currentUpload && !busy() && operationAllowed("move-to");
}

function uploadAllowed({ forAdvance = false } = {}) {
  if (layoutPreparation() || layoutReadOnly()) return false;
  if (waitingForAutomaticAdvance() && !forAdvance) return false;
  const autoRunOff = machine.autoRunReadback === true ? live.autoRunOff === true
    : needsManualAutoRunConfirmation() && live.autoRunOff !== false && $("manual-auto-run-off").checked;
  return machine.verifiedJobUpload === true && controlActive() && autoRunOff
    && !stopNeedsVerification && !coordinateMoveBlocksControl() && !jobError && !busy() && !drawingLocked()
    && !cutPlacementPreviewActive() && !simulation.active && activeRun?.state !== "transferring"
    && live.responded && !live.busy && live.positionFresh && live.operations.upload !== false
    && Boolean(uploadPageNumber()) && !currentUpload && workspaceRevision !== null;
}

function hasCurrentCut() {
  return Boolean(source || controllerRoll || cutProgress);
}

function canStartNewCut() {
  return machine.apiVersion >= 21 && hasCurrentCut() && workspaceRevision !== null && !drawingLocked() && !busy()
    && !live.busy && !live.stopAvailable && !needsMotionVerification() && !coordinateMoveBlocksControl()
    && activeRun?.state !== "transferring" && !xiaoHandoff;
}

function renderLayoutStatistics() {
  const plan = cutPlan?.pages.length ? cutPlan : pendingCanvasPlan;
  const empty = $("layout-empty");
  updateControl($("layout-statistics"), "hidden", !source);
  updateAttribute($("layout-statistics"), "aria-busy", String(Boolean(pendingCanvasPlan)));
  updateControl(empty, "hidden", Boolean(source));
  if (!simulation.active) layoutStatistics.setPlan(plan, false);
}

function renderSelectionControls(importing) {
  const editing = selectedBounds && !importing && drawingPartsReady;
  for (const id of ["offset-x", "offset-y", "rotation", "rotate-button"]) {
    updateControl($(id), "disabled", !editing);
  }
  const editGeometry = editing && !kitLayout();
  updateControl($("size-width"), "disabled", !editGeometry || selectedBounds.maxY - selectedBounds.minY < 0.001);
  updateControl($("size-height"), "disabled", !editGeometry || selectedBounds.maxX - selectedBounds.minX < 0.001);
  updateControl($("exclude-button"), "disabled", !editGeometry);
  updateControl($("delete-button"), "disabled", !editGeometry);
  updateControl($("measure-tool"), "disabled", importing || !placed.length);
  renderHistoryControls(importing);
}

function renderHistoryControls(importing = drawingLocked()) {
  for (const [id, history, action, shortcut] of [
    ["undo-tool", measureMode ? measurementUndo : drawingUndo, "Отменить", "Ctrl+Z"],
    ["redo-tool", measureMode ? measurementRedo : drawingRedo, "Повторить", "Ctrl+Shift+Z"],
  ]) {
    const label = hasMeasurementCloseStep(id === "redo-tool") ? "закрытие линейки" : history.at(-1)?.label;
    updateControl($(id), "title", `${action}${label ? `: ${label}` : ""} (${shortcut})`);
    updateControl($(id), "disabled", importing || !label);
  }
}

function renderControls() {
  renderAppUpdate();
  renderKitSave();
  const armed = controlActive();
  const locked = busy();
  const importing = drawingLocked();
  const inProgress = runInProgress();
  const transferring = pendingCommand === "upload" || activeRun?.state === "transferring";
  const jobActive = inProgress || stopPending || Boolean(live.stopAvailable);
  updateControl(demoSwitch, "disabled", Boolean(managedCut) || !canSwitchMachineMode());
  updateAttribute(demoSwitch, "aria-checked", String(demoMode));
  updateControl(demoSwitch, "title", demoMode ? "Вернуться к реальному станку" : "Включить виртуальный станок");
  const connectButton = $("connect-companion");
  const useLocalPanel = location.protocol === "http:";
  updateControl(connectButton, "hidden", !centralMode || companionConnected || demoMode);
  updateControl(connectButton, "disabled", companionConnecting || locked);
  updateText(connectButton, useLocalPanel ? "Открыть панель станка"
    : companionConnecting ? "Подключаем…" : "Подключить помощник");
  renderLayoutStatistics();
  if (importing) {
    closePartMenu();
    if (drag?.type === "move" || drag?.type === "rotate") cancelCanvasGesture();
    endFreeRotation();
    if (measureMode) setMeasureMode(false);
  }
  updateControl($("canvas-import"), "disabled", importing);
  const newCutButton = $("new-cut-button");
  updateControl(newCutButton, "hidden", !layoutVisible() || Boolean(managedCut) || !(machine.apiVersion >= 21) || !hasCurrentCut());
  updateControl(newCutButton, "disabled", !canStartNewCut());
  updateControl($("new-cut-confirm"), "disabled", newCutButton.disabled);
  renderSelectionControls(importing);
  const includedCount = placed.length - excluded.size;
  updateControl($("nest-button"), "disabled", importing || !includedCount);
  updateControl($("optimize-button"), "disabled", importing || !includedCount);
  updateContourCheckControls();
  if (nestDialog.open || optimizeDialog.open) updateOptimizationControls();

  const armButton = $("arm-button");
  updateControl(armButton, "disabled", kitLayout() || (armed ? Boolean(activeHold) || ["arm", "new-cut"].includes(pendingCommand)
    : locked || !live.responded || live.busy || (!live.positionFresh && !inProgress)));
  updateText(armButton, pendingCommand === "arm" ? "Включаем…" : armed ? "Отключить управление" : "Включить управление");
  armButton.classList.toggle("armed", armed);
  updateAttribute(armButton, "aria-pressed", String(armed));
  updateControl($("motion-verification"), "hidden", !needsMotionVerification());
  updateControl($("verify-stop-button"), "disabled", !armed || locked || !live.responded || live.busy || machine.persistentControl !== true);
  updateText($("motion-verification-status"), machine.persistentControl === true
    ? "Остановка пока не подтверждена. Движение заблокировано."
    : "Обновите сервер плоттера, чтобы проверить остановку без отключения управления.");

  for (const button of holdButtons) {
    const held = activeHold?.button === button && !activeHold.released;
    const direction = button.dataset.controllerHold;
    const available = !stopNeedsVerification && !coordinateMoveBlocksControl() && live.positionFresh && live.hold[direction] === true;
    updateAttribute(button, "aria-pressed", String(held));
    updateControl(button, "disabled", !held && (!armed || locked || runInProgress() || activeRun?.state === "transferring" || !live.responded || live.busy || !available));
  }
  const canMoveTo = operationAllowed("move-to");
  updateControl($("move-to-button"), "disabled", !canMoveTo);
  updateControl($("move-to-submit"), "disabled", !canMoveTo);
  const pause = $("pause-button");
  const stop = $("cancel-button");
  updateAttribute(pause, "data-operation", activeRun?.state === "paused" ? "resume-cut" : "pause-cut");
  updateText(pause.querySelector("span"), pause.dataset.operation === "resume-cut" ? "Продолжить" : "Пауза");
  for (const button of operationButtons) updateControl(button, "disabled", !operationAllowed(button.dataset.operation));
  updateControl(pause, "hidden", !inProgress || !["running", "pausing", "paused", "resuming"].includes(activeRun?.state));
  updateControl(stop, "hidden", !jobActive);
  updateControl($("job-action-row"), "hidden", pause.hidden && stop.hidden);
  renderClamp();
  renderVacuum();
  renderLayoutActions({ importing, locked, inProgress, transferring, jobActive, armed });
  const acknowledgeRun = $("run-acknowledge");
  updateControl(acknowledgeRun, "hidden", !(runInProgress() && ["unknown", "error"].includes(activeRun?.state)));
  updateControl(acknowledgeRun, "disabled", !operationAllowed("acknowledge-run"));
  if (!acknowledgeRun.hidden) updateText(acknowledgeRun, demoMode
    ? "Подтверждаю остановку виртуального станка" : "Подтверждаю остановку на пульте");
}

function launchOrderLabel() {
  const orderNumber = managedCut?.order?.number?.trim();
  return orderNumber ? `Наряд № ${orderNumber}` : "";
}

function launchLayoutName() {
  const order = managedCut?.order;
  const product = order?.designation?.trim() || order?.modelName?.trim();
  const names = [product, managedCut?.name?.trim()].filter(Boolean);
  return [...new Set(names)].join(" · ");
}

function importedLayoutName(filename) {
  if (filename === "registry.dxf") return "Детали из реестра";
  return filename || "";
}

function xiaoDownloadName(filename) {
  const title = managedCut ? [launchOrderLabel(), launchLayoutName()].filter(Boolean).join(" — ") : importedLayoutName(filename);
  const clean = title.replace(/\.(dxf|dwg)$/i, "").replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, " ")
    .replace(/\s+/g, " ").trim();
  const encoder = new TextEncoder();
  let bytes = 0, base = "";
  for (const character of clean) {
    bytes += encoder.encode(character).length;
    if (bytes > 220) break;
    base += character;
  }
  return (base.replace(/[. ]+$/g, "") || "Раскрой") + " — раскладка.dxf";
}

function renderLayoutActions({ importing = drawingLocked(), locked = busy(), inProgress = runInProgress(),
  transferring = pendingCommand === "upload" || activeRun?.state === "transferring",
  jobActive = inProgress || stopPending || Boolean(live.stopAvailable), armed = controlActive() } = {}) {
  const partMoving = cutPlacementPreviewActive();
  const widthPending = cutWidthPending();
  const displayedPlan = cutPlan ?? pendingCanvasPlan;
  const planPending = !cutPlan && Boolean(includedBounds) && !cutPlanError;
  updateControl($("simulation-button"), "disabled", (importing && !layoutReadOnly()) || partMoving || stopPending || Boolean(currentUpload) || Boolean(jobError)
    || widthPending || planPending || !includedBounds || Boolean(cutPlan && !cutPlan.pages.length));
  const uploadButton = $("controller-upload-button");
  const advanceButton = $("roll-advance-button");
  const roll = controllerRoll?.planId === displayedPlan?.planId ? controllerRoll : null;
  renderPagePicker(displayedPlan, roll, importing || partMoving || !cutPlan || locked || Boolean(currentUpload));
  const directRoll = cutPlan?.mode === "roll" && cutPlan.canUpload && machine.directRoll === true;
  const waitingForAdvance = roll?.state === "advance";
  const visibleUploadPage = uploadPageNumber(displayedPlan);
  const manualAutoRun = Boolean(visibleUploadPage && !currentUpload && !jobActive && needsManualAutoRunConfirmation());
  const canResetRoll = Boolean(controllerRoll && ["completed", "cancelled", "unknown", "ready", "advance"].includes(controllerRoll.state));
  updateControl($("manual-auto-run"), "hidden", !manualAutoRun);
  updateControl($("manual-auto-run-off"), "disabled", importing || partMoving || !cutPlan || locked || !live.responded || live.busy);
  updateControl($("roll-reset"), "hidden", !canResetRoll || jobActive);
  updateControl($("job-pass-frame"), "hidden", $("job-page-field").hidden && $("launch-pass-navigation").hidden && $("roll-reset").hidden);
  updateControl($("roll-reset"), "disabled", !armed || locked || importing || live.busy || !live.positionFresh || stopNeedsVerification || coordinateMoveBlocksControl());
  updateText($("roll-reset"), roll?.state === "ready" && !roll.completedPages && !cutPlan?.hasContinuations
    ? "Сменить начальный проход" : "Начать раскрой заново");
  updateControl(uploadButton, "disabled", !(currentUpload ? operationAllowed("cut") : uploadAllowed()));
  if (uploadConfirmDialog.open) {
    if (inProgress || stopPending) closeUploadConfirmation();
    else updateControl($("upload-confirm-accept"), "disabled", !matchesLaunchIntent(uploadConfirmation));
  }
  updateText(uploadButton.querySelector("span"), transferring ? "Загрузка…" : "Старт");
  const canDownloadXiao = !widthPending && !jobError && cutPlan?.canPrepareXiao === true;
  const useXiao = canDownloadXiao && !currentUpload && !cutPlan.canUpload;
  const showAdvance = !widthPending && waitingForAutomaticAdvance() && !jobActive && !layoutPreparation() && !layoutReadOnly();
  updateControl(advanceButton, "hidden", !showAdvance);
  updateControl(advanceButton, "disabled", !showAdvance || !uploadAllowed({ forAdvance: true }));
  updateText(advanceButton, pendingCommand === "advance-roll" ? "Подтягиваем полотно…" : "Подтянуть следующий проход");
  updateControl(uploadButton, "hidden", (!visibleUploadPage && !currentUpload && !transferring)
    || showAdvance || jobActive || ["cut", "new-cut"].includes(pendingCommand));
  updateControl($("job-prepare-actions"), "hidden", uploadButton.hidden && advanceButton.hidden);
  const xiaoOpen = $("prepare-xiao-open");
  const xiaoDownload = $("prepare-xiao-download");
  const showXiaoOpen = !demoMode && machine.xiaoAvailable && displayedPlan?.canPrepareXiao && !displayedPlan.canUpload;
  updateControl(xiaoOpen, "hidden", !showXiaoOpen);
  updateControl(xiaoOpen, "disabled", !useXiao || importing || partMoving || locked || jobActive || Boolean(xiaoHandoff));
  updateControl(xiaoDownload, "disabled", !canDownloadXiao || importing || partMoving || Boolean(xiaoHandoff));
  updateControl(xiaoDownload, "title", !canDownloadXiao ? jobError || cutPlanError || "Дождитесь проверки раскладки."
    : partMoving ? "Завершите перемещение детали." : "");
  updateText(xiaoOpen, xiaoHandoff === "open" ? "Открываем…" : "Открыть в Xiao");
  updateText(xiaoDownload, xiaoHandoff === "download" ? "Готовим DXF…" : "Скачать DXF для Xiao");
  updateControl($("xiao-handoff"), "hidden", widthPending || !includedBounds);
  updateControl($("xiao-result"), "hidden", !xiaoResult);
  updateText($("xiao-result"), xiaoResult || "");
  let status;
  if (pendingCommand === "new-cut") status = "Очищаем рабочую область…";
  else if (stopPending) status = "Отправляем команду остановки…";
  else if (pendingCommand === "cut") status = "Запускаем раскрой…";
  else if (transferring) status = "Передача чертежа…";
  else if (inProgress) status = runLabels[live.externalMotion || activeRun?.state] || "Проверьте штатный пульт";
  else if (stopNeedsVerification) status = "Ожидаем подтверждение остановки";
  else if (widthPending && includedBounds) status = "Укажите ширину полотна и выполните автораскладку всех деталей.";
  else if (machine.verifiedJobUpload === false) status = "Перезапустите локальный сервер плоттера";
  else if (!source) status = "Откройте DXF или DWG";
  else if (!includedBounds) status = "Добавьте контур к заданию";
  else if (jobError || cutPlanError) status = jobError || cutPlanError;
  else if (!cutPlan) status = "Обновляем расчёт раскладки…";
  else if (controllerRoll && !roll) status = "На станке открыт другой рулон. Завершите его сеанс перед загрузкой.";
  else if (currentUpload) status = "";
  else if (roll?.state === "completed") status = `Раскрой завершён · ${roll.completedPages} из ${roll.pages - (roll.firstPage ?? 1) + 1} проходов`;
  else if (cutProgress?.state === "completed" && cutProgress.planId === cutPlan.planId) status = "Раскрой завершён";
  else if (roll?.state === "cancelled" || roll?.state === "unknown") status = `Сверьте проход ${roll.page} и положение ткани перед продолжением.`;
  else if (waitingForAdvance && showAdvance) status = `Проход ${roll.page} завершён. Протяжка на ${formatFeedDistance.format(roll.feedAfterMm)} мм.`;
  else if (waitingForAdvance && roll.materialAdvanced) status = `Полотно подтянуто. Проход ${roll.nextPage} готов к запуску.`;
  else if (waitingForAdvance) status = `Проход ${roll.page} завершён. Следующий проход — ${roll.nextPage}.`;
  else if (useXiao && cutPlan.uploadIssue) status = cutPlan.uploadIssue;
  else if (useXiao && demoMode) status = "Раскладка доступна для скачивания в DXF.";
  else if (useXiao && [...sectionByPath.values()].includes(null)) {
    status = !machine.continuousRoll ? "Для продолжения длинных деталей обновите локальный сервер. Также можно открыть цельную раскладку в Xiao."
      : "Прямая загрузка недоступна для этой раскладки. Можно открыть цельную раскладку в Xiao.";
  }
  else if (useXiao) status = "Откройте раскладку в Xiao для отправки и запуска.";
  else if (cutPlan.mode === "roll" && !directRoll) status = "Обновите локальный сервер для запуска по проходам.";
  else if (uploadUncertain) status = "Предыдущая передача не подтверждена";
  else if (!cutPlan.canUpload) status = "Проверьте профиль передачи Xiao";
  else if (!live.responded) status = "Нет связи со станком";
  else if (live.busy) status = "Контроллер занят";
  else if (!armed) status = "Включите управление для загрузки";
  else if (live.autoRunOff === false) status = "Выключите Auto Run на штатном пульте";
  else if (manualAutoRun && !$("manual-auto-run-off").checked) status = "Отметьте проверку Auto Run для загрузки";
  else if (!manualAutoRun && machine.autoRunReadback === false) status = "Чтение Auto Run для платы ещё не настроено";
  else if (!manualAutoRun && live.autoRunOff !== true) status = "Режим Auto Run не считан";
  else status = live.uploadUnavailableReason || "";
  const statusLabel = $("loaded-job");
  updateText(statusLabel, status);
  updateControl(statusLabel, "hidden", !status && !includedBounds);
  statusLabel.classList.toggle("has-layout", Boolean(includedBounds));
  updateControl(uploadButton, "title", uploadButton.disabled ? status : "Подтвердить и запустить раскрой");
  const identity = $("job-identity");
  const orderLabel = launchOrderLabel();
  const title = managedCut ? (launchLayoutName() || orderLabel || "Раскрой") : importedLayoutName(source?.filename);
  updateText(identity, title);
  updateControl(identity, "title", identity.textContent);
  updateControl(identity, "hidden", !identity.textContent);
  const detail = $("job-detail");
  const dimensions = includedBounds
    ? `${number(includedBounds.maxY - includedBounds.minY)} × ${number(includedBounds.maxX - includedBounds.minX)} мм` : "";
  const passes = $("job-page-field").hidden && $("launch-pass-navigation").hidden && displayedPlan?.pages.length
    ? `проходов: ${displayedPlan.pages.length}` : "";
  updateText(detail, [orderLabel !== title ? orderLabel : "", dimensions, passes].filter(Boolean).join(" · "));
  updateControl(detail, "hidden", !detail.textContent);
}

function renderClamp() {
  const button = $("clamp-button");
  const action = clampLowered ? "raise-clamp" : "clamp";
  const sending = pendingCommand === "clamp" ? "Опускаем…"
    : ["raise-clamp", "cancel-cut"].includes(pendingCommand) ? "Поднимаем…" : "";
  updateAttribute(button, "data-operation", action);
  updateText(button.querySelector(".material-action-label"), sending || "Лапки");
  updateAttribute(button.querySelector("use"), "href", clampLowered ? "#i-lift" : "#i-lower");
  updateAttribute(button, "aria-busy", String(Boolean(sending)));
  button.classList.toggle("lowered", clampLowered);
  updateAttribute(button, "aria-pressed", String(clampLowered));
  updateControl(button, "disabled", !operationAllowed(action));
  updateControl(button, "title", sending ? "Команда отправляется. Дождитесь завершения."
    : machine.clampControl !== true || !(machine.apiVersion >= 12) ? "Перезапустите сервер плоттера"
    : clampLowered ? "Лапки опущены. Поднять лапки — вакуум выключится, Z поднимется."
    : "Лапки подняты. Опустить лапки.");
}

function renderVacuum() {
  const button = $("vacuum-button");
  const action = vacuumEnabled ? "vacuum-off" : "vacuum";
  const sending = pendingCommand === "vacuum" ? "Включаем…"
    : ["vacuum-off", "raise-clamp", "cancel-cut"].includes(pendingCommand) ? "Выключаем…" : "";
  updateAttribute(button, "data-operation", action);
  updateText(button.querySelector(".material-action-label"), sending || "Вакуум");
  updateAttribute(button, "aria-busy", String(Boolean(sending)));
  button.classList.toggle("enabled", vacuumEnabled);
  updateAttribute(button, "aria-pressed", String(vacuumEnabled));
  updateControl(button, "disabled", !operationAllowed(action));
  updateControl(button, "title", sending ? "Команда отправляется. Дождитесь завершения."
    : machine.independentVacuumControl !== true || !(machine.apiVersion >= 12) ? "Перезапустите сервер плоттера"
    : vacuumEnabled ? "Вакуум включён. Выключить вакуум." : "Вакуум выключен. Включить вакуум.");
}

function renderRun() {
  const run = activeRun;
  const partCounts = renderCutProgress();
  const started = cutProgress ? Number.isFinite(cutProgress.startedAt) || cutProgress.completedPages > 0
    : machine.liveCutProgress !== true && Number.isFinite(run?.startedAt) && run.state !== "idle"
      && (Boolean(run.startedReceipt) || (trackedRun?.startedAt === run.startedAt && trackedRun.filename === run.filename));
  const panel = $("run-panel");
  updateControl(panel, "hidden", !started);
  if (panel.hidden) return;
  const state = live.externalMotion || cutProgress?.state || run?.state;
  const label = state === "advance" ? "Смена прохода"
    : demoMode && (!runLabels[state] || state === "unknown") ? "Состояние не подтверждено"
    : runLabels[state] || "Сверьте пульт";
  updateText($("run-state"), label);
  updateAttribute($("run-state"), "data-state", state || "");
  renderRunStatistics(run ?? cutProgress);
  const page = cutProgress ?? controllerRoll ?? run;
  updateText($("run-page"), page?.page ? `${page.state === "advance" ? page.page + 1 : page.page} из ${page.pages}` : "—");
  updateText($("run-done"), partCounts ? `${partCounts.count} из ${partCounts.total}` : "—");
}

function renderLive() {
  const state = $("machine-state");
  const link = live.busy ? "busy" : live.responded ? "connected" : liveObserved ? "offline" : "";
  updateControl(state, "className", ("machine-state " + link).trim());
  updateControl(state, "title", live.message || "");
  updateText($("machine-state-label"), live.busy ? "Контроллер занят" : live.responded ? "На связи"
    : liveObserved ? "Нет связи" : "Проверяем связь");
  updateText($("machine-address"), demoMode ? "Виртуальный станок" : live.ip || machine.ip || "—");
  renderHead();
  renderRun();
  renderControls();
}

function applyLive(response) {
  liveObserved = true;
  live = {
    responded: Boolean(response.responded),
    busy: Boolean(response.busy),
    ip: response.ip,
    message: response.message,
    position: response.position || live.position,
    positionFresh: Boolean(response.position),
    hold: response.hold || {},
    operations: response.operations || {},
    uploadUnavailableReason: response.uploadUnavailableReason,
    autoRunOff: response.autoRunOff,
    externalMotion: Object.hasOwn(response, "externalMotion") ? response.externalMotion
      : response.responded ? { 3: "running", 9: "paused", 50: "feeding" }[response.lastEvent?.code] ?? null : live.externalMotion,
    stopAvailable: typeof response.stopAvailable === "boolean" ? response.stopAvailable : undefined,
  };
  if (response.motionInhibited === true) stopNeedsVerification = true;
  else if (stopNeedsVerification && response.motionInhibited === false && response.position && response.responded && !response.busy) {
    stopNeedsVerification = false;
    setMachineError();
  }
  applyJobStatus(response);
  if (simulation.active && runInProgress()) simulation.close();
  if (response.position && response.responded && !response.busy) observeHeadPosition(response.position, response.drawingPosition);
  renderLive();
}

async function readConfiguration() {
  const epoch = liveEpoch;
  const modeEpoch = machineModeEpoch;
  try {
    const { machine: configuration, apiVersion, appVersion, roll, cutProgress: progress, demoAvailable: available,
      demoMode: reportedMode, centralMode: central, companionMode: companion, selectedCut } =
      await getJson("/api/status", { signal: AbortSignal.timeout(3500) });
    if (modeEpoch !== machineModeEpoch || !configuration || (reportedMode === true) !== demoMode) return false;
    if (!appUpdate && typeof appVersion === "string") appUpdate = { version: appVersion };
    if (!companion) centralMode = central === true;
    hardwareCutId = selectedCut ?? null;
    demoAvailable = available === true;
    machine = { ...configuration, apiVersion, verifiedJobUpload: configuration.verifiedJobUpload === true };
    const { actuators, ...jobConfiguration } = configuration;
    applyJobStatus(jobConfiguration);
    if (epoch === liveEpoch) {
      applyActuatorState(actuators);
      if (roll !== undefined && controllerRoll === null) updateControllerRoll(roll);
      if (progress && !cutProgress) cutProgress = progress;
    }
    updateDrawing({ edited: false });
    renderLive();
    if (location.protocol === "https:" && centralMode && !demoMode && !companionConnected && !companionConnecting
        && readStoredBoolean("plotter.companion")) {
      void connectCompanion();
    }
    return true;
  } catch {
    // The live controller request reports local connectivity independently.
    return false;
  }
}

function scheduleLiveRead(delay = 2000) {
  clearTimeout(liveTimer);
  if (document.visibilityState === "visible") {
    liveTimer = setTimeout(() => void readLive(), delay);
  }
}

async function readLive() {
  if (busy() || document.visibilityState !== "visible") {
    scheduleLiveRead();
    return;
  }
  if (liveReading) {
    liveQueued = true;
    return;
  }
  const epoch = liveEpoch;
  const readStarted = performance.now();
  let finish;
  let observed = false;
  liveReading = new Promise((resolve) => { finish = resolve; });
  try {
    const response = await getJson("/api/controller-live", jsonRequest({}, AbortSignal.timeout(3500)));
    if (epoch === liveEpoch && !busy()) {
      applyLive(response);
      observed = response.responded === true;
    }
  } catch (error) {
    if (epoch === liveEpoch && !busy()) {
      const companionUnavailable = error.status === undefined || error.status === 403 || error.status === 404 || error.status >= 500;
      if (!demoMode && centralMode && companionConnected && companionUnavailable && !runInProgress() && !controlActive()) {
        companionConnected = false;
        try { sessionStorage.removeItem("plotter.companion"); } catch { /* Storage is optional. */ }
        setMachineError("Нет связи с помощником. Подключите его снова.");
        void readConfiguration();
      }
      applyLive({ responded: false, busy: error.status === 409, message: messageFrom(error) });
    }
  } finally {
    liveReading = null;
    finish();
    const immediate = liveQueued || epoch !== liveEpoch;
    liveQueued = false;
    const settling = performance.now() < operationRefreshUntil && (live.busy || !live.positionFresh);
    const cutting = activeRun?.state === "running" || live.externalMotion === "running";
    let delay = 2000;
    if (coordinateMove?.state === "moving") delay = 350;
    else if (settling) delay = 250;
    else if (cutting) {
      delay = Math.max(225, Math.min(1000, 2 * (performance.now() - readStarted)));
      if (live.busy) delay = Math.max(delay, 500);
      else if (!live.positionFresh) delay = 1000;
    }
    else if (runInProgress()) delay = 500;
    if (!busy()) scheduleLiveRead(immediate ? 0 : delay);
  }
  if (observed && automaticRollPositioningAllowed()) void advanceControllerRoll({ automatic: true });
}

function applyPosition(response) {
  if (typeof response.motionInhibited === "boolean") stopNeedsVerification = response.motionInhibited;
  if (response.position) {
    live.position = response.position;
    live.positionFresh = true;
  }
  if (response.hold) live.hold = response.hold;
  if (response.operations) live.operations = response.operations;
  if (Object.hasOwn(response, "autoRunOff")) live.autoRunOff = response.autoRunOff;
  applyJobStatus(response);
  if (response.position && !response.busy) observeHeadPosition(response.position);
  renderLive();
}

function sendDisarm(token) {
  return checkedFetch("/api/control-session", {
    ...jsonRequest({ action: "disarm", controlToken: token }, AbortSignal.timeout(2500)), keepalive: true,
  }).catch(() => {});
}

function disarmControl(message = "") {
  interruptControllerHold();
  clearTimeout(controlTimer);
  const token = control?.token;
  control = null;
  closeUploadConfirmation();
  if (moveToDialog.open) moveToDialog.close();
  clearCurrentUpload();
  if (token) void sendDisarm(token);
  if (token) setMachineError(message);
  renderControls();
}

function scheduleControlRenewal(delay) {
  clearTimeout(controlTimer);
  if (control) controlTimer = setTimeout(() => void renewControl(), delay);
}

async function renewControl() {
  const session = control;
  if (!session || controlRenewing === session) return;
  if (performance.now() >= session.expiresAt) {
    disarmControl("Сеанс управления истёк.");
    return;
  }
  if (machine.persistentControl !== true) {
    scheduleControlRenewal(Math.max(0, session.expiresAt - performance.now()));
    return;
  }
  controlRenewing = session;
  const started = performance.now();
  try {
    const response = await getJson("/api/control-session", jsonRequest({
      action: "renew", controlToken: session.token,
    }, AbortSignal.timeout(3500)));
    if (control !== session) return;
    if (response.armed !== true || !Number.isFinite(response.expiresIn) || response.expiresIn <= 0) {
      disarmControl("Сеанс управления закончился. Включите управление снова.");
      return;
    }
    session.expiresAt = started + response.expiresIn * 1000;
    scheduleControlRenewal(Math.min(30_000, response.expiresIn * 500));
  } catch {
    if (control === session) scheduleControlRenewal(Math.max(0, Math.min(3000, session.expiresAt - performance.now())));
  } finally {
    if (controlRenewing === session) controlRenewing = null;
  }
}

async function armControl() {
  if (controlActive()) {
    disarmControl();
    return;
  }
  if ($("arm-button").disabled || !focused()) return;
  const focus = focusEpoch;
  const started = performance.now();
  setPending("arm");
  try {
    const response = await getJson("/api/control-session", jsonRequest({
      action: "arm", ready: true,
    }, AbortSignal.timeout(5500)));
    if (typeof response.controlToken !== "string" || !response.controlToken) throw new Error("Контроллер не включил управление.");
    const ttl = Number.isFinite(response.expiresIn) ? response.expiresIn : 900;
    const expiresAt = started + ttl * 1000;
    if (focusEpoch !== focus || !focused() || expiresAt <= performance.now()) {
      void sendDisarm(response.controlToken);
      return;
    }
    control = { token: response.controlToken, expiresAt };
    scheduleControlRenewal(machine.persistentControl === true ? Math.min(30_000, ttl * 500) : expiresAt - performance.now());
    applyPosition(response);
  } catch (error) {
    setMachineError(messageFrom(error));
  } finally {
    setPending(null);
  }
}

async function startControllerHold(button, input) {
  if (button.disabled || busy() || !controlActive() || !focused()) return;
  if (input.pointerId !== undefined) {
    try { button.setPointerCapture(input.pointerId); } catch { return; }
  }
  const session = crypto.randomUUID();
  const hold = { session, button, input, released: false, timer: null };
  const { axis, direction } = controllerDirections[button.dataset.controllerHold];
  activeHold = hold;
  liveEpoch++;
  setMachineError();
  renderControls();
  try {
    const response = await getJson("/api/controller-hold", jsonRequest({
      action: "start", session, axis, direction, controlToken: control.token,
    }, AbortSignal.timeout(3500)));
    if (activeHold !== hold || hold.released) return;
    if (response.phase !== "holding") {
      void stopControllerHold(response.message || "Контроллер не начал движение.");
      return;
    }
    scheduleHoldRenew(hold);
  } catch (error) {
    if (activeHold === hold && !hold.released) void stopControllerHold(messageFrom(error));
  }
}

function scheduleHoldRenew(hold, delay = 150) {
  if (activeHold !== hold || hold.released) return;
  hold.timer = setTimeout(() => void renewControllerHold(hold), delay);
}

async function renewControllerHold(hold) {
  if (activeHold !== hold || hold.released) return;
  if (!focused()) {
    void stopControllerHold();
    return;
  }
  const started = performance.now();
  try {
    const response = await getJson("/api/controller-hold", jsonRequest({
      action: "renew", session: hold.session,
    }, AbortSignal.timeout(425)));
    if (activeHold !== hold || hold.released) return;
    if (response.phase !== "holding") {
      void stopControllerHold(response.message || "Контроллер прекратил удержание.");
      return;
    }
    scheduleHoldRenew(hold, Math.max(0, 150 - (performance.now() - started)));
  } catch (error) {
    if (activeHold === hold && !hold.released) void stopControllerHold(messageFrom(error));
  }
}

function interruptControllerHold() {
  if (activeHold) void stopControllerHold();
}

async function stopControllerHold(reason = "") {
  const hold = activeHold;
  if (!hold || hold.released) return;
  hold.released = true;
  clearTimeout(hold.timer);
  const request = getJson("/api/controller-hold", {
    ...jsonRequest({ action: "stop", session: hold.session }, AbortSignal.timeout(2500)), keepalive: true,
  });
  const { pointerId } = hold.input;
  if (pointerId !== undefined && hold.button.hasPointerCapture(pointerId)) hold.button.releasePointerCapture(pointerId);
  if (reason) setMachineError(reason);
  renderControls();
  try {
    const response = await request;
    const stopped = ["released", "expired", "cancelled", "stopped"].includes(response.phase);
    const unverified = !stopped || (response.sent === true && response.positionStable !== true);
    if (reason || unverified || response.phase === "expired") {
      const verify = demoMode ? "Подтвердите остановку виртуального станка." : "Проверьте положение на штатном пульте.";
      const detail = response.message || (unverified ? verify
        : response.phase === "expired" ? "Удержание прервано." : "");
      setMachineError([reason, detail].filter(Boolean).join(" "));
    }
    if (unverified) stopNeedsVerification = true;
    applyPosition(response);
  } catch {
    stopNeedsVerification = true;
    setMachineError(demoMode ? "Ответ на остановку виртуального станка не получен."
      : "Ответ на остановку не получен. Проверьте станок на штатном пульте.");
  } finally {
    activeHold = null;
    liveEpoch++;
    renderControls();
    scheduleLiveRead(0);
  }
}

async function verifyStop() {
  if ($("verify-stop-button").disabled || !focused() || !needsMotionVerification()) return;
  const session = control;
  setPending("verify-stop");
  try {
    const response = await getJson("/api/control-session", jsonRequest({
      action: "verify-stop", controlToken: session.token, ready: true,
    }, AbortSignal.timeout(4000)));
    if (control !== session) return;
    applyPosition(response);
    if (needsMotionVerification()) setMachineError("Остановка пока не подтверждена.");
  } catch (error) {
    setMachineError(messageFrom(error));
  } finally {
    setPending(null);
  }
}

function holdKey(event) {
  if (event.key === " " || event.code === "Space") return "Space";
  return event.key === "Enter" ? "Enter" : "";
}

function rememberRunStatistics(run, plan) {
  trackedRun = { startedAt: run.startedAt, filename: run.filename, plan };
  try {
    sessionStorage.setItem(machineStorageKey("runStatistics"), JSON.stringify(trackedRun));
  } catch { /* Session storage is optional. */ }
}

async function runControllerOperation(action, target) {
  if (!operationAllowed(action)) return;
  if (action === "cancel-cut") {
    await stopControllerCut();
    return;
  }
  if (!focused()) return;
  const stopAtStart = stopRevision;
  const session = control;
  const focus = focusEpoch;
  const payload = { action, controlToken: control.token };
  const statistics = action === "cut" ? currentUpload.statistics : null;
  if (action === "cut") {
    payload.jobReceipt = currentUpload.receipt;
    payload.planId = currentUpload.planId;
    payload.jobVerified = $("job-verified").checked;
    if (managedCut) payload.cutId = managedCut.id;
  }
  if (action === "range" && managedCut) payload.cutId = managedCut.id;
  if (action === "acknowledge-run") payload.jobVerified = true;
  if (action === "move-to") {
    payload.x = target.x;
    payload.y = target.y;
  }
  setPending(action);
  try {
    // Stop/pause commands must never wait behind polling.
    if (materialOperations.has(action) || action === "move-to") {
      if (liveReading) await liveReading;
      if (control !== session || focusEpoch !== focus || !focused() || !operationAllowed(action, true)) {
        setMachineError("Условия управления изменились. Повторите команду на активном пульте.");
        return;
      }
    }
    const response = await getJson("/api/controller-operation", jsonRequest(payload, AbortSignal.timeout(8000)));
    if (stopAtStart !== stopRevision) return;
    if (action === "cut" && response.sent && response.run?.startedAt && statistics) {
      rememberRunStatistics(response.run, statistics);
    }
    applyJobStatus(response);
    renderClamp();
    renderVacuum();
    if (response.acknowledged || (response.sent && ["cut", "raise-clamp", "home", "set-zero"].includes(action))) clearCurrentUpload();
    renderRun();
    if (!response.sent && !response.acknowledged && !(action === "move-to" && coordinateMove?.state === "reached")) {
      setMachineError(response.message || "Контроллер отклонил команду.");
    }
  } catch (error) {
    if (stopAtStart !== stopRevision) return;
    const rejected = error.status >= 400 && error.status < 500;
    if (action === "cut") clearCurrentUpload();
    const message = rejected || demoMode ? messageFrom(error) : "Результат отправки неизвестен. Проверьте штатный пульт.";
    setMachineError(message);
  } finally {
    operationRefreshUntil = performance.now() + 3000;
    setPending(null);
  }
}

async function stopControllerCut() {
  stopPending = true;
  stopRevision++;
  clearTimeout(liveTimer);
  liveEpoch++;
  setMachineError();
  renderControls();
  const retry = demoMode ? "Повторите остановку." : "Используйте физический стоп.";
  try {
    const payload = { action: "cancel-cut", ...(controlActive() ? { controlToken: control.token } : {}) };
    const response = await getJson("/api/controller-operation", jsonRequest(payload, AbortSignal.timeout(5000)));
    applyJobStatus(response);
    if (response.sent) {
      clearCurrentUpload();
      if (response.run?.state === "cancelling") live.externalMotion = null;
    } else {
      setMachineError(response.message || `Команда остановки отклонена. ${retry}`);
    }
  } catch (error) {
    setMachineError(`${messageFrom(error)} ${retry}`);
  } finally {
    stopPending = false;
    liveEpoch++;
    operationRefreshUntil = performance.now() + 3000;
    renderLive();
    scheduleLiveRead(0);
  }
}

function closeUploadConfirmation() {
  uploadConfirmation = null;
  if (uploadConfirmDialog.open) uploadConfirmDialog.close();
}

function openMoveToDialog() {
  if (moveToDialog.open || !focused() || !operationAllowed("move-to")) return;
  for (const [axis, limit] of [["x", machine.width], ["y", machine.height]]) {
    const input = $("move-to-" + axis);
    input.max = String(limit);
    input.value = Number.isFinite(live.position?.[axis]) ? String(live.position[axis]) : "";
  }
  moveToDialog.showModal();
}

function submitMoveTo(event) {
  event.preventDefault();
  if (!moveToDialog.open || !focused() || !operationAllowed("move-to")) return;
  const x = $("move-to-x").valueAsNumber;
  const y = $("move-to-y").valueAsNumber;
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > machine.width || y < 0 || y > machine.height) return;
  moveToDialog.close();
  void runControllerOperation("move-to", { x, y });
}

function matchesLaunchIntent(intent) {
  if (!intent || !focused() || focusEpoch !== intent.focus || stopRevision !== intent.stopRevision
      || control?.token !== intent.token || cutPlan?.planId !== intent.planId) return false;
  if (intent.upload) return currentUpload === intent.upload && operationAllowed("cut");
  return uploadAllowed() && uploadPageNumber() === intent.page
    && needsManualAutoRunConfirmation() === intent.checkAutoRun
    && needsPagePositionConfirmation(intent.page) === intent.checkZero;
}

function requestControllerLaunch() {
  if (!focused() || uploadConfirmDialog.open || !(currentUpload ? operationAllowed("cut") : uploadAllowed())) return;
  cancelCanvasGesture();
  const page = currentUpload?.page ?? uploadPageNumber();
  const intent = {
    planId: cutPlan.planId, page, token: control.token, focus: focusEpoch, stopRevision, upload: currentUpload,
    checkAutoRun: !currentUpload && needsManualAutoRunConfirmation(),
    checkZero: !currentUpload && needsPagePositionConfirmation(page),
  };
  uploadConfirmation = intent;
  const job = cutPlan.mode === "roll" ? `проход ${page} из ${cutPlan.pages.length}` : "раскрой";
  const mode = demoMode ? " в деморежиме" : "";
  $("upload-confirm-target").textContent = intent.upload
    ? `Запустить ${job}${mode}.` : `Загрузить и запустить ${job}${mode}.`;
  $("upload-confirm-checks").hidden = demoMode && !intent.checkAutoRun && !intent.checkZero;
  $("upload-confirm-placement").hidden = demoMode;
  $("upload-confirm-auto-run").hidden = !intent.checkAutoRun;
  $("upload-confirm-zero").hidden = !intent.checkZero;
  const target = demoMode ? "виртуального станка" : "станка";
  const feed = controllerRoll?.state === "advance" ? cutPlan.pages[page - 2]?.feedAfterMm : null;
  const showFeed = intent.checkZero && Number.isFinite(feed) && feed > 0;
  $("upload-confirm-feed").hidden = !showFeed;
  if (showFeed) {
    const feedAction = demoMode ? "Имитируйте протяжку полотна на" : "Протяните полотно на";
    $("upload-confirm-feed-text").textContent = `${feedAction} ${formatFeedDistance.format(feed)} мм.`;
  }
  $("upload-confirm-zero-text").textContent = `Совместите начало прохода ${page} с нулём ${target}.`;
  $("upload-confirm-seam").hidden = !intent.checkZero || !cutPlan.pages[page - 1]?.incomingSeams?.length;
  $("upload-confirm-accept").disabled = false;
  uploadConfirmDialog.showModal();
}

async function confirmControllerLaunch(intent) {
  if (!matchesLaunchIntent(intent)) return;
  const upload = intent.upload ?? await uploadControllerDrawing(intent);
  if (!upload) return;
  if (!matchesLaunchIntent({ ...intent, upload })) {
    if (currentUpload === upload) setMachineError("Условия запуска изменились. Нажмите «Старт», чтобы повторить.");
    return;
  }
  await runControllerOperation("cut");
}

async function uploadControllerDrawing(intent) {
  const plan = cutPlan;
  const { page, focus, planId, token, checkAutoRun, checkZero } = intent;
  const payload = {
    planId, controlToken: token, page,
    ...(managedCut ? { cutId: managedCut.id } : {}),
    autoRunOff: checkAutoRun,
    advanceConfirmed: checkZero,
  };
  clearCurrentUpload();
  setPending("upload");
  try {
    if (!await persistWorkspace() || !controlActive() || control?.token !== payload.controlToken || !focused()
        || focusEpoch !== focus || stopRevision !== intent.stopRevision
        || cutPlan?.planId !== payload.planId || uploadPageNumber(plan) !== page) {
      setMachineError("Сохраните раскладку и проверьте сеанс управления.");
      return;
    }
    const response = await getJson("/api/controller-upload", jsonRequest(payload, AbortSignal.timeout(120_000)));
    const uploaded = response.uploaded === true && typeof response.jobReceipt === "string" && response.jobReceipt
      && Number.isInteger(response.indexLimit) && response.indexLimit > 0
      && typeof response.verificationRequired === "boolean"
      && response.planId === payload.planId;
    applyJobStatus(response);
    uploadUncertain = uploaded ? false : response.uploadUncertain !== false || response.uploaded === true;
    if (uploaded && controlActive() && control.token === payload.controlToken) {
      currentUpload = {
        receipt: response.jobReceipt, indexLimit: response.indexLimit, planId: payload.planId,
        verificationRequired: response.verificationRequired,
        page, statistics: snapshotRunPlan(plan, response.indexLimit, page),
      };
      return currentUpload;
    } else if (uploaded) {
      setMachineError("Сеанс закончился. Включите управление и загрузите текущий чертёж заново.");
    } else {
      setMachineError(response.uploaded !== true && response.message
        || "Передача текущего файла не подтверждена. Загрузите его заново.");
    }
  } catch (error) {
    const rejected = error.status >= 400 && error.status < 500;
    if (!rejected) uploadUncertain = true;
    setMachineError(rejected || demoMode ? messageFrom(error) : "Результат передачи неизвестен. Проверьте задание на пульте.");
  } finally {
    renderRun();
    setPending(null);
  }
}

async function advanceControllerRoll({ automatic = false } = {}) {
  if (!focused() || !(automatic ? automaticRollPositioningAllowed()
    : waitingForAutomaticAdvance() && uploadAllowed({ forAdvance: true }))) return;
  if (automatic) rollPositioningAttempted = true;
  const { planId, nextPage: page } = controllerRoll;
  const payload = { action: "advance-roll", planId, page, controlToken: control.token,
    ...(managedCut ? { cutId: managedCut.id } : {}), ...(automatic ? { automatic: true } : {}) };
  setPending("advance-roll");
  try {
    const result = await getJson("/api/controller-operation", jsonRequest(payload, AbortSignal.timeout(8000)));
    const matching = result.roll?.planId === planId && result.roll.nextPage === page;
    if (automatic) {
      if (matching) applyPosition(result);
    } else {
      if (!matching || result.roll.materialAdvanced !== true) throw new Error("Протяжка не подтверждена.");
      applyJobStatus(result);
    }
  } catch (error) {
    setMachineError(messageFrom(error));
  } finally {
    setPending(null);
  }
}

function canResetControllerRoll() {
  const button = $("roll-reset");
  return Boolean(controllerRoll && !button.hidden && !button.disabled && controlActive() && focused());
}

function openRollResetDialog() {
  if (!canResetControllerRoll() || rollResetDialog.open) return;
  const abandoning = cutPlan?.planId === controllerRoll.planId && cutPlan.hasContinuations && controllerRoll.state !== "completed";
  const title = $("roll-reset").textContent;
  updateText($("roll-reset-title"), `${title}?`);
  updateText($("roll-reset-confirm"), title === "Сменить начальный проход" ? "Сменить проход" : "Начать заново");
  updateControl($("roll-reset-continuation"), "hidden", !abandoning);
  updateControl($("roll-reset-stop"), "hidden", demoMode);
  rollResetIntent = {
    roll: controllerRoll,
    controlToken: control.token,
    focus: focusEpoch,
    startedAt: cutProgress?.startedAt,
  };
  rollResetDialog.returnValue = "";
  rollResetDialog.showModal();
}

async function resetControllerRoll(intent) {
  if (!intent) return;
  const { planId, state, page, completedPages } = intent.roll;
  const { controlToken } = intent;
  if (!canResetControllerRoll() || intent.focus !== focusEpoch || control.token !== controlToken
      || controllerRoll.planId !== planId || controllerRoll.state !== state
      || controllerRoll.page !== page || controllerRoll.completedPages !== completedPages
      || cutProgress?.startedAt !== intent.startedAt) {
    setMachineError("Состояние раскроя изменилось. Откройте подтверждение снова.");
    return;
  }
  setPending("roll-reset");
  try {
    const response = await getJson("/api/roll-reset",
      jsonRequest({ controlToken, planId, stopped: true }, AbortSignal.timeout(8000)));
    applyJobStatus(response);
    clearCurrentUpload();
    resetTransferChecklist();
    selectedCutPage = 1;
  } catch (error) {
    setMachineError(messageFrom(error));
  } finally {
    setPending(null);
  }
}

function openNewCutDialog() {
  if (!canStartNewCut()) return;
  newCutDialog.returnValue = "";
  newCutDialog.showModal();
}

async function clearForNewCut() {
  if (!canStartNewCut()) return;
  const controlToken = controlActive() ? control.token : null;
  clearTimeout(saveTimer);
  setPending("new-cut");
  try {
    if (savePromise && !await savePromise) throw new Error("Не удалось сохранить текущие правки перед очисткой.");
    if (!demoMode && centralMode && companionConnected) {
      const local = await companionFetch("/api/new-cut", jsonRequest({ stopped: true }, AbortSignal.timeout(6000)));
      if (!local.ok) throw new Error("Помощник не подтвердил остановку станка. Проверьте состояние на пульте.");
    }
    const response = await getJson("/api/new-cut", jsonRequest({
      revision: workspaceRevision, stopped: true, ...(controlToken ? { controlToken } : {}),
    }, AbortSignal.timeout(12_000)));
    workspaceRevision = response.revision;
    workspaceSaved = workspaceChange;
    source = null;
    placed = [];
    excluded = new Set();
    selection = new Set();
    drawingUndo.length = 0;
    drawingRedo.length = 0;
    routeApplied = true;
    nestingSettings = null;
    nestBlocking = [];
    $("optimize-join").checked = true;
    cameraMode = "work";
    selectedCutPage = 1;
    pagePickerPlan = null;
    transferChecklistPlanId = null;
    resetTransferChecklist();
    clearCurrentUpload();
    applyJobStatus(response);
    activeRun = null;
    uploadUncertain = false;
    trackedRun = null;
    restoredRunStatistics = null;
    try { sessionStorage.removeItem(machineStorageKey("runStatistics")); } catch { /* Session storage is optional. */ }
    setSaveError();
    updateDrawing({ edited: false });
    renderRun();
  } catch (error) {
    setMachineError(messageFrom(error));
    if (workspaceRevision !== null && workspaceSaved !== workspaceChange) {
      saveTimer = setTimeout(() => void persistWorkspace(), 250);
    }
    return;
  } finally {
    setPending(null);
  }
  $("canvas-import").focus({ preventScroll: true });
}

async function prepareXiaoDrawing(openInXiao) {
  if (cutWidthPending() || xiaoHandoff || jobError || drawingLocked() || cutPlacementPreviewActive() || !cutPlan?.canPrepareXiao
      || (openInXiao && $("prepare-xiao-open").disabled)) return;
  const { planId, exportPlanId, filename } = cutPlan;
  const downloadName = openInXiao ? null : xiaoDownloadName(filename);
  xiaoHandoff = openInXiao ? "open" : "download";
  xiaoResult = null;
  renderControls();
  try {
    if (!await persistWorkspace() || cutPlacementPreviewActive() || cutPlan?.planId !== planId) {
      throw new Error("Дождитесь сохранения раскладки и повторите попытку.");
    }
    const payload = { planId, openInXiao, ...(exportPlanId && !openInXiao ? { exportPlanId } : {}),
      ...(managedCut ? { cutId: managedCut.id } : {}) };
    const response = await checkedFetch("/api/prepare-xiao", jsonRequest(payload, AbortSignal.timeout(30_000)), openInXiao);
    if (openInXiao) {
      const result = await response.json();
      xiaoResult = result.opened
        ? "Проверьте в Xiao размеры, рабочий ноль, SP4 и режим разбиения. Отправка и запуск выполняются в Xiao."
        : result.message || "Откройте сохранённый DXF в Xiao вручную.";
    } else {
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = downloadName;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      xiaoResult = demoMode ? "DXF скачан."
        : "DXF подготовлен. Откройте его в Xiao и проверьте размеры, рабочий ноль и SP4.";
    }
  } catch (error) {
    xiaoResult = messageFrom(error);
  } finally {
    xiaoHandoff = null;
    renderControls();
  }
}

async function playSimulation() {
  if ($("simulation-button").disabled) return;
  cancelCanvasGesture();
  const plan = await requestCutPlan();
  if ($("simulation-button").disabled || !plan?.pages.length) return;
  try {
    simulation.start(plan, canvasSection()?.page.number ?? 1);
  } catch (error) {
    notifyError(messageFrom(error));
  }
}

for (const id of placementFields) {
  $(id).addEventListener("change", () => applyPlacement(id));
  $(id).addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    event.currentTarget.blur();
  });
}
$("rotate-button").addEventListener("click", () => {
  rotateSelectionBy(90);
});
$("exclude-button").addEventListener("click", toggleSelectionExcluded);
$("delete-button").addEventListener("click", () => {
  deleteSelection();
  contourDialog.close();
  svg.focus({ preventScroll: true });
});
const contourDialog = $("contour-dialog");
const nestDialog = $("nest-dialog");
const optimizeDialog = $("optimize-dialog");
let contourJoinPreview = null;
let optimizationJoined = false;
$("contour-close").addEventListener("click", () => contourDialog.close());
$("contour-check-button").addEventListener("click", () => {
  if (!contourCheckAvailable()) return;
  contourCheckDialog.showModal();
  void scanDrawingContours();
});
$("contour-check-scan").addEventListener("click", () => void scanDrawingContours());
$("contour-check-apply").addEventListener("click", applyContourRepair);
$("contour-check-close").addEventListener("click", () => contourCheckDialog.close());
contourCheckDialog.addEventListener("close", () => {
  if (contourCheckDialog.open) return;
  const request = contourRepair;
  contourRepair = null;
  contourRepairProposal = null;
  request?.abort();
  renderControls();
  if (!$("contour-check-button").disabled) $("contour-check-button").focus({ preventScroll: true });
});
$("nest-button").addEventListener("click", () => {
  cancelCanvasGesture();
  nestSelection = new Set(selection);
  const scope = nestSelection.size
    ? "В раскрой попадут только выделенные детали. Остальные останутся на чертеже как исключённые."
    : "В раскрой попадут все включённые детали.";
  const pendingWidth = cutWidthPending();
  $("nest-scope").textContent = pendingWidth ? `Чтобы сохранить ширину полотна, разложите все детали. ${scope}` : scope;
  applyNestingSettings();
  nestDialog.showModal();
  if (pendingWidth && $("nest-width").required) $("nest-width").focus();
  void checkNestingBackend();
});
$("nest-cancel").addEventListener("click", () => {
  nestRequest?.abort();
  nestDialog.close();
});
nestDialog.addEventListener("cancel", () => nestRequest?.abort());
function openOptimization() {
  if (optimizeDialog.open) return;
  cancelCanvasGesture();
  optimizationJoined = false;
  contourJoinPreview = null;
  $("optimize-route").checked = routeApplied;
  optimizeDialog.showModal();
  updateOptimizationControls();
}
$("optimize-button").addEventListener("click", openOptimization);
$("optimize-cancel").addEventListener("click", () => optimizeDialog.close());
$("optimize-dismiss").addEventListener("click", () => optimizeDialog.close());
optimizeDialog.addEventListener("close", () => {
  if (!optimizeDialog.open) contourJoinPreview = null;
});
$("optimize-route").addEventListener("change", updateOptimizationControls);
$("optimize-join").addEventListener("change", updateOptimizationControls);

function previewContourJoin() {
  if (kitLayout() || !$("optimize-join").checked) return null;
  if (contourJoinPreview?.epoch !== cutPlanEpoch) {
    contourJoinPreview = { epoch: cutPlanEpoch, result: joinTouchingContours(included()) };
  }
  return contourJoinPreview.result;
}

function renderRouteComparison(outdated, join) {
  const result = cutPlan?.routeOptimization;
  const current = cutPlan?.travelDistanceMm;
  const status = $("optimize-result");
  const comparison = $("optimize-comparison");
  const routeChanged = $("optimize-route").checked !== routeApplied;
  const hasComparison = !outdated && routeApplied
    && Number.isFinite(result?.beforeMm) && Number.isFinite(current);
  updateControl(comparison, "hidden", !hasComparison);
  let message = "";
  if (outdated) message = "Перезапустите локальную программу плоттера для расчёта маршрута.";
  else if (cutPlanError && !hasComparison) message = "Сравнение недоступно. Проверьте геометрию раскладки.";
  else if (!cutPlan) message = "Рассчитываем маршрут…";
  else if (routeChanged) message = routeApplied ? "После применения вернётся исходный порядок реза."
    : "После применения пересчитаем порядок реза.";
  else if (!routeApplied) {
    message = "Сокращение холостого хода выключено.";
    if (Number.isFinite(current)) message += ` Текущий холостой ход — ${number(current / 1000)} м.`;
  }
  else if (hasComparison) message = result.beforeMm - current > 1e-7
    ? "Оптимизированный маршрут уже применяется." : "Сокращение холостого хода для этой раскладки не найдено.";
  else message = "Нет данных для сравнения маршрутов.";
  const joinMessage = join?.skipped ? "Слишком много контуров для проверки всех стыков."
    : join?.joined ? "Есть линии, которые можно соединить."
    : optimizationJoined ? "Линии в точных стыках соединены."
    : join ? "Точных стыков для соединения не найдено." : "";
  updateText(status, [message, joinMessage].filter(Boolean).join(" "));
  updateControl(status, "hidden", !status.textContent);
  if (hasComparison) {
    const saved = result.beforeMm > 0 ? Math.max(0, 100 * (result.beforeMm - current) / result.beforeMm) : 0;
    updateText($("optimize-before"), `${number(result.beforeMm / 1000)} м`);
    updateText($("optimize-after"), `${number(current / 1000)} м`);
    updateText($("optimize-saved"), `${number(Math.round(saved * 10) / 10)} %`);
  }
}

function updateOptimizationControls() {
  const locked = drawingLocked();
  const outdated = Number.isInteger(machine.apiVersion) && machine.apiVersion < 14;
  if (optimizeDialog.open) {
    updateControl($("optimize-join"), "disabled", kitLayout());
    const join = locked ? null : previewContourJoin();
    const routeChanged = !outdated && $("optimize-route").checked !== routeApplied;
    const canApply = !locked && placed.length > excluded.size && (routeChanged || Boolean(join?.joined));
    updateControl($("optimize-apply"), "disabled", !canApply);
    updateText($("optimize-dismiss"), canApply ? "Отмена" : "Закрыть");
    renderRouteComparison(outdated, join);
  }
  if (nestDialog.open) {
    const canApply = !locked && nestingBackend === "ready" && hasNestingPaths();
    updateControl($("nest-apply"), "disabled", !canApply);
    updateText($("nest-apply"), nestRequest ? "Рассчитываем…" : nestSelection.size ? "Разложить выделенное" : "Разложить на полотне");
  }
  const closeLabel = nestRequest ? "Отменить расчёт" : "Закрыть";
  $("nest-cancel").title = closeLabel;
  $("nest-cancel").setAttribute("aria-label", closeLabel);
  for (const id of ["nest-width", "nest-length", "nest-gap", "nest-margin", "nest-rotation", "optimize-route", "optimize-join"]) {
    $(id).disabled = locked;
  }
  $("optimize-route").disabled ||= outdated;
  $("nest-show-blocking").disabled = locked;
  $("nest-try-rotation").disabled = locked;
}
$("optimize-apply").addEventListener("click", () => {
  if (!optimizeDialog.open || $("optimize-apply").disabled) return;
  applyContourOptimization();
  if ($("optimize-apply").disabled) $("optimize-dismiss").focus({ preventScroll: true });
});
$("nest-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void applyNesting();
});
$("nest-form").addEventListener("input", () => {
  if (nestingBackend === "ready") setNestStatus("");
});
$("nest-recheck").addEventListener("click", () => void checkNestingBackend());
$("nest-try-rotation").addEventListener("click", () => {
  $("nest-rotation").value = "any";
  void applyNesting();
});
$("nest-show-blocking").addEventListener("click", () => {
  if (drawingLocked() || !nestBlocking.length) return;
  setMeasureMode(false);
  setSelection(new Set(nestBlocking));
  nestDialog.close();
  svg.focus({ preventScroll: true });
});
$("undo-tool").addEventListener("click", () => stepEditHistory());
$("redo-tool").addEventListener("click", () => stepEditHistory(true));
$("canvas-import").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => void loadDrawing(event.target.files[0]));
$("toast-close").addEventListener("click", () => { $("toast").hidden = true; });
$("new-cut-button").addEventListener("click", openNewCutDialog);
$("kit-layout-save").addEventListener("click", () => void saveKitLayout());
$("app-update").addEventListener("click", () => void applyAppUpdate());
$("release-notes-button").addEventListener("click", () => void openReleaseNotes());
$("release-notes-dialog").addEventListener("close", () => {
  if ($("release-notes-dialog").open) return;
  releaseNotesRequest?.abort();
  releaseNotesRequest = null;
  $("release-notes-button").focus({ preventScroll: true });
});
newCutDialog.addEventListener("close", () => {
  if (newCutDialog.returnValue === "confirm") void clearForNewCut();
});
demoSwitch.addEventListener("click", () => { if (!managedCut) void toggleDemoMode(); });
$("arm-button").addEventListener("click", () => void armControl());
$("connect-companion").addEventListener("click", () => {
  if (location.protocol === "http:") window.open(companionAddress + "/", "_blank", "noopener");
  else void connectCompanion();
});
$("verify-stop-button").addEventListener("click", () => void verifyStop());
$("simulation-button").addEventListener("click", () => void playSimulation());
$("run-acknowledge").addEventListener("click", () => void runControllerOperation("acknowledge-run"));
$("job-page").addEventListener("change", (event) => {
  selectedCutPage = Number(event.currentTarget.value);
  previewCutPage = null;
  resetTransferChecklist();
  renderControls();
});
$("launch-next-pass").addEventListener("click", () => {
  if (canShowNextLaunchPass()) navigateCanvasSection(1);
});
$("manual-auto-run-off").addEventListener("change", renderControls);
$("roll-reset").addEventListener("click", openRollResetDialog);
rollResetDialog.addEventListener("close", () => {
  const intent = rollResetIntent;
  rollResetIntent = null;
  if (rollResetDialog.returnValue === "confirm") void resetControllerRoll(intent);
});
$("controller-upload-button").addEventListener("click", requestControllerLaunch);
$("roll-advance-button").addEventListener("click", () => void advanceControllerRoll());
$("move-to-button").addEventListener("click", openMoveToDialog);
$("move-to-form").addEventListener("submit", submitMoveTo);
$("move-to-cancel").addEventListener("click", () => moveToDialog.close());
$("upload-confirm-cancel").addEventListener("click", closeUploadConfirmation);
uploadConfirmDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeUploadConfirmation();
});
$("upload-confirm-accept").addEventListener("click", () => {
  const intent = uploadConfirmation;
  closeUploadConfirmation();
  void confirmControllerLaunch(intent);
});
$("prepare-xiao-open").addEventListener("click", () => void prepareXiaoDrawing(true));
$("prepare-xiao-download").addEventListener("click", () => void prepareXiaoDrawing(false));
for (const button of operationButtons) {
  button.addEventListener("click", () => void runControllerOperation(button.dataset.operation));
}

for (const button of holdButtons) {
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    void startControllerHold(button, { pointerId: event.pointerId });
  });
  button.addEventListener("keydown", (event) => {
    const key = holdKey(event);
    if (!key || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    if (!event.repeat) void startControllerHold(button, { key });
  });
  button.addEventListener("lostpointercapture", (event) => {
    if (activeHold && activeHold.input.pointerId === event.pointerId && !activeHold.released) interruptControllerHold();
  });
  button.addEventListener("blur", () => {
    if (activeHold?.button === button && activeHold.input.key && !activeHold.released) interruptControllerHold();
  });
  button.addEventListener("contextmenu", (event) => event.preventDefault());
}
document.addEventListener("pointerup", (event) => {
  if (activeHold?.input.pointerId === event.pointerId) void stopControllerHold();
}, true);
document.addEventListener("pointercancel", (event) => {
  if (activeHold?.input.pointerId === event.pointerId) interruptControllerHold();
}, true);
document.addEventListener("pointermove", (event) => {
  if (event.pointerType === "mouse" && event.buttons === 0 && activeHold?.input.pointerId === event.pointerId) interruptControllerHold();
}, true);
document.addEventListener("keyup", (event) => {
  updateMeasurementModifiers(event);
  const key = holdKey(event);
  if (key && activeHold?.input.key === key) {
    event.preventDefault();
    void stopControllerHold();
  }
}, true);
function loseFocus() {
  measurementCursor = null;
  measurementHover = null;
  if (measurement) {
    measurement.lockedCoordinate = null;
    measurement.end = null;
  }
  if (measureMode) renderMeasurement();
  focusEpoch++;
  liveEpoch++;
  closeUploadConfirmation();
  if (moveToDialog.open) moveToDialog.close();
  if (rollResetDialog.open) rollResetDialog.close("cancel");
  live.positionFresh = false;
  interruptControllerHold();
  renderLive();
}
window.addEventListener("blur", loseFocus);
window.addEventListener("pagehide", () => {
  focusEpoch++;
  disarmControl();
});
window.addEventListener("beforeunload", (event) => {
  if (workspaceChange !== workspaceSaved) {
    if (!kitLayout()) void persistWorkspace();
    event.preventDefault();
    event.returnValue = "";
  }
});
function resumeControlPolling() {
  scheduleLiveRead(0);
  if (control && machine.persistentControl === true) void renewControl();
}
window.addEventListener("focus", resumeControlPolling);
window.addEventListener("focus", () => void readAppUpdate());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") resumeControlPolling();
  else {
    clearTimeout(liveTimer);
    loseFocus();
  }
});

$("select-tool").addEventListener("click", () => setMeasureMode(false));
$("measure-tool").addEventListener("click", () => setMeasureMode(!measureMode));
$("measurement-close").addEventListener("click", () => {
  setMeasureMode(false);
  svg.focus({ preventScroll: true });
});
$("measurement-clear-all").addEventListener("click", removeAllMeasurements);
$("measurement-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-measurement]");
  if (button) removeMeasurement(button);
});
$("fit-tool").addEventListener("click", () => fitCamera("overview"));
$("section-focus").addEventListener("click", () => fitCamera());
$("section-prev").addEventListener("click", () => navigateCanvasSection(-1));
$("section-next").addEventListener("click", () => navigateCanvasSection(1));
const pageAnnotations = $("page-annotation-layer");
pageAnnotations.addEventListener("click", (event) => {
  const tag = event.target.closest("[data-canvas-page]");
  if (tag) openCanvasSection(Number(tag.dataset.canvasPage));
});
pageAnnotations.addEventListener("keydown", (event) => {
  const tag = event.target.closest("[data-canvas-page]");
  if (!tag || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  if (!event.repeat && openCanvasSection(Number(tag.dataset.canvasPage))) {
    $("section-focus").focus({ preventScroll: true });
  }
});
svg.addEventListener("wheel", (event) => {
  event.preventDefault();
  if (!event.deltaY) return;
  const [x, y] = screenFraction(event);
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? svg.clientHeight : 1;
  const delta = Math.max(-160, Math.min(160, event.deltaY * unit));
  zoom(Math.exp(-delta * 0.0014), x, y, true);
}, { passive: false });
function boxFromPoints([x1, y1], [x2, y2]) {
  return { minX: Math.min(x1, x2), minY: Math.min(y1, y2), maxX: Math.max(x1, x2), maxY: Math.max(y1, y2) };
}

function unionPlacementBoxes(a, b) {
  return { minX: Math.min(a.minX, b.minX), minY: Math.min(a.minY, b.minY),
    maxX: Math.max(a.maxX, b.maxX), maxY: Math.max(a.maxY, b.maxY) };
}

function boxContains(outer, inner) {
  return outer.minX <= inner.minX && outer.maxX >= inner.maxX
    && outer.minY <= inner.minY && outer.maxY >= inner.maxY;
}

const placementEpsilon = 1e-6;

function manualOutlines(paths) {
  const outlines = paths.filter((path) => path.closed);
  const nodes = new Map();
  const unique = new Set();
  const records = [];
  for (const path of paths) {
    if (path.closed) continue;
    const forward = path.points.join(";");
    const backward = [...path.points].reverse().join(";");
    const key = forward < backward ? forward : backward;
    if (unique.has(key)) continue;
    unique.add(key);
    const record = { path, ends: [] };
    for (const [side, point] of [path.points[0], path.points.at(-1)].entries()) {
      const endpoint = point.map((coordinate) => Math.round(coordinate * 1000)).join(",");
      if (!nodes.has(endpoint)) nodes.set(endpoint, { links: [] });
      const node = nodes.get(endpoint);
      node.links.push({ record, side });
      record.ends.push(node);
    }
    if (record.ends[0] === record.ends[1] && path.points.length >= 4) {
      outlines.push(path);
      for (const node of record.ends) node.links = node.links.filter((link) => link.record !== record);
      continue;
    }
    records.push(record);
  }
  // A dangling cut is part of the clearance check but not part of a closed outline.
  const leaves = [...nodes.values()].filter((node) => node.links.length === 1);
  for (const node of leaves) {
    if (node.links.length !== 1) continue;
    const { record } = node.links[0];
    for (const end of record.ends) {
      end.links = end.links.filter((link) => link.record !== record);
      if (end !== node && end.links.length === 1) leaves.push(end);
    }
    record.ends = null;
  }
  for (const component of joinComponents(records)) {
    if (!component.closed) continue;
    const outline = combineContourChain(component);
    if (outline) outlines.push(outline);
  }
  return outlines.map((path) => ({ points: path.points, box: bounds([path]) }));
}

function prepareManualPlacement(selected = selection) {
  const moving = [];
  const fixed = [];
  for (const [index, path] of placed.entries()) {
    const part = drawingParts.get(path.partNumber);
    if (part && index !== part.indices[0]) continue;
    const indices = (part?.indices ?? [index]).filter((member) => !excluded.has(member));
    if (!indices.length) continue;
    const paths = indices.map((member) => placed[member]);
    const shape = { key: partProgressKey(index), indices, paths, box: part?.activeBox ?? pathBoxes[index],
      outlineBox: bounds(paths.filter((path) => !path.notch)) };
    (selected.has(index) ? moving : fixed).push(shape);
  }
  return { moving, fixed, limits: materialLimits(), gap: Math.max(nestingSettings?.gapMm ?? 0, 0.002) };
}

function placementBoxesNear(a, b, distance) {
  return a.minX <= b.maxX + distance && b.minX <= a.maxX + distance
    && a.minY <= b.maxY + distance && b.minY <= a.maxY + distance;
}

function eachPlacementSegment(paths, visit) {
  for (const { points, closed, notch } of paths) {
    for (let i = closed ? 0 : 1; i < points.length; i++) {
      const first = points.at(i - 1), last = points[i];
      if (visit(first, last, boxFromPoints(first, last), notch) === false) return false;
    }
  }
  return true;
}

function placementSegmentTree(segments) {
  const box = {
    minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity,
  };
  for (const segment of segments) {
    box.minX = Math.min(box.minX, segment.box.minX);
    box.minY = Math.min(box.minY, segment.box.minY);
    box.maxX = Math.max(box.maxX, segment.box.maxX);
    box.maxY = Math.max(box.maxY, segment.box.maxY);
  }
  if (segments.length <= 12) return { box, segments };
  const [min, max] = box.maxX - box.minX >= box.maxY - box.minY ? ["minX", "maxX"] : ["minY", "maxY"];
  segments.sort((a, b) => a.box[min] + a.box[max] - b.box[min] - b.box[max]);
  const half = Math.floor(segments.length / 2);
  return { box, left: placementSegmentTree(segments.slice(0, half)), right: placementSegmentTree(segments.slice(half)) };
}

function visitPlacementSegments(tree, box, distance, visit) {
  if (!placementBoxesNear(tree.box, box, distance)) return true;
  if (tree.segments) return tree.segments.every((segment) => !placementBoxesNear(segment.box, box, distance) || visit(segment));
  return visitPlacementSegments(tree.left, box, distance, visit) && visitPlacementSegments(tree.right, box, distance, visit);
}

function pointInPlacementOutline([x, y], { box, points }) {
  if (x < box.minX || x > box.maxX || y < box.minY || y > box.maxY) return false;
  let inside = false;
  for (let i = 0; i < points.length; i++) {
    const [ax, ay] = points.at(i - 1);
    const [bx, by] = points[i];
    if ((ay > y) !== (by > y) && x < ax + (bx - ax) * (y - ay) / (by - ay)) inside = !inside;
  }
  return inside;
}

function placementCross([ax, ay], [bx, by], [x, y]) {
  return (bx - ax) * (y - ay) - (by - ay) * (x - ax);
}

function placementPointOffset([x, y], [ax, ay], [bx, by]) {
  const dx = bx - ax, dy = by - ay;
  const length = dx * dx + dy * dy;
  const fraction = length ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length)) : 0;
  return [x - (ax + dx * fraction), y - (ay + dy * fraction)];
}

function placementSegmentOffset(a, b, c, d) {
  if (placementCross(a, b, c) * placementCross(a, b, d) < 0
    && placementCross(c, d, a) * placementCross(c, d, b) < 0) return [0, 0, 0];
  const offsets = [placementPointOffset(a, c, d), placementPointOffset(b, c, d)];
  for (const point of [c, d]) {
    const [x, y] = placementPointOffset(point, a, b);
    offsets.push([-x, -y]);
  }
  let best = [0, 0, Infinity];
  for (const [x, y] of offsets) {
    const square = x * x + y * y;
    if (square < best[2]) best = [x, y, square];
  }
  return best;
}

function placementTree(part) {
  if (!part.tree) {
    const segments = [];
    eachPlacementSegment(part.paths, (first, last, box, notch) => segments.push({ first, last, box, notch }));
    part.tree = placementSegmentTree(segments);
  }
  return part.tree;
}

function placementPartsConflict(part, other, gap) {
  if (!placementBoxesNear(part.box, other.box, gap)) return false;
  const tree = placementTree(other);
  const clearance = (gap - placementEpsilon) ** 2;
  if (!eachPlacementSegment(part.paths, (a, b, edgeBox) => visitPlacementSegments(tree, edgeBox, gap,
    ({ first, last }) => placementSegmentOffset(a, b, first, last)[2] >= clearance))) return true;
  if (!placementBoxesNear(part.box, other.box, 0)) return false;
  part.outlines ??= manualOutlines(part.paths.filter((path) => !path.notch));
  other.outlines ??= manualOutlines(other.paths.filter((path) => !path.notch));
  return part.paths.some((path) => other.outlines.some((outline) => pointInPlacementOutline(path.points[0], outline)))
    || other.paths.some((path) => part.outlines.some((outline) => pointInPlacementOutline(path.points[0], outline)));
}

function refreshPlacementWarnings() {
  placementConflicts = new Set();
  if (drawingPartsReady) {
    const { fixed, gap } = prepareManualPlacement(new Set());
    fixed.sort((a, b) => a.box.minX - b.box.minX);
    const active = [];
    for (const part of fixed) {
      for (let i = active.length - 1; i >= 0; i--) {
        const other = active[i];
        if (other.box.maxX + gap < part.box.minX) active.splice(i, 1);
        else if ((!placementConflicts.has(part.key) || !placementConflicts.has(other.key))
          && placementPartsConflict(part, other, gap)) {
          placementConflicts.add(part.key);
          placementConflicts.add(other.key);
        }
      }
      active.push(part);
    }
  }
  jobError = outsideMaterial(includedBounds) ? "Красные детали выходят за границы рабочей области."
    : placementConflicts.size ? "Красные детали пересекаются или расположены слишком близко." : "";
}

function shiftPlacementBox(box, dx, dy) {
  return { minX: box.minX + dx, minY: box.minY + dy, maxX: box.maxX + dx, maxY: box.maxY + dy };
}

function manualPlacementConflicts(placement, transform, offset) {
  const conflicts = new Set();
  for (const part of placement.moving) {
    let paths, shape;
    const transformedPaths = () => paths ??= part.paths.map((path) => mapPath(path, transform));
    const box = offset ? shiftPlacementBox(part.box, ...offset) : bounds(transformedPaths());
    if (outsideMaterial(box, placement.limits) || placement.fixed.some((other) =>
      placementBoxesNear(box, other.box, placement.gap) && placementPartsConflict(
        shape ??= { paths: transformedPaths(), box }, other, placement.gap))) conflicts.add(part.key);
  }
  return conflicts;
}

function boundarySnap(placement, dx, dy, radius) {
  const snap = [null, null];
  const edges = [["minX", "maxX"], ["minY", "maxY"]];
  for (const part of placement.moving) {
    if (!part.outlineBox) continue;
    for (const [axis, offset] of [dx, dy].entries()) {
      for (const edge of edges[axis]) {
        const value = placement.limits[edge];
        const delta = value - (part.outlineBox[edge] + offset);
        if (Math.abs(delta) <= radius && (!snap[axis] || Math.abs(delta) < Math.abs(snap[axis].delta))) {
          snap[axis] = { axis, value, delta };
        }
      }
    }
  }
  return snap.filter(Boolean);
}

function contourSnap(placement, dx, dy, radius) {
  const candidates = [];
  const reach = placement.gap + radius;
  const minOverlap = Math.min(12, radius / 2);
  for (const part of placement.moving) {
    if (!part.outlineBox) continue;
    const box = shiftPlacementBox(part.outlineBox, dx, dy);
    const nearby = placement.fixed.filter((other) => other.outlineBox && placementBoxesNear(box, other.outlineBox, reach));
    if (!nearby.length) continue;
    eachPlacementSegment(part.paths, (first, last, edgeBox, notch) => {
      if (notch) return;
      const a = [first[0] + dx, first[1] + dy], b = [last[0] + dx, last[1] + dy];
      const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (length < placementEpsilon) return;
      const ux = (b[0] - a[0]) / length, uy = (b[1] - a[1]) / length;
      const edge = shiftPlacementBox(edgeBox, dx, dy);
      for (const other of nearby) {
        visitPlacementSegments(placementTree(other), edge, reach, ({ first: c, last: d, notch: fixedNotch }) => {
          if (fixedNotch) return true;
          const vx = d[0] - c[0], vy = d[1] - c[1];
          const span = Math.hypot(vx, vy);
          const parallel = Math.abs((ux * vx + uy * vy) / span);
          if (!span || parallel < Math.cos(Math.PI / 60)) return true;
          const project = ([x, y]) => (x - a[0]) * ux + (y - a[1]) * uy;
          const sc = project(c), sd = project(d);
          const from = Math.max(0, Math.min(sc, sd)), to = Math.min(length, Math.max(sc, sd));
          if (to - from < minOverlap) return true;
          const normal = ([x, y]) => (x - a[0]) * -uy + (y - a[1]) * ux;
          const nc = normal(c), nd = normal(d);
          const at = (s) => nc + (nd - nc) * (s - sc) / (sd - sc);
          const lo = at(from), hi = at(to);
          const side = Math.sign(lo + hi) || 1;
          const contact = side * lo <= side * hi ? from : to;
          const distance = at(contact);
          const delta = distance - side * placement.gap / parallel;
          const shift = Math.abs(delta);
          if (shift > radius || candidates.some(({ offset }) => Math.hypot(offset[0] + delta * uy, offset[1] - delta * ux) < 0.01)) return true;
          const score = shift - 0.02 * Math.min(to - from, radius * 3);
          if (candidates.length === 4 && score >= candidates.at(-1).score) return true;
          const point = (s, n) => [a[0] + s * ux - n * uy, a[1] + s * uy + n * ux];
          const pad = radius * 1.5;
          candidates.push({ score, offset: [-delta * uy, delta * ux],
            lines: [[point(from - pad, delta), point(to + pad, delta)], [point(from, lo), point(to, hi)]] });
          candidates.sort((a, b) => a.score - b.score);
          if (candidates.length > 4) candidates.pop();
          return true;
        });
      }
    });
  }
  return candidates;
}

function alignmentSnap(placement, dx, dy, radius) {
  const guides = [null, null];
  const edges = [["minX", "maxX"], ["minY", "maxY"]];
  for (const part of placement.moving) {
    if (!part.outlineBox) continue;
    const box = shiftPlacementBox(part.outlineBox, dx, dy);
    for (const other of placement.fixed) {
      const target = other.outlineBox;
      if (!target || !placementBoxesNear(box, target, Math.max(placement.gap + radius, radius * 8))) continue;
      for (const [axis, keys] of edges.entries()) {
        for (const key of keys) {
          const delta = target[key] - box[key];
          if (Math.abs(delta) <= radius && (!guides[axis] || Math.abs(delta) < Math.abs(guides[axis].delta))) {
            guides[axis] = { axis, value: target[key], delta };
          }
        }
      }
    }
  }
  return guides.filter(Boolean);
}

function previewManualMove(gesture, { dx, dy, free }) {
  const { placement } = gesture;
  const radius = Math.min(40, camera.width / svg.clientWidth * 11);
  const check = (x, y) => manualPlacementConflicts(placement, ([u, v]) => [u + x, v + y], [x, y]);
  gesture.guides = [];
  gesture.edgeGuide = null;
  const applyGuide = (guide) => {
    const x = dx + (guide.axis === 0 ? guide.delta : 0);
    const y = dy + (guide.axis === 1 ? guide.delta : 0);
    if (check(x, y).size) return;
    dx = x;
    dy = y;
    gesture.guides.push(guide);
  };
  if (!free) {
    for (const guide of boundarySnap(placement, dx, dy, radius)) applyGuide(guide);
    const shift = gesture.guides.map(({ axis }) => axis);
    for (const snap of contourSnap(placement, dx, dy, radius)) {
      if (shift.some((axis) => Math.abs(snap.offset[axis]) > placementEpsilon)) continue;
      const x = dx + snap.offset[0], y = dy + snap.offset[1];
      if (!check(x, y).size) {
        dx = x;
        dy = y;
        gesture.edgeGuide = snap.lines;
        break;
      }
    }
    if (!gesture.edgeGuide) for (const guide of alignmentSnap(placement, dx, dy, radius)) {
      if (!shift.includes(guide.axis)) applyGuide(guide);
    }
  }
  gesture.offset = [dx, dy];
  previewPlacementStatus(placement, check(dx, dy));
  previewMove(dx, dy);
  renderManualGuides();
}

function previewPlacementStatus(placement, conflicts) {
  const groups = $("path-layer").children;
  for (const part of placement.moving) {
    const invalid = conflicts.has(part.key);
    for (const index of part.indices) {
      groups[index].classList.toggle("move-invalid", invalid);
      groups[index].classList.toggle("move-valid", !invalid);
    }
  }
}

function segmentCrossesBox([x, y], [endX, endY], box) {
  const dx = endX - x;
  const dy = endY - y;
  let enter = 0;
  let exit = 1;
  for (const [direction, edge] of [[-dx, x - box.minX], [dx, box.maxX - x], [-dy, y - box.minY], [dy, box.maxY - y]]) {
    if (!direction && edge < 0) return false;
    if (direction < 0) enter = Math.max(enter, edge / direction);
    if (direction > 0) exit = Math.min(exit, edge / direction);
    if (enter > exit) return false;
  }
  return true;
}

function contourCrossesBox(index, box) {
  const outer = pathBoxes[index];
  if (outer.minX > box.maxX || outer.maxX < box.minX || outer.minY > box.maxY || outer.maxY < box.minY) return false;
  if (boxContains(box, outer)) return true;
  const { points, closed } = placed[index];
  for (let i = 1; i < points.length; i++) {
    if (segmentCrossesBox(points[i - 1], points[i], box)) return true;
  }
  return closed && segmentCrossesBox(points.at(-1), points[0], box);
}

function contourAt(event, [x, y]) {
  const badge = event.target.closest("[data-part-number]");
  if (badge) return drawingParts.get(Number(badge.dataset.partNumber))?.indices[0] ?? null;
  const hit = event.target.closest("[data-index]");
  if (hit) return Number(hit.dataset.index);
  // A click inside nested closed contours selects the smallest enclosing shape.
  let best = null;
  let area = Infinity;
  const point = new DOMPoint(...projectPoint(x, y));
  const groups = $("path-layer").children;
  for (const [index, path] of placed.entries()) {
    if (!path.closed) continue;
    const box = pathBoxes[index];
    if (x < box.minX || x > box.maxX || y < box.minY || y > box.maxY) continue;
    if (!groups[index].firstElementChild.isPointInFill(point)) continue;
    const [originX, originY] = path.points[0];
    let twiceArea = 0;
    for (let i = 2; i < path.points.length; i++) {
      const [aX, aY] = path.points[i - 1];
      const [bX, bY] = path.points[i];
      twiceArea += (aX - originX) * (bY - originY) - (aY - originY) * (bX - originX);
    }
    const candidate = Math.abs(twiceArea) / 2;
    if (candidate <= area) {
      area = candidate;
      best = index;
    }
  }
  return best ?? fragmentedPartAt([x, y])?.indices[0] ?? null;
}

function fragmentedPartAt([x, y]) {
  // Some CAD outlines consist of several open paths, so they have no SVG fill.
  let best;
  let area = Infinity;
  for (const part of drawingParts.values()) {
    if (part.indices.some((member) => placed[member].closed)) continue;
    const { minX, minY, maxX, maxY } = part.box;
    if (x < minX || x > maxX || y < minY || y > maxY) continue;
    const candidate = (maxX - minX) * (maxY - minY);
    if (candidate >= area || candidate <= 0) continue;
    let inside = false;
    for (const member of part.indices) {
      const { points, closed } = placed[member];
      for (let i = closed ? 0 : 1; i < points.length; i++) {
        const [ax, ay] = points.at(i - 1);
        const [bx, by] = points[i];
        if ((ax > x) !== (bx > x) && y < ay + (by - ay) * (x - ax) / (bx - ax)) inside = !inside;
      }
    }
    if (inside) {
      best = part;
      area = candidate;
    }
  }
  return best;
}

const partMenu = $("part-menu");
const partMenuButtons = [...partMenu.querySelectorAll("button")];
let lastPartClick = null;

function closePartMenu(restoreFocus = false) {
  if (partMenu.hidden) return;
  partMenu.hidden = true;
  menuPartNumber = null;
  if (restoreFocus) svg.focus({ preventScroll: true });
}

function openPartMenu(part, x, y) {
  if (!part || drawingLocked()) return;
  cancelCanvasGesture();
  endFreeRotation();
  menuPartNumber = part.number;
  updateText($("part-menu-title"), `Деталь №${part.number}`);
  const omit = part.indices.every((index) => excluded.has(index));
  const status = $("part-menu-status");
  updateText(status, omit ? "Исключена" : "В раскрое");
  status.classList.toggle("is-excluded", omit);
  const excludeAction = $("part-menu-exclude");
  $("part-menu-edit").hidden = !canCorrectLayoutPart();
  updateText(excludeAction.querySelector("span"), omit ? "Вернуть в раскрой" : "Исключить из раскроя");
  updateAttribute(excludeAction.querySelector("use"), "href", omit ? "#i-plus" : "#i-minus");
  for (const button of partMenu.querySelectorAll("[data-part-rotation]")) {
    button.setAttribute("aria-checked", String(button.dataset.partRotation === part.rotation));
  }
  partMenu.style.left = "8px";
  partMenu.style.top = "8px";
  partMenu.hidden = false;
  partMenu.scrollTop = 0;
  const { clientWidth, clientHeight } = document.documentElement;
  const left = x + partMenu.offsetWidth + 16 > clientWidth ? x - partMenu.offsetWidth - 8 : x + 8;
  partMenu.style.left = `${Math.max(8, Math.min(left, clientWidth - partMenu.offsetWidth - 8))}px`;
  partMenu.style.top = `${Math.max(8, Math.min(y + 8, clientHeight - partMenu.offsetHeight - 8))}px`;
  partMenuButtons[0].focus({ preventScroll: true });
}

for (const button of partMenuButtons) {
  button.addEventListener("click", () => {
    const part = drawingParts.get(menuPartNumber);
    closePartMenu(true);
    if (!part || drawingLocked()) return;
    if (button.dataset.partAction) {
      setMeasureMode(false);
      setSelection(new Set(part.indices));
      if (button.dataset.partAction === "edit") void openLayoutPart(part);
      else if (button.dataset.partAction === "exclude") toggleSelectionExcluded();
      else if (button.dataset.partAction === "delete") deleteSelection();
      else contourDialog.showModal();
      return;
    }
    if (button.dataset.rotatePart) {
      setMeasureMode(false);
      setSelection(new Set(part.indices));
      if (button.dataset.rotatePart === "free") startFreeRotation();
      else rotateSelectionBy(Number(button.dataset.rotatePart));
      return;
    }
    const rotation = button.dataset.partRotation;
    if (part.rotation === rotation) return;
    rememberDrawing("настройку поворота детали");
    const next = placed.slice();
    for (const index of part.indices) {
      const path = { ...placed[index] };
      if (rotation) path.partRotation = rotation;
      else delete path.partRotation;
      next[index] = path;
    }
    placed = next;
    part.rotation = rotation;
    updateSelection();
    scheduleWorkspaceSave();
    renderControls();
  });
}
partMenu.addEventListener("keydown", (event) => {
  const available = partMenuButtons.filter(button => !button.hidden);
  const index = available.indexOf(event.target);
  const last = available.length - 1;
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? last
      : (index + (event.key === "ArrowDown" ? 1 : last)) % partMenuButtons.length;
    const target = available[next];
    const item = target.getBoundingClientRect();
    const menu = partMenu.getBoundingClientRect();
    if (item.bottom > menu.bottom - 5) partMenu.scrollTop += item.bottom - menu.bottom + 5;
    else if (item.top < menu.top + 5) partMenu.scrollTop -= menu.top + 5 - item.top;
    target.focus({ preventScroll: true });
  } else if (event.key === "Escape" || event.key === "Tab") {
    event.preventDefault();
    closePartMenu(true);
  }
});
document.addEventListener("pointerdown", (event) => {
  if (!partMenu.contains(event.target)) closePartMenu();
}, true);
window.addEventListener("resize", () => closePartMenu());
window.addEventListener("scroll", (event) => {
  if (event.target !== partMenu) closePartMenu();
}, true);

svg.addEventListener("contextmenu", (event) => {
  if (!camera || !placed.length) return;
  event.preventDefault();
  if (measureMode || event.target.closest("[data-canvas-page], [data-rotation-handle]")) return;
  const index = contourAt(event, drawingPoint(event));
  openPartMenu(drawingParts.get(placed[index]?.partNumber), event.clientX, event.clientY);
});

svg.addEventListener("dblclick", (event) => {
  if (event.button !== 0 || measureMode || event.target.closest("[data-canvas-page], [data-rotation-handle]")) return;
  const click = lastPartClick;
  lastPartClick = null;
  if (!click?.samePart || click.paths !== placed) return;
  event.preventDefault();
  const part = drawingParts.get(click.number);
  if (canCorrectLayoutPart()) void openLayoutPart(part);
  else openPartMenu(part, event.clientX, event.clientY);
});
function drawMarquee(from, to, crossing) {
  const rect = $("selection-marquee");
  for (const [key, value] of Object.entries(svgBox(boxFromPoints(from, to)))) rect.setAttribute(key, value);
  rect.classList.toggle("crossing", crossing);
  rect.removeAttribute("hidden");
}

function previewSelection(transform, transformPoint) {
  const groups = $("path-layer").children;
  for (const index of selection) {
    groups[index].setAttribute("transform", transform);
    notchMarkers.get(index)?.setAttribute("transform", transform);
  }
  for (const entry of drag.labels) positionPartLabel(entry, transformPoint(entry.center));
}

function previewMove(dx, dy) {
  const [x, y] = projectPoint(dx, dy);
  previewSelection(`translate(${x} ${y})`, ([u, v]) => [u + dx, v + dy]);
}

function renderManualGuides() {
  const path = $("snap-guide");
  if (drag?.type !== "move" || !drag.moved || !camera) {
    path.setAttribute("hidden", "");
    return;
  }
  const guideLine = ([x1, y1], [x2, y2]) => `M${x1} ${y1}L${x2} ${y2}`;
  const lines = (drag.guides ?? []).map(({ axis, value }) => axis === 0
    ? guideLine([camera.x, -value], [camera.x + camera.width, -value])
    : guideLine([-value, camera.y], [-value, camera.y + camera.height]));
  if (drag.edgeGuide) {
    const [moved, fixed] = drag.edgeGuide;
    lines.push(...[moved, fixed].map((edge) => guideLine(...edge.map((point) => projectPoint(...point)))));
    const center = ([a, b]) => projectPoint((a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    lines.push(guideLine(center(moved), center(fixed)));
  }
  if (lines.length) updateAttribute(path, "d", lines.join(" "));
  if (lines.length) path.removeAttribute("hidden");
  else path.setAttribute("hidden", "");
}

function manualMoveInput(gesture, event) {
  const [x, y] = drawingPoint(event);
  return { dx: x - gesture.start[0], dy: y - gesture.start[1], free: event.altKey };
}

function scheduleManualMove(gesture, event) {
  gesture.input = manualMoveInput(gesture, event);
  if (gesture.frame != null) return;
  gesture.frame = requestAnimationFrame(() => {
    gesture.frame = null;
    if (drag === gesture) previewManualMove(gesture, gesture.input);
  });
}

function endFreeRotation() {
  if (!freeRotation) return;
  freeRotation = null;
  renderFreeRotation();
}

function startFreeRotation() {
  if (!selectedBounds || !camera) return;
  const { minX, maxX, minY, maxY } = selectedBounds;
  freeRotation = { pivot: [(minX + maxX) / 2, (minY + maxY) / 2] };
  updateProtractorAngle(selectedAngle() ?? 0);
  renderFreeRotation();
}

const protractor = $("protractor");
const protractorAngle = $("protractor-angle");

function updateProtractorAngle(angle) {
  const value = String(Number(normalizeAngle(angle).toFixed(6))).replace(".", ",");
  updateControl(protractorAngle, "value", value);
}

function rotationDial() {
  const units = camera.width / svg.clientWidth;
  const radius = Math.min(100, svg.clientWidth / 4, svg.clientHeight / 4);
  const padding = (radius + 26) * units;
  const [x, y] = projectPoint(...freeRotation.pivot);
  const clamp = (value, start, size) => size < 2 * padding ? start + size / 2
    : Math.max(start + padding, Math.min(start + size - padding, value));
  const centerX = clamp(x, camera.x, camera.width);
  let centerY = clamp(y, camera.y, camera.height);
  const box = protractor.getBoundingClientRect(), viewport = svg.getBoundingClientRect();
  const cx = viewport.left + (centerX - camera.x) / units;
  const cy = viewport.top + (centerY - camera.y) / units;
  const bottom = (box.bottom - viewport.top) * units + camera.y + padding;
  if (cx + radius + 22 > box.left && cx - radius - 22 < box.right
      && cy - radius - 22 < box.bottom && cy + radius + 22 > box.top && bottom + padding <= camera.y + camera.height) {
    centerY = bottom;
  }
  return { x: centerX, y: centerY, units, radius };
}

function anglePoint(angle, radius) {
  const radians = angle * Math.PI / 180;
  return [-Math.sin(radians) * radius, -Math.cos(radians) * radius];
}

function renderFreeRotation() {
  const layer = $("rotation-layer");
  const active = Boolean(freeRotation && camera && drawingPartsReady && !drawingLocked());
  updateControl(protractor, "hidden", !active);
  if (!active) {
    layer.replaceChildren();
    return;
  }
  const gesture = drag?.type === "rotate" ? drag : null;
  const angle = normalizeAngle((selectedAngle() ?? 0) + (gesture?.angle ?? 0));
  if (gesture || document.activeElement !== protractorAngle) updateProtractorAngle(angle);
  const { x, y, units, radius } = gesture?.dial ?? rotationDial();
  const [px, py] = projectPoint(...freeRotation.pivot);
  const [hx, hy] = anglePoint(angle, radius);
  const group = element("g", { class: `rotation-guide${gesture?.snapped ? " is-snapped" : ""}`,
    transform: `translate(${x} ${y}) scale(${units})` });
  group.append(element("circle", { r: radius, class: "rotation-ring" }));
  const ticks = [[], []];
  for (let mark = 0; mark < 360; mark += 15) {
    const major = mark % ROTATION_SNAP_STEP === 0;
    const inner = anglePoint(mark, radius - (major ? 12 : 5));
    const outer = anglePoint(mark, radius);
    ticks[Number(major)].push(`M${inner.join(" ")}L${outer.join(" ")}`);
    if (major && (radius >= 75 || mark % 90 === 0)) {
      const [tx, ty] = anglePoint(mark, radius - 26);
      group.append(rulerText(`${mark}°`, tx, ty + 4, 12, "middle", "rotation-mark"));
    }
  }
  group.append(...ticks.map((segments, index) => element("path", {
    d: segments.join(" "), class: index ? "rotation-ticks major" : "rotation-ticks",
  })));
  if (gesture?.snapped) {
    const [dx, dy] = anglePoint(angle, Math.hypot(camera.width, camera.height) / units);
    const cx = (px - x) / units, cy = (py - y) / units;
    group.append(element("line", { x1: cx - dx, y1: cy - dy, x2: cx + dx, y2: cy + dy, class: "rotation-snap-ray" }));
  }
  group.append(element("line", { x1: 0, y1: 0, x2: hx, y2: hy, class: "rotation-ray" }),
    element("circle", { cx: (px - x) / units, cy: (py - y) / units, r: 4, class: "rotation-pivot" }));
  const title = element("title");
  title.textContent = "Потяните ручку для поворота. Alt отключает привязку.";
  const grip = element("g", { class: "rotation-handle", "data-rotation-handle": "", transform: `translate(${hx} ${hy})` });
  grip.append(title, element("circle", { r: 16 }), element("use", { href: "#i-turn-cw", x: -9, y: -9, width: 18, height: 18 }));
  group.append(grip);
  layer.replaceChildren(group);
}

function rotationPointerAngle([x, y], dial) {
  return Math.atan2(y + dial.x, x + dial.y) * 180 / Math.PI;
}

function previewFreeRotation(gesture, { point, free }) {
  const initial = selectedAngle() ?? 0;
  const raw = initial + rotationPointerAngle(point, gesture.dial) - gesture.startAngle;
  const nearest = Math.round(raw / ROTATION_SNAP_STEP) * ROTATION_SNAP_STEP;
  gesture.snapped = !free && Math.abs(raw - nearest) <= 4;
  const target = gesture.snapped ? nearest : Math.round(raw * 10) / 10;
  const angle = normalizeAngle(target - initial);
  const transform = rotationTransform(angle * Math.PI / 180, freeRotation.pivot);
  gesture.angle = angle;
  const [x, y] = projectPoint(...freeRotation.pivot);
  previewPlacementStatus(gesture.placement, manualPlacementConflicts(gesture.placement, transform));
  previewSelection(`rotate(${-angle} ${x} ${y})`, transform);
  renderFreeRotation();
}

protractor.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!freeRotation || drawingLocked()) return;
  const raw = protractorAngle.value.trim().replace(",", ".");
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw) || !Number.isFinite(Number(raw))) {
    protractorAngle.setCustomValidity("Введите угол в градусах, например 23,5.");
    protractorAngle.reportValidity();
    return;
  }
  rotateSelectionBy(Number(raw) - (selectedAngle() ?? 0), freeRotation.pivot);
  updateProtractorAngle(selectedAngle() ?? 0);
  protractorAngle.select();
});
protractorAngle.addEventListener("input", () => protractorAngle.setCustomValidity(""));
function closeProtractor() {
  cancelCanvasGesture();
  endFreeRotation();
  svg.focus({ preventScroll: true });
}
$("protractor-close").addEventListener("click", closeProtractor);
protractor.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeProtractor();
  }
});

function cancelCanvasGesture({ refreshActions = true } = {}) {
  const gesture = drag;
  if (!gesture) return;
  drag = null;
  if (gesture.frame != null) cancelAnimationFrame(gesture.frame);
  renderManualGuides();
  svg.classList.remove("dragging", "moving-contours", "rotating-contours");
  $("selection-marquee").setAttribute("hidden", "");
  if (["move", "rotate"].includes(gesture.type) && gesture.moved) {
    const groups = $("path-layer").children;
    for (const index of selection) {
      groups[index]?.removeAttribute("transform");
      groups[index]?.classList.remove("move-invalid", "move-valid");
      notchMarkers.get(index)?.removeAttribute("transform");
    }
    for (const entry of gesture.labels ?? []) positionPartLabel(entry, entry.center);
    if (gesture.type === "rotate") renderFreeRotation();
    if (refreshActions) renderLayoutActions();
  }
  if (svg.hasPointerCapture(gesture.pointerId)) svg.releasePointerCapture(gesture.pointerId);
}

svg.addEventListener("pointerdown", (event) => {
  if (!camera || drag || !event.isPrimary) return;
  if (event.button === 0 && event.target.closest("[data-canvas-page]")) return;
  if (cameraFrame !== null) drawCamera();
  const panning = event.button === 1;
  if (!panning && measureMode && event.button === 0 && !drawingLocked()) {
    event.preventDefault();
    svg.focus({ preventScroll: true });
    startMeasurement(event);
    return;
  }
  if (!panning && (event.button !== 0 || !placed.length || drawingLocked())) return;
  if (!panning && !drawingPartsReady) {
    void identifyDrawingParts().catch(() => notifyError("Не удалось определить детали и их насечки. Повторите выбор."));
    return;
  }
  event.preventDefault();
  svg.focus({ preventScroll: true });
  const start = drawingPoint(event);
  const additive = event.shiftKey || event.ctrlKey || event.metaKey;
  const index = panning ? null : contourAt(event, start);
  const rotating = !panning && freeRotation && event.target.closest("[data-rotation-handle]");
  if (!panning && !rotating) endFreeRotation();
  drag = {
    type: panning ? "pan" : rotating ? "rotate" : index !== null && !additive ? "move" : "select",
    pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY,
    x: camera.x, y: camera.y, start, index, additive, moved: false,
  };
  if (rotating) {
    const dial = rotationDial();
    Object.assign(drag, { dial, startAngle: rotationPointerAngle(start, dial), angle: 0, placement: prepareManualPlacement() });
  }
  svg.setPointerCapture(event.pointerId);
  if (panning) svg.classList.add("dragging");
});

svg.addEventListener("pointermove", (event) => {
  if (drag?.pointerId === event.pointerId && drag.type === "pan") {
    const x = drag.x - (event.clientX - drag.clientX) * camera.width / svg.clientWidth;
    const y = drag.y - (event.clientY - drag.clientY) * camera.height / svg.clientHeight;
    if (x !== camera.x || y !== camera.y) {
      cameraMode = "manual";
      camera.x = x;
      camera.y = y;
      scheduleCameraDraw();
    }
  } else if (measureMode && event.isPrimary && !drag) {
    moveMeasurementPointer(event);
  } else if (drag?.pointerId === event.pointerId) {
    if (!drag.moved && Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) >= 4) {
      drag.moved = true;
      if (drag.type === "move") {
        if (!selection.has(drag.index)) setSelection(new Set([drag.index]));
        drag.placement = prepareManualPlacement();
        drag.offset = [0, 0];
        svg.classList.add("moving-contours");
      }
      if (drag.type === "rotate") svg.classList.add("rotating-contours");
      if (placementPreviewActive()) {
        const numbers = new Set([...selection].map((index) => placed[index].partNumber));
        drag.labels = [...numbers].map((number) => partLabels.get(number)).filter(Boolean);
        renderLayoutActions();
      }
    }
    if (!drag.moved) return;
    if (drag.type === "move") scheduleManualMove(drag, event);
    else if (drag.type === "rotate") previewFreeRotation(drag, { point: drawingPoint(event), free: event.altKey });
    else drawMarquee(drag.start, drawingPoint(event), event.clientX < drag.clientX);
  }
});

svg.addEventListener("pointerup", (event) => {
  if (measurePointer?.id === event.pointerId) {
    finishMeasurement(event);
    return;
  }
  const gesture = drag;
  if (gesture?.pointerId !== event.pointerId) return;
  const partNumber = !gesture.moved && gesture.type === "move" ? placed[gesture.index]?.partNumber : null;
  lastPartClick = partNumber ? { number: partNumber, paths: placed,
    samePart: lastPartClick?.paths === placed && lastPartClick.number === partNumber } : null;
  if (!drawingLocked() && gesture.moved && (gesture.type === "move" || gesture.type === "rotate")) {
    if (gesture.type === "move") previewManualMove(gesture, manualMoveInput(gesture, event));
    else previewFreeRotation(gesture, { point: drawingPoint(event), free: event.altKey });
  }
  const committing = !drawingLocked() && (gesture.type === "rotate" ? Boolean(gesture.angle) : gesture.offset?.some(Boolean));
  cancelCanvasGesture({ refreshActions: !committing });
  if (gesture.type === "pan") {
    scheduleWorkspaceSave({ viewOnly: true });
    return;
  }
  if (drawingLocked()) return;
  if (gesture.type === "rotate") {
    if (gesture.angle) rotateSelectionBy(gesture.angle, freeRotation.pivot);
    return;
  }
  const next = gesture.additive ? new Set(selection) : new Set();
  if (!gesture.moved) {
    if (gesture.index !== null) {
      const remove = next.has(gesture.index);
      for (const index of partMembers(gesture.index)) {
        if (remove) next.delete(index);
        else next.add(index);
      }
    }
    setSelection(next);
    return;
  }
  if (gesture.type === "move") {
    moveSelection(...gesture.offset);
    return;
  }
  const end = drawingPoint(event);
  const box = boxFromPoints(gesture.start, end);
  const crossing = event.clientX < gesture.clientX;
  for (const index of placed.keys()) {
    if (crossing ? contourCrossesBox(index, box) : boxContains(box, pathBoxes[index])) next.add(index);
  }
  setSelection(next);
});

svg.addEventListener("lostpointercapture", (event) => {
  if (drag?.pointerId === event.pointerId) cancelCanvasGesture();
  if (measurePointer?.id === event.pointerId) {
    clearMeasurement();
    renderMeasurement();
  }
});
svg.addEventListener("click", (event) => {
  if (!camera || !drawingPartsReady || !drawingLocked() || !(layoutReadOnly() || runInProgress())
      || event.button !== 0 || event.target.closest("[data-canvas-page]")) return;
  const index = contourAt(event, drawingPoint(event));
  setSelection(new Set(index === null ? [] : partMembers(index)));
});
svg.addEventListener("pointerleave", () => {
  if (!measureMode || measurePointer) return;
  if (measurementFrame !== null) {
    cancelAnimationFrame(measurementFrame);
    measurementFrame = null;
  }
  measurementHover = null;
  measurementCursor = null;
  if (measurement) measurement.end = null;
  renderMeasurement();
});
document.addEventListener("keydown", (event) => {
  if (!layoutVisible()) return;
  if (!event.repeat) updateMeasurementModifiers(event);
  if (event.defaultPrevented || event.altKey || event.repeat || event.isComposing || drawingLocked()) return;
  const target = event.target;
  if (event.key === "Escape" && measureMode && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    dismissMeasurement();
    return;
  }
  if (!(target instanceof Element) || target.closest("input, select, textarea, [contenteditable]")) return;
  const modified = event.ctrlKey || event.metaKey;
  if (!modified && !event.shiftKey && (event.code === "KeyL" || event.key.toLowerCase() === "l")
      && !$("measure-tool").disabled && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    setMeasureMode(!measureMode);
    svg.focus({ preventScroll: true });
    return;
  }
  if (!target.closest(".drawing, #contour-dialog")) return;
  const key = event.code === "KeyZ" ? "z" : event.code === "KeyY" ? "y" : event.key.toLowerCase();
  if (event.key === "Delete" && !modified && !event.shiftKey && !measureMode && selection.size) {
    event.preventDefault();
    deleteSelection();
    if (contourDialog.open) contourDialog.close();
    svg.focus({ preventScroll: true });
  } else if (modified && (key === "z" || key === "y" && !event.shiftKey)) {
    event.preventDefault();
    stepEditHistory(key === "y" || event.shiftKey);
  }
});
svg.addEventListener("keydown", (event) => {
  if (!placed.length || drawingLocked() || event.target.closest("[data-canvas-page]")) return;
  if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
    event.preventDefault();
    const [index] = selection;
    const part = drawingParts.get(placed[index]?.partNumber);
    const box = svg.getBoundingClientRect();
    openPartMenu(part, box.left + box.width / 2, box.top + box.height / 2);
    return;
  }
  if (event.key === "Escape") {
    if (measureMode) dismissMeasurement();
    else if (drag) cancelCanvasGesture();
    else if (freeRotation) endFreeRotation();
    else setSelection(new Set());
    event.preventDefault();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    setMeasureMode(false);
    selectAllContours();
    return;
  }
  if (measureMode || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  const seen = new Set();
  const parts = [];
  for (const [index, path] of placed.entries()) {
    if (path.partNumber && seen.has(path.partNumber)) continue;
    if (path.partNumber) seen.add(path.partNumber);
    parts.push(index);
  }
  const change = event.key === "ArrowLeft" ? -1 : 1;
  const selected = parts.findIndex((index) => selection.has(index));
  const current = selected >= 0 ? selected : change === -1 ? 0 : -1;
  setSelection(new Set([parts[(current + change + parts.length) % parts.length]]));
});

const viewport = $("viewport");
viewport.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  fileDragDepth++;
  $("drop-overlay").hidden = false;
});
viewport.addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = drawingLocked() ? "none" : "copy";
});
viewport.addEventListener("dragleave", () => {
  fileDragDepth = Math.max(0, fileDragDepth - 1);
  if (!fileDragDepth) $("drop-overlay").hidden = true;
});
viewport.addEventListener("drop", (event) => {
  event.preventDefault();
  fileDragDepth = 0;
  $("drop-overlay").hidden = true;
  if (event.dataTransfer.files[0]) void loadDrawing(event.dataTransfer.files[0]);
});
document.addEventListener("dragover", (event) => {
  if (event.dataTransfer.types.includes("Files")) event.preventDefault();
});
document.addEventListener("drop", (event) => {
  if (event.dataTransfer.types.includes("Files")) event.preventDefault();
});


new ResizeObserver(() => {
  if (svg.clientWidth !== cameraViewport?.width || svg.clientHeight !== cameraViewport?.height) {
    resizeCamera({ keepView: Boolean(source) });
  }
}).observe(svg);
renderLive();
void readConfiguration();
void readAppUpdate();
scheduleLiveRead(0);

function workspaceQuery(id, mode) {
  return `/api/operator-workspace?${new URLSearchParams({ cutId: id, mode })}`;
}

function canCorrectLayoutPart() {
  return layoutVisible() && !layoutPreparation() && !drawingLocked() && !busy()
    && !live.stopAvailable && !currentUpload && !xiaoHandoff && activeRun?.state !== "transferring";
}

let openingLayoutPart = false;

function layoutPartEntities(paths) {
  const entities = paths.map((path, index) => ({
    id: `layout-${index}`, type: "polyline", points: path.points, layer: path.layer || "0", closed: path.closed,
    ...(path.notch ? { notch: true } : {}),
  }));
  const outlines = entities.filter(entity => entity.closed && !entity.notch);
  let checksRemaining = 250_000;
  for (const mark of entities) {
    if (!mark.notch) continue;
    let closest = null;
    let bestDistance = .05;
    const endpoints = [mark.points[0], mark.points.at(-1)];
    for (const outline of outlines) {
      if (outline.points.length > checksRemaining) return entities;
      checksRemaining -= outline.points.length;
      for (let i = 0; i < outline.points.length; i++) {
        const a = outline.points[i];
        const b = outline.points[(i + 1) % outline.points.length];
        for (const endpoint of endpoints) {
          const distance = Math.hypot(...placementPointOffset(endpoint, a, b));
          if (distance < bestDistance) {
            closest = outline.id;
            bestDistance = distance;
          }
        }
      }
    }
    if (closest) mark.parentId = closest;
  }
  return entities;
}

async function openLayoutPart(part) {
  if (!part || !workplaces || openingLayoutPart || !canCorrectLayoutPart()) return;
  openingLayoutPart = true;
  const originalPaths = placed;
  const originalCut = managedCut;
  const originals = part.indices.map(index => originalPaths[index]);
  const layout = $("layout-workplace");
  const blocked = () => busy() || drawingLocked() || live.stopAvailable || currentUpload || xiaoHandoff
    || activeRun?.state === "transferring";
  const validateContext = () => {
    if (managedCut !== originalCut || blocked()) throw new Error("Дождитесь освобождения раскладки на плоттере.");
    if (placed !== originalPaths || workspaceRevision === null) throw new Error("Раскладка изменилась. Откройте деталь заново.");
  };
  try {
    if (!await persistWorkspace()) throw new Error("Сохраните текущую раскладку перед редактированием детали.");
    validateContext();
    cancelCanvasGesture();
    endFreeRotation();
    closePartMenu();
    layout.hidden = true;
    renderControls();
    const name = `№${part.number} · ${part.name || "Деталь"}`;
    workplaces.openLayoutPart({ name, entities: layoutPartEntities(originals) }, {
      onBack() {
        layout.hidden = false;
        resizeCamera({ keepView: true });
        renderControls();
        svg.focus({ preventScroll: true });
      },
      async onSaveDocument({ entities }) {
        validateContext();
        const result = await getJson("/api/cad/paths", jsonRequest({ entities }, AbortSignal.timeout(30_000)));
        validateContext();
        if (!result.paths.some(path => !path.notch)) {
          throw new Error(`${name}: оставьте хотя бы один контур реза.`);
        }
        const indices = new Set(part.indices);
        const allExcluded = part.indices.every(index => excluded.has(index));
        const replacement = result.paths.map((path, index) => ({
          path: {
            ...path, notch: Boolean(path.notch), partNumber: part.number,
            ...(part.rotation ? { partRotation: part.rotation } : {}),
            ...(part.name ? { partName: part.name } : {}),
            ...(part.product ? { productName: part.product } : {}),
            ...(originals[0].partAngle !== undefined ? { partAngle: originals[0].partAngle } : {}),
          },
          excluded: allExcluded || Boolean(result.sourceIds?.[index]?.length && result.sourceIds[index].every(id => {
            const sourceIndex = /^layout-(\d+)$/.exec(id)?.[1];
            return sourceIndex !== undefined && excluded.has(part.indices[Number(sourceIndex)]);
          })),
        }));
        const next = [];
        const nextExcluded = new Set();
        const nextSelection = new Set();
        for (const [index, path] of placed.entries()) {
          if (index === part.indices[0]) {
            for (const item of replacement) {
              nextSelection.add(next.length);
              if (item.excluded) nextExcluded.add(next.length);
              next.push(item.path);
            }
          }
          if (!indices.has(index)) {
            if (excluded.has(index)) nextExcluded.add(next.length);
            next.push(path);
          }
        }
        let response;
        try {
          response = await writeWorkspace(workspaceSnapshot(next, nextExcluded), true);
        } catch (error) {
          if (error.status === 409 && /вкладк|редакц/i.test(messageFrom(error))) workspaceRevision = null;
          throw error;
        }
        rememberDrawing("изменение детали в конструкторе");
        workspaceRevision = response.revision;
        updateManagedCutMetadata(response);
        placed = next;
        excluded = nextExcluded;
        selection = nextSelection;
        setSaveError();
        updateDrawing({ save: false });
        return { closeEditor: true };
      },
    });
  } catch (error) {
    layout.hidden = false;
    renderControls();
    setSaveError(messageFrom(error));
  } finally {
    openingLayoutPart = false;
  }
}

async function navigateLayout(next, { resume = false } = {}) {
  if (busy() || (!resume && layoutVisible() && !layoutPreparation() && runInProgress())) {
    throw new Error("Дождитесь завершения текущего действия на плоттере.");
  }
  if (layoutVisible()) {
    if (kitLayout()) {
      if (workspaceChange !== workspaceSaved) {
        const { confirmAction } = await import("/confirm-action.js");
        if (!await confirmAction({
          title: "Закрыть раскладку комплекта?",
          message: "Изменения раскладки будут потеряны.",
          confirmLabel: "Закрыть без сохранения",
          cancelLabel: "Продолжить редактирование",
        })) return false;
      }
    } else if (!await persistWorkspace() && workspaceChange !== workspaceSaved) {
      throw new Error("Сохраните изменения текущей раскладки.");
    }
  }
  if (next && ["operator", "demo"].includes(next.mode) && demoMode !== (next.mode === "demo")) {
    await readConfiguration();
    if (!liveObserved) {
      if (liveReading) await liveReading;
      else await readLive();
    }
    if (!canSwitchMachineMode()) throw new Error("Дождитесь завершения текущего раскроя для смены режима.");
    await toggleDemoMode();
    if (demoMode !== (next.mode === "demo")) throw new Error("Не удалось переключить режим плоттера.");
  }
  setPending("open-layout");
  try {
    if (!next) {
      $("layout-workplace").hidden = true;
      $("workplace-page").hidden = false;
      if (kitLayout()) {
        managedCut = null;
        applyWorkspace({ revision: null, workspace: null });
        setSaveError();
      }
      return;
    }
    let response;
    if (next.mode === "operator") await readConfiguration();
    const interactive = ["operator", "demo"].includes(next.mode);
    const oldHardwareCut = hardwareCutId;
    if (next.mode === "kit") {
      const query = new URLSearchParams({ id: next.kitId, mode: "kit-layout", widthMm: next.widthMm, quantity: next.quantity });
      response = await getJson(`/api/registry?${query}`);
    } else if (interactive) {
      response = await getJson(workspaceQuery(next.id, next.mode));
      if (response.status !== "done" || next.mode === "demo") {
        const context = await getJson("/api/cut-context", jsonRequest({ action: "select", cutId: next.id }));
        response = { ...response, ...context, order: context.order ?? response.order };
        hardwareCutId = next.id;
      }
    } else if (next.id) {
      response = await getJson(workspaceQuery(next.id, next.mode));
    } else {
      await readConfiguration();
      if (hardwareCutId) {
        const released = await getJson("/api/cut-context", jsonRequest({ action: "select", cutId: null }));
        hardwareCutId = null;
        clearCurrentUpload();
        activeRun = null;
        cutProgress = controllerRoll = null;
        applyJobStatus(released);
      }
      response = await getJson("/api/workspace");
    }
    cancelCanvasGesture();
    $("layout-workplace").dataset.mode = response.status === "done" && next.mode === "operator" ? "preview" : next.mode;
    $("layout-workplace").hidden = false;
    $("workplace-page").hidden = true;
    managedCut = next.mode === "kit"
      ? { ...next, widthMm: response.widthMm, quantity: response.quantity, saved: response.saved && response.valid }
      : next.id ? { ...next, name: response.name ?? next.name, order: response.order ?? next.order,
        status: response.status, cutRevision: response.cutRevision, widthMm: response.widthMm } : null;
    if (interactive && oldHardwareCut !== hardwareCutId) {
      clearCurrentUpload();
      activeRun = null;
      cutProgress = controllerRoll = null;
    }
    if (Object.hasOwn(response, "roll")) applyJobStatus(response);
    applyWorkspace(response);
    setSaveError(kitLayout() && response.saved && !response.valid
      ? "Состав или чертежи комплекта изменились. Разложите детали заново." : "");
    requestAnimationFrame(() => resizeCamera({ keepView: true }));
    return { ...response, demo: next.mode === "demo" };
  } finally {
    restoringWorkspace = false;
    setPending(null);
    renderControls();
  }
}

function addRegisteredParts(paths) {
  if (!layoutVisible() || layoutPreparation() || drawingLocked() || !paths.length) {
    throw new Error("Откройте рабочий стол раскроя для добавления деталей.");
  }
  let number = placed.reduce((max, path) => Math.max(max, path.partNumber ?? 0), 0);
  const groups = new Map();
  for (const path of paths) {
    if (!groups.has(path.partNumber)) groups.set(path.partNumber, []);
    groups.get(path.partNumber).push(path);
  }
  const gap = Math.max(5, nestingSettings?.gapMm ?? 0);
  const margin = Math.max(5, nestingSettings?.marginMm ?? 0);
  let x = (bounds(placed)?.maxX ?? 0) + gap;
  const start = placed.length;
  const added = [];
  for (const contours of groups.values()) {
    const box = bounds(contours);
    number++;
    for (const path of contours) added.push({ ...translatePath(path, x - box.minX, margin - box.minY), partNumber: number });
    x += box.maxX - box.minX + gap;
  }
  rememberDrawing("добавление деталей из реестра");
  if (!source) source = { filename: "registry.dxf", warnings: [] };
  placed = [...placed, ...added];
  selection = new Set(added.map((_, index) => index + start));
  updateDrawing({ keepView: false });
  fitCamera("overview");
}

async function finishRegisteredCut() {
  if (managedCut?.mode !== "operator" || !await persistWorkspace()) throw new Error("Сохраните раскладку перед завершением.");
  await getJson("/api/cut-context", jsonRequest({ action: "finish", cutId: managedCut.id, revision: workspaceRevision }));
  hardwareCutId = null;
  managedCut.status = "done";
  $("layout-workplace").dataset.mode = "preview";
  renderControls();
}

async function releaseRegisteredCut() {
  if (managedCut?.mode !== "operator" || !await persistWorkspace()) throw new Error("Сохраните раскладку перед возвращением в очередь.");
  const response = await getJson("/api/cut-context", jsonRequest({ action: "release", cutId: managedCut.id, revision: workspaceRevision }));
  applyJobStatus(response);
  managedCut.status = response.status;
}

void import("/workplaces.js").then(({ mountWorkplaces }) => {
  workplaces = mountWorkplaces({ navigateLayout, addRegisteredParts, finishRegisteredCut, releaseRegisteredCut, onExit: disarmControl,
    flushLayout: persistWorkspace,
    layoutWidths: () => ({ defaultWidthMm: defaultFabricWidthMm(), maxWidthMm: machine.height }),
    request: (url, options) => fetch(url, { cache: "no-store", ...options }).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Сервер вернул ошибку ${response.status}.`);
      return data;
    }),
    openFile: () => $("file-input").click(),
    getActiveCut: async () => { await readConfiguration(); return hardwareCutId; },
  });
}).catch(error => { $("workplace-page").textContent = `Не удалось открыть рабочие места. ${messageFrom(error)}`; });
