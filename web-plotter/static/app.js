"use strict";

const $ = (id) => document.getElementById(id);
const svg = $("drawing-svg");
const svgNamespace = "http://www.w3.org/2000/svg";
const formatNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const formatCoordinate = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
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
const clockFormat = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const holdButtons = Array.from(document.querySelectorAll("[data-controller-hold]"));
const operationButtons = Array.from(document.querySelectorAll("[data-operation]"));
const placementFields = ["offset-x", "offset-y", "rotation", "size-width", "size-height"];
const DEFAULT_FABRIC_WIDTH_MM = 1380;
const sectionColors = ["#373737", "#5a5a5a", "#444444", "#686868"];
const trackedHeadStates = new Set(["ready", "starting", "running", "pausing", "paused", "resuming", "cancelling"]);
const number = (value) => formatNumber.format(value);
const included = () => placed.filter((_, index) => !excluded.has(index));
const focused = () => document.visibilityState === "visible" && document.hasFocus();

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
let transferChecklistPlanId = null;
let activeRun = null;
let controllerRoll = null;
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
let lastPositionAt = null;

let source = null;
let placed = [];
let pathBoxes = [];
let drawingParts = new Map();
let drawingPartsReady = true;
let partDiscovery = null;
let menuPartNumber = null;
let includedBounds = null;
let selection = new Set();
let selectedBounds = null;
let selectionAngle = 0;
let excluded = new Set();
const drawingUndo = [];
let routeApplied = true;
let contourRepair = null;
let nestingSettings = null;
let nestRequest = null;
let nestBlocking = [];
let jobError = "";
let isLoading = false;
let toastTimer;
let gridVisible = true;
let panMode = false;
let drag = null;
let freeRotation = null;
let fileDragDepth = 0;
// The camera uses SVG coordinates. From the operator's end, X goes up and Y goes left.
let baseCamera = null;
let camera = null;
let cameraMode = "work";
let workspaceRevision = null;
let restoringWorkspace = true;
let workspaceChange = 0;
let workspaceSaved = 0;
let saveTimer;
let savePromise = null;
let cutPlan = null;
let cutPlanError = "";
let cutPlanEpoch = 0;
let cutPlanTimer;
let cutPlanRequest = null;
let warningIndices = new Set();
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
let clampLowered = readStoredBoolean("plotter.clampLowered");
let vacuumEnabled = readStoredBoolean("plotter.vacuumEnabled");
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
    return JSON.parse(sessionStorage.getItem("plotter.runStatistics"));
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

async function checkedFetch(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { cache: "no-store", ...options });
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

async function getJson(url, options) {
  return (await checkedFetch(url, options)).json();
}

function jsonRequest(body, signal) {
  return {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  };
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

function renderTable() {
  if (!camera) return;
  const { width, height } = machine;
  const length = Math.max(width, includedBounds?.maxX || 0);
  const layer = $("table-layer");
  const units = camera.width / Math.max(svg.clientWidth, 1);
  const surface = svgBox({ minX: 0, minY: 0, maxX: length, maxY: height });
  const parts = [element("rect", { ...surface, class: "table-background" })];
  const materialWidth = cutPlan?.material?.widthMm ?? nestingSettings?.widthMm;
  const fabric = includedBounds && materialWidth > 0 ? Math.min(materialWidth, height) : 0;
  if (fabric) {
    parts.push(element("rect", { x: -fabric, y: -length, width: fabric, height: length, class: "material-surface" }));
  }
  if (gridVisible) parts.push(element("rect", { ...surface, fill: "url(#major-grid)" }));
  parts.push(element("rect", { ...surface, class: "table-border", "vector-effect": "non-scaling-stroke" }));
  if (fabric) {
    parts.push(element("path", {
      d: `M${-fabric} 0V${-length}`, class: "material-edge", "vector-effect": "non-scaling-stroke",
    }));
  }

  const step = [100, 250, 500, 1000, 2000, 5000, 10000, 25000].find((mm) => mm >= units * 54) ?? 50000;
  const stroke = { class: "ruler-tick", "vector-effect": "non-scaling-stroke" };
  for (let y = 0; y <= height; y += step) {
    parts.push(element("path", { d: `M${-y} 0v${5 * units}`, ...stroke }));
    parts.push(rulerText(number(y), -y, 22 * units, 12 * units));
  }
  const firstX = Math.max(step, Math.ceil((-camera.y - camera.height) / step) * step);
  for (let x = firstX; x <= Math.min(length, -camera.y); x += step) {
    parts.push(element("path", { d: `M0 ${-x}h${5 * units}`, ...stroke }));
    parts.push(rulerText(number(x), 10 * units, -x + 4 * units, 12 * units, "start"));
  }
  const axis = 25 * units;
  const tip = 3 * units;
  const axisPath = `M0 ${-axis}V0H${-axis} M${-tip} ${tip - axis}l${tip} ${-tip} ${tip} ${tip} M${tip - axis} ${-tip}l${-tip} ${tip} ${tip} ${tip}`;
  parts.push(element("path", { d: axisPath, class: "axis-mark", "vector-effect": "non-scaling-stroke" }));
  parts.push(rulerText("Y", -axis - 7 * units, 3 * units, 11 * units, "end", "axis-label"));
  parts.push(rulerText("X", 0, -axis - 5 * units, 11 * units, "middle", "axis-label"));
  layer.replaceChildren(...parts);
}

function canvasSection() {
  if (cutPlan?.mode !== "roll") return null;
  const tracked = trackedCanvasRun();
  const matching = tracked?.planId === cutPlan.planId && !["completed", "cancelled", "error"].includes(tracked.state);
  const waiting = matching && tracked.state === "advance";
  const kind = simulation.active ? "simulation" : waiting ? "next" : matching ? "current" : "preview";
  const number = simulation.active ? simulation.visiblePage
    : waiting ? tracked.page + 1 : matching ? tracked.page : previewCutPage ?? shownCutPage ?? 1;
  const page = cutPlan.pages[number - 1] ?? cutPlan.pages[0];
  return page ? { page, kind } : null;
}

function trackedCanvasRun() {
  if (controllerRoll) return cutProgress?.planId === controllerRoll.planId ? cutProgress : controllerRoll;
  return cutProgress;
}

function updateCanvasSection() {
  const section = canvasSection();
  const tools = $("canvas-section-tools");
  tools.hidden = !section;
  if (section) {
    const page = section.page;
    $("section-focus").textContent = `Проход ${page.number} из ${cutPlan.pages.length}`;
    const locked = section.kind === "current" || section.kind === "next";
    $("section-prev").disabled = locked || page.number === 1;
    $("section-next").disabled = locked || page.number === cutPlan.pages.length;
  }
  const pageKey = section ? `${cutPlan.planId}/${section.page.number}` : "";
  const paintKey = section ? `${pageKey}/${section.kind}` : "";
  if (paintKey === paintedSectionKey) return;
  paintedSectionKey = paintKey;
  if (pageKey !== followedSectionKey) {
    followedSectionKey = pageKey;
    if (cameraMode === "work" && !drag) {
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
  scheduleWorkspaceSave();
  return true;
}

function renderPages() {
  const layer = $("page-layer");
  const annotations = $("page-annotation-layer");
  if (!camera || cutPlan?.mode !== "roll") {
    layer.replaceChildren();
    annotations.replaceChildren();
    return;
  }
  const units = camera.width / Math.max(svg.clientWidth, 1);
  const elements = [];
  const tags = [];
  const section = canvasSection();
  const selected = section?.page;
  const width = Math.min(cutPlan.material?.widthMm ?? machine.height, machine.height);
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
  for (const page of cutPlan.pages) {
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
      ...(!viewingLocked || page === selected ? {
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
  const dots = [];
  const activeDots = [];
  for (const seam of cutPlan.seams) {
    const [x, y] = projectPoint(seam.x, seam.y);
    if (x < camera.x || x > camera.x + camera.width || y < camera.y || y > camera.y + camera.height) continue;
    const active = selected && Math.abs(seam.x - selected.startX) < 1e-6;
    const radius = (active ? 5.5 : 4) * units;
    (active ? activeDots : dots).push(`M${x - radius} ${y}a${radius} ${radius} 0 1 0 ${2 * radius} 0a${radius} ${radius} 0 1 0 ${-2 * radius} 0`);
  }
  if (dots.length) elements.push(element("path", { d: dots.join(""), class: "page-seam", "vector-effect": "non-scaling-stroke" }));
  if (activeDots.length) elements.push(element("path", { d: activeDots.join(""), class: "page-seam is-active", "vector-effect": "non-scaling-stroke" }));
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
    headMarker.setAttribute("transform", `translate(${x} ${y}) scale(${units})`);
    headStaleTimer = setTimeout(renderHead, Math.max(1, (stale ? visibleFor : freshFor) - age));
  }
}

function drawCamera() {
  if (!camera) return;
  svg.setAttribute("viewBox", `${camera.x} ${camera.y} ${camera.width} ${camera.height}`);
  $("zoom-reset").textContent = `${Math.round(baseCamera.width / camera.width * 100)}%`;
  renderTable();
  renderPages();
  renderHead();
  renderPartLabels();
  renderFreeRotation();
  simulation.render();
}

function newCamera(content, controlGutter) {
  const { clientWidth: viewWidth, clientHeight: viewHeight } = svg;
  const left = Math.min(120, viewWidth * .32);
  const right = Math.max(Math.min(80, viewWidth * .32), controlGutter);
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

function resizeCamera() {
  if (!svg.clientWidth || !svg.clientHeight) return;
  const controls = $("canvas-controls");
  const controlGutter = getComputedStyle(controls).position === "absolute" ? controls.offsetWidth + 28 : 0;
  const content = {
    minX: Math.min(0, includedBounds?.minX || 0), minY: Math.min(0, includedBounds?.minY || 0),
    maxX: Math.max(machine.width, includedBounds?.maxX || 0), maxY: Math.max(machine.height, includedBounds?.maxY || 0),
  };
  const previousBase = baseCamera;
  baseCamera = newCamera(svgBox(content), controlGutter);
  if (cameraMode === "work") {
    const page = canvasSection()?.page;
    const start = page?.startX ?? content.minX;
    const end = page?.endX ?? machine.width;
    camera = newCamera(svgBox({ ...content, minX: start, maxX: end }), controlGutter);
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
  scheduleWorkspaceSave();
}

function hasRollLayout() {
  return cutPlan?.mode === "roll" || includedBounds?.maxX > machine.width;
}

function zoom(factor, px = 0.5, py = 0.5) {
  if (!camera) return;
  const scale = Math.max(0.04, Math.min(hasRollLayout() ? 1 : 2.4, (camera.width / baseCamera.width) / factor));
  if (hasRollLayout() && scale === 1 && factor < 1) {
    fitCamera("overview");
    return;
  }
  const width = baseCamera.width * scale;
  const height = baseCamera.height * scale;
  cameraMode = "manual";
  camera = {
    x: camera.x + (camera.width - width) * px, y: camera.y + (camera.height - height) * py, width, height,
  };
  drawCamera();
  scheduleWorkspaceSave();
}

function screenFraction(event) {
  const box = svg.getBoundingClientRect();
  return [(event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height];
}

function drawingPoint(event) {
  const [px, py] = screenFraction(event);
  return projectPoint(camera.x + px * camera.width, camera.y + py * camera.height);
}

function setTool(isPan) {
  cancelCanvasGesture();
  endFreeRotation();
  panMode = isPan;
  svg.classList.toggle("pan", isPan);
  for (const [id, active] of [["select-tool", !isPan], ["pan-tool", isPan]]) {
    $(id).setAttribute("aria-pressed", String(active));
    $(id).classList.toggle("selected", active);
  }
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

function outsideMaterial(box) {
  if (!box) return false;
  const epsilon = 0.000001;
  const materialWidth = Math.min(nestingSettings?.widthMm ?? machine.height, machine.height);
  return box.minX < -epsilon || box.minY < -epsilon || box.maxY > materialWidth + epsilon;
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

function drawingLocked() {
  return restoringWorkspace || isLoading || nestRequest !== null || contourRepair !== null
    || ["upload", "cut"].includes(pendingCommand) || runInProgress();
}

function runInProgress() {
  return Boolean(live.externalMotion || activeRun?.startedAt && activeRunStates.has(activeRun.state));
}

function selectAllContours() {
  if (placed.length && !drawingLocked()) setSelection(new Set(placed.keys()));
}

function rememberDrawing(label) {
  drawingUndo.push({ placed, excluded, selection, selectionAngle, nestingSettings, routeApplied, label });
  if (drawingUndo.length > 10) drawingUndo.shift();
}

function editSelection(transform, label, angle = selectionAngle) {
  if (!selectedBounds || drawingLocked() || !drawingPartsReady) return;
  rememberDrawing(label);
  placed = placed.map((path, index) => selection.has(index) ? transform(path, index) : path);
  selectionAngle = angle;
  updateDrawing();
}

function moveSelection(dx, dy) {
  if (dx || dy) editSelection((path) => translatePath(path, dx, dy), "перемещение");
}

function rotationTransform(radians, [x, y]) {
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  return ([u, v]) => [x + (u - x) * cosine - (v - y) * sine, y + (u - x) * sine + (v - y) * cosine];
}

function rotateSelection(angle) {
  if (!selectedBounds || drawingLocked() || !drawingPartsReady || angle === selectionAngle) return;
  const radians = ((angle - selectionAngle) % 360) * Math.PI / 180;
  const { minX, minY } = selectedBounds;
  const rotate = rotationTransform(radians, [minX, minY]);
  const placement = prepareManualPlacement();
  const rotated = bounds([...selection].map((index) => mapPath(placed[index], rotate)));
  const transform = (point) => {
    const [x, y] = rotate(point);
    return [x + minX - rotated.minX, y + minY - rotated.minY];
  };
  if (!checkManualPlacement(placement, transform).valid) {
    notifyError("Для поворота недостаточно места с учётом зазора и отступов.");
    return;
  }
  editSelection((path) => mapPath(path, transform), "поворот", angle);
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
  if (id === "rotation") rotateSelection(value % 360);
  if (id === "size-width" || id === "size-height") {
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
  if (!selection.size || drawingLocked() || !drawingPartsReady) return;
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
  selectionAngle = 0;
  updateDrawing();
  if (placed.length) fitCamera();
}

function pathData(path) {
  const xy = path.points.map(([x, y]) => {
    const [u, v] = projectPoint(x, y);
    return `${u.toFixed(4)} ${v.toFixed(4)}`;
  }).join(" L ");
  return `M ${xy}${path.closed ? " Z" : ""}`;
}

function renderPaths() {
  const fragment = document.createDocumentFragment();
  for (const [index, path] of placed.entries()) {
    const group = element("g", { "data-index": index });
    const classes = ["cut-path"];
    if (path.closed) classes.push("closed");
    if (selection.has(index)) classes.push("selected");
    if (excluded.has(index)) classes.push("excluded");
    if (!excluded.has(index) && (outsideMaterial(pathBoxes[index]) || warningIndices.has(index))) classes.push("outside");
    if (completedDrawingPaths.has(index)) classes.push("is-cut-complete");
    if (completedDrawingParts.has(partProgressKey(index))) classes.push("is-part-complete");
    const section = sectionByPath.get(index);
    if (section === null) classes.push("crosses-sections");
    const attributes = { d: pathData(path), "vector-effect": "non-scaling-stroke" };
    if (section) colorCutSection(group, section);
    group.append(
      element("path", { ...attributes, class: classes.join(" ") }),
      element("path", { ...attributes, class: "cut-hit" }),
    );
    fragment.append(group);
  }
  $("path-layer").replaceChildren(fragment);
}

function syncPlacementFields() {
  const box = selectedBounds;
  const values = box ? [box.minX, box.minY, selectionAngle, box.maxY - box.minY, box.maxX - box.minX] : [];
  for (const [index, id] of placementFields.entries()) {
    $(id).value = values.length ? String(Number(values[index].toFixed(3))) : "";
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
      drawingParts.set(id, { number: id, indices: [], box: { ...box }, rotation: "" });
    }
    const part = drawingParts.get(id);
    part.indices.push(index);
    part.rotation = path.partRotation === "quarter" ? "any" : path.partRotation ?? part.rotation;
    part.box.minX = Math.min(part.box.minX, box.minX);
    part.box.minY = Math.min(part.box.minY, box.minY);
    part.box.maxX = Math.max(part.box.maxX, box.maxX);
    part.box.maxY = Math.max(part.box.maxY, box.maxY);
  }
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
  if (!model || model.signature !== signature) {
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
    $("live-trace-layer").replaceChildren(...(verifiedTrace.length ? [element("path", {
      d: verifiedTrace.join(" "), class: "cut-trace", "vector-effect": "non-scaling-stroke",
    })] : []));
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
  if (!simulation.active && tracking) {
    const page = tracking.state === "advance" ? Math.min(tracking.page + 1, tracking.pages) : tracking.page;
    const state = tracking.state === "advance" ? "Смена прохода" : runLabels[tracking.state];
    renderCanvasStatus("live", page, tracking.pages, [state, `Детали ${model.count} / ${model.total}`].filter(Boolean).join(" · "),
      tracking.state === "completed");
  } else if (!simulation.active) {
    $("canvas-cut-status").hidden = true;
  }
  return tracking ? { count: model.count, total: model.total } : null;
}

function renderCanvasStatus(mode, page, total, detail, complete = false) {
  const chip = $("canvas-cut-status");
  chip.hidden = false;
  chip.dataset.mode = mode;
  chip.classList.toggle("is-complete", complete);
  for (const [id, value] of [
    ["canvas-cut-mode", mode === "live" ? "Раскрой" : "Симуляция"],
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
  if (drawingPartsReady) return Promise.resolve();
  if (partDiscovery?.paths === placed) return partDiscovery.promise;
  const paths = placed;
  const request = { paths };
  partDiscovery = request;
  request.promise = (async () => {
    try {
      const result = await getJson("/api/nest-parts", jsonRequest({ paths }, AbortSignal.timeout(30_000)));
      if (paths !== placed) return;
      const numbered = new Array(paths.length).fill(null);
      for (const { number: id, indices, rotation } of result.parts) {
        for (const index of indices) {
          numbered[index] = { ...paths[index], partNumber: id, ...(rotation ? { partRotation: rotation } : {}) };
        }
      }
      if (numbered.some((path) => !path)) throw new Error("Сервер вернул неполный список деталей.");
      placed = numbered;
      indexDrawingParts();
      selection = expandToWholeParts(selection);
      resetCutPlan();
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

function renderPartLabels() {
  const layer = $("part-label-layer");
  if (!camera || !svg.clientWidth || !drawingParts.size || (drag?.moved && ["move", "rotate"].includes(drag.type))) {
    layer.replaceChildren();
    return;
  }
  const units = camera.width / svg.clientWidth;
  const selectedParts = new Set([...selection].map((index) => placed[index]?.partNumber));
  const fragment = document.createDocumentFragment();
  const visible = [];
  for (const part of drawingParts.values()) {
    const { x, y, width, height } = svgBox(part.box);
    const px = (x + width / 2 - camera.x) / units;
    const py = (y + height / 2 - camera.y) / units;
    if (px < 0 || px > svg.clientWidth || py < 0 || py > svg.clientHeight) continue;
    visible.push({ part, px, py, area: width * height, active: selectedParts.has(part.number) });
  }
  visible.sort((a, b) => b.active - a.active || Boolean(b.part.rotation) - Boolean(a.part.rotation) || b.area - a.area);
  const occupied = [];
  for (const { part, px, py, active } of visible) {
    if (occupied.length === 200) break;
    const label = `№${part.number}`;
    const complete = completedDrawingParts.has(part.number);
    const width = 12 + label.length * 8 + (part.rotation || complete ? 17 : 0);
    const left = px - width / 2;
    if (occupied.some((box) => Math.abs(box.y - py) < 25 && left < box.right + 3 && left + width > box.left - 3)) continue;
    occupied.push({ left, right: left + width, y: py });
    const classes = ["part-label", part.rotation, active ? "is-active" : "",
      part.indices.every((index) => excluded.has(index)) ? "is-excluded" : "", complete ? "is-complete" : ""];
    const badge = element("g", {
      class: classes.join(" "), "data-part-number": part.number,
      transform: `translate(${camera.x + left * units} ${camera.y + (py - 11) * units}) scale(${units})`,
    });
    const title = element("title");
    title.textContent = `Деталь ${label} · ${complete ? "Рез завершён" : partRotationText(part.rotation)}`;
    const text = element("text", { x: 6, y: 15.5 });
    text.textContent = label;
    badge.append(title, element("rect", { width, height: 22, rx: 5, class: "part-label-bg" }), text);
    if (complete) {
      badge.append(element("path", { class: "part-check", d: `M${width - 16} 11l3 3 5 -6` }));
    } else if (part.rotation) {
      badge.append(element("use", {
        class: "icon", href: part.rotation === "none" ? "#i-lock" : "#i-turn-cw",
        x: width - 17, y: 5, width: 12, height: 12,
      }));
    }
    fragment.append(badge);
  }
  layer.replaceChildren(fragment);
}

function updateSelection() {
  selectedBounds = bounds([...selection].map((index) => placed[index]));
  const allExcluded = selection.size > 0 && [...selection].every((index) => excluded.has(index));
  const exclude = $("exclude-button");
  exclude.textContent = allExcluded ? "Вернуть" : "Исключить";
  exclude.title = allExcluded ? "Вернуть выделенные контуры в задание" : "Исключить выделенные контуры из задания";
  syncPlacementFields();
  renderPartLabels();
}

function setSelection(indices) {
  indices = expandToWholeParts(indices);
  if (indices.size === selection.size && [...indices].every((index) => selection.has(index))) return;
  endFreeRotation();
  const groups = $("path-layer").children;
  for (const index of selection) if (!indices.has(index)) groups[index]?.firstElementChild.classList.remove("selected");
  for (const index of indices) if (!selection.has(index)) groups[index].firstElementChild.classList.add("selected");
  selection = indices;
  selectionAngle = 0;
  updateSelection();
  renderControls();
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
    if (closed || points.length < 2 || sameContourPoint(points[0], points.at(-1))) continue;
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
  cancelCanvasGesture();
  const paths = included();
  const join = $("optimize-join").checked ? joinTouchingContours(paths) : { paths, joined: 0, skipped: false };
  const optimize = $("optimize-route").checked;
  if (join.joined || optimize !== routeApplied) {
    rememberDrawing(join.joined ? "соединение контуров" : "настройку маршрута");
    routeApplied = optimize;
    if (join.joined) replaceJoinedContours(join.paths);
    updateDrawing();
  }
  if (join.skipped) notifyError("Чертёж слишком сложный для проверки всех стыков.");
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
  selectionAngle = 0;
}

function undoDrawingEdit() {
  if (!drawingUndo.length || drawingLocked()) return;
  cancelCanvasGesture();
  ({ placed, excluded, selection, selectionAngle, nestingSettings, routeApplied } = drawingUndo.pop());
  updateDrawing();
  fitCamera();
}

function showRepairResult(message) {
  const result = $("repair-result");
  result.hidden = !message;
  result.textContent = message;
}

async function restoreClosedContours() {
  if (drawingLocked() || !included().length) return;
  cancelCanvasGesture();
  const request = new AbortController();
  contourRepair = request;
  showRepairResult("Проверяем стыки контуров…");
  renderControls();
  try {
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]);
    const result = await getJson("/api/close-contours", jsonRequest({ paths: placed, excluded: [...excluded] }, signal));
    if (signal.aborted || runInProgress()) return;
    if (result.closedCount) {
      rememberDrawing("восстановление замкнутых контуров");
      selection = new Set(result.sources.flatMap((indices, index) => indices.some((old) => selection.has(old)) ? [index] : []));
      placed = result.paths;
      excluded = new Set(result.excluded);
      selectionAngle = 0;
      updateDrawing();
    }
    const message = result.closedCount
      ? `Восстановлено замкнутых контуров: ${number(result.closedCount)}.`
      : "Контуров, которые можно уверенно замкнуть, не найдено.";
    showRepairResult(message);
  } catch (error) {
    if (request.signal.aborted) return;
    showRepairResult(error.status === 404 ? "Перезапустите локальную программу плоттера для восстановления контуров." : messageFrom(error));
  } finally {
    contourRepair = null;
    renderControls();
  }
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
  if (nestingBackend === "ready") setNestStatus("");
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

function applyNestingSettings() {
  const passLimit = $("nest-pass-limit");
  passLimit.hidden = !Number.isFinite(machine.rollPassLengthMm) || machine.rollPassLengthMm <= 0;
  if (!passLimit.hidden) passLimit.textContent = `Длина прохода по столу — до ${number(machine.rollPassLengthMm)} мм`;
  const width = $("nest-width");
  width.max = String(machine.height);
  width.placeholder = String(defaultFabricWidthMm());
  const savedWidth = nestingSettings?.widthMm;
  const oldDefault = nestingSettings?.widthExplicit == null && savedWidth === machine.height;
  width.value = !oldDefault && savedWidth != null ? savedWidth : defaultFabricWidthMm();
  $("nest-gap").value = nestingSettings?.gapMm ?? 10;
  $("nest-margin").value = nestingSettings?.marginMm ?? 10;
  $("nest-rotation").value = ["any", "quarter"].includes(nestingSettings?.rotation) ? "any" : "none";
}

async function applyNesting() {
  if (drawingLocked() || !nestDialog.open || nestingBackend !== "ready" || !$("nest-form").reportValidity()) return;
  const indices = [];
  const paths = [];
  const settings = {
    widthMm: $("nest-width").value ? $("nest-width").valueAsNumber : defaultFabricWidthMm(),
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
    for (const [index, path] of placed.entries()) {
      if (excluded.has(index)) continue;
      const rotation = drawingParts.get(path.partNumber)?.rotation;
      paths.push(rotation ? { ...path, partRotation: rotation } : path);
      indices.push(index);
    }
    if (!paths.length) return;
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
    const next = placed.slice();
    indices.forEach((index, i) => { next[index] = layout.paths[i]; });
    const moves = new Map(layout.placements.map((part) => [part.partNumber, part]));
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
    }
    rememberDrawing("автораскладку");
    placed = next;
    selectionAngle = 0;
    nestingSettings = { ...settings, widthExplicit: settings.widthMm !== defaultFabricWidthMm() };
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

function setSaveStatus(message, failed = false) {
  const status = $("workspace-save");
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle("failed", failed);
}

function workspaceSnapshot() {
  if (!source) return null;
  return {
    source: { filename: source.filename, warnings: source.warnings || [] },
    paths: placed,
    excluded: [...excluded],
    view: {
      zoom: camera && baseCamera ? camera.width / baseCamera.width : 1,
      focusX: camera ? camera.x + camera.width / 2 : 0,
      focusY: camera ? camera.y + camera.height / 2 : 0,
      grid: gridVisible,
      cameraMode,
      ...(previewCutPage ? { section: previewCutPage } : {}),
    },
    optimization: {
      route: routeApplied, routeApplied, join: $("optimize-join").checked,
      ...(nestingSettings ? { nesting: nestingSettings } : {}),
    },
  };
}

function scheduleWorkspaceSave() {
  if (restoringWorkspace || !source) return;
  workspaceChange++;
  clearTimeout(saveTimer);
  if (workspaceRevision === null) return;
  setSaveStatus("Сохраняем…");
  saveTimer = setTimeout(() => void persistWorkspace(), 250);
}

function persistWorkspace() {
  clearTimeout(saveTimer);
  if (workspaceRevision === null) return Promise.resolve(false);
  if (savePromise) return savePromise;
  if (workspaceSaved === workspaceChange) return Promise.resolve(true);
  savePromise = (async () => {
    try {
      while (workspaceSaved !== workspaceChange) {
        const change = workspaceChange;
        const response = await getJson("/api/workspace", jsonRequest({
          revision: workspaceRevision, workspace: workspaceSnapshot(),
        }, AbortSignal.timeout(30_000)));
        workspaceRevision = response.revision;
        workspaceSaved = change;
      }
      if (source) setSaveStatus("Сохранено");
      return true;
    } catch (error) {
      if (error.status === 409) workspaceRevision = null;
      setSaveStatus(error.status === 409 ? "Другая вкладка сохранила правки. Перезагрузите страницу." : "Не сохранено: " + messageFrom(error), true);
      return false;
    } finally {
      savePromise = null;
    }
  })();
  return savePromise;
}

async function restoreWorkspace() {
  try {
    const response = await getJson("/api/workspace", { signal: AbortSignal.timeout(15_000) });
    workspaceRevision = response.revision;
    const saved = response.workspace;
    if (!saved) return;
    source = saved.source;
    placed = saved.paths;
    excluded = new Set(saved.excluded);
    gridVisible = saved.view.grid;
    $("grid-tool").setAttribute("aria-pressed", String(gridVisible));
    routeApplied = saved.optimization.routeApplied ?? true;
    $("optimize-join").checked = saved.optimization.join;
    nestingSettings = saved.optimization.nesting || null;
    cameraMode = saved.view.cameraMode ?? "work";
    updateOptimizationControls();
    updateDrawing({ edited: false });
    previewCutPage = saved.view.section ?? null;
    if (baseCamera && cameraMode === "manual") {
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
    setSaveStatus(response.recovered ? "Восстановлено из копии" : "Сохранено");
  } catch (error) {
    setSaveStatus("Не удалось восстановить: " + messageFrom(error), true);
  } finally {
    restoringWorkspace = false;
    renderControls();
  }
}

function resetCutPlan() {
  clearTimeout(cutPlanTimer);
  cutPlanEpoch++;
  cutPlan = null;
  previewCutPage = null;
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
  if (source && includedBounds) cutPlanTimer = setTimeout(() => void requestCutPlan(), 250);
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
  if (cutPlan) return Promise.resolve(cutPlan);
  const epoch = cutPlanEpoch;
  if (cutPlanRequest?.epoch === epoch) return cutPlanRequest.promise;
  const includedIndices = placed.flatMap((_, index) => excluded.has(index) ? [] : [index]);
  if (!source || !includedIndices.length) return Promise.resolve(null);
  const promise = (async () => {
    try {
      const plan = await getJson("/api/cut-plan", jsonRequest({
        job: buildJob(), filename: source.filename,
      }, AbortSignal.timeout(30_000)));
      if (epoch !== cutPlanEpoch) return null;
      cutPlan = plan;
      if (plan.planId !== transferChecklistPlanId) {
        transferChecklistPlanId = plan.planId;
        resetTransferChecklist();
      }
      cutPlanError = plan.issues.map(({ message }) => message).join(" ");
      warningIndices = new Set(plan.issues.flatMap(({ indices }) => indices.map((index) => includedIndices[index])));
      sectionByPath = indexCutSections(plan, includedIndices);
      drawingProgressModel = makeDrawingProgressModel(plan, includedIndices);
      renderPaths();
      updateCanvasSection();
      renderRun();
      return plan;
    } catch (error) {
      if (epoch === cutPlanEpoch) cutPlanError = messageFrom(error);
      return null;
    } finally {
      if (cutPlanRequest?.epoch === epoch) cutPlanRequest = null;
      if (epoch === cutPlanEpoch) {
        renderControls();
      }
    }
  })();
  cutPlanRequest = { epoch, promise };
  return promise;
}

function updateDrawing({ edited = true } = {}) {
  cancelCanvasGesture();
  endFreeRotation();
  closePartMenu();
  if (edited) clearCurrentUpload();
  pathBoxes = placed.map((path) => bounds([path]));
  indexDrawingParts();
  selection = expandToWholeParts(selection);
  includedBounds = bounds(included());
  svg.classList.toggle("has-drawing", placed.length > 0);
  $("empty-canvas").hidden = placed.length > 0;
  const width = Math.min(nestingSettings?.widthMm ?? machine.height, machine.height);
  jobError = outsideMaterial(includedBounds)
    ? `Проверьте начало раскладки и ширину полотна по Y (${number(width)} мм).` : "";
  resetCutPlan();
  resizeCamera();
  updateSelection();
  if (edited) scheduleWorkspaceSave();
  renderControls();
  void identifyDrawingParts().catch(() => {});
}

async function loadDrawing(file) {
  if (!file || drawingLocked()) return;
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
    source = imported;
    placed = imported.paths.map((path) => translatePath(path, 20 - box.minX, 20 - box.minY));
    excluded = new Set();
    selection = new Set(placed.keys());
    selectionAngle = 0;
    routeApplied = true;
    drawingUndo.length = 0;
    updateDrawing();
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
  label.textContent = message;
  label.hidden = !message;
}

function busy() {
  return Boolean(pendingCommand || stopPending || activeHold);
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

function setClampLowered(lowered) {
  clampLowered = lowered;
  try {
    sessionStorage.setItem("plotter.clampLowered", String(lowered));
    sessionStorage.removeItem("plotter.manualFeed");
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
    sessionStorage.setItem("plotter.vacuumEnabled", String(enabled));
  } catch {
    // Browser storage is optional.
  }
}

function applyActuatorState(state) {
  if (!state || typeof state.epoch !== "string" || !Number.isInteger(state.revision) || state.revision < 0) return;
  if (state.epoch === actuatorEpoch && state.revision < actuatorRevision) return;
  actuatorEpoch = state.epoch;
  actuatorRevision = state.revision;
  if (typeof state.clampLowered === "boolean") setClampLowered(state.clampLowered);
  if (typeof state.vacuumEnabled === "boolean") setVacuumEnabled(state.vacuumEnabled);
}

function clearCurrentUpload() {
  currentUpload = null;
  $("job-verified").checked = false;
}

function resetTransferChecklist() {
  for (const id of ["manual-auto-run-off", "roll-advance-confirmed"]) $(id).checked = $(id).defaultChecked;
}

function updateControllerRoll(roll) {
  const previous = controllerRoll;
  if (roll?.state === "advance" && (roll.planId !== previous?.planId || roll.nextPage !== previous?.nextPage)) {
    resetTransferChecklist();
  }
  controllerRoll = roll;
}

function applyJobStatus(response) {
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

function operationAllowed(action, allowPending = false) {
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
  if (action !== "cut") return true;
  return Boolean(!jobError && !drawingLocked() && !simulation.active && currentUpload && cutPlan?.planId === currentUpload.planId
    && $("job-verified").checked);
}

function uploadPageNumber(plan = cutPlan) {
  if (!plan?.canUpload) return null;
  if (controllerRoll) return controllerRoll.planId === plan.planId ? controllerRoll.nextPage : null;
  const page = plan.mode === "roll" && machine.apiVersion >= 17 ? selectedCutPage : 1;
  return plan.pages[page - 1]?.canStart === false ? null : page;
}

function needsPagePositionConfirmation(page) {
  return Boolean(page && (cutPlan?.hasContinuations || controllerRoll?.state === "advance" || !controllerRoll && page > 1));
}

function needsManualAutoRunConfirmation() {
  return machine.directRoll === true && machine.autoRunReadback === false;
}

function renderPagePicker(roll, disabled) {
  const picker = $("job-page");
  const visible = Boolean(cutPlan?.mode === "roll" && cutPlan.canUpload && machine.apiVersion >= 17);
  $("job-page-field").hidden = !visible;
  if (visible && pagePickerPlan !== cutPlan.planId) {
    selectedCutPage = cutPlan.pages.find((page) => page.canStart !== false)?.number ?? 1;
    pagePickerPlan = cutPlan.planId;
    const total = cutPlan.pages.length;
    picker.replaceChildren(...cutPlan.pages.map((page) => new Option(
      `${page.number} из ${total}${page.canStart === false ? " · продолжение" : ""}`, page.number,
    )));
  }
  const next = visible ? roll?.nextPage ?? roll?.page ?? selectedCutPage : null;
  if (visible) {
    picker.value = String(next);
    for (const option of picker.options) {
      option.disabled = !controllerRoll && cutPlan.pages[Number(option.value) - 1]?.canStart === false;
    }
    picker.disabled = disabled || Boolean(controllerRoll);
    picker.title = controllerRoll ? "В открытом сеансе следующий проход выбирается по ходу раскроя" : "";
  }
  shownCutPage = next;
  updateCanvasSection();
}

function renderContinuationNote(roll, pageNumber) {
  const note = $("roll-continuation-note");
  const plan = cutPlan;
  note.hidden = !plan?.hasContinuations || !plan.canUpload || runInProgress()
    || Boolean(controllerRoll && !roll || roll?.state === "completed");
  if (note.hidden) return;
  const page = plan.pages[pageNumber - 1];
  const next = plan.pages[pageNumber];
  if (page?.canStart === false && (roll?.state === "advance" || roll?.state === "ready")) {
    const previous = plan.pages[pageNumber - 2];
    const feed = previous?.feedAfterMm;
    const alignment = page.incomingSeams?.length ? "Совместите точки стыка с X = 0 без поперечного сдвига."
      : "Совместите начало прохода с X = 0 без поперечного сдвига.";
    note.textContent = roll.state === "advance" && Number.isFinite(feed)
      ? `Протяните полотно на ${formatFeedDistance.format(feed)} мм. ${alignment}` : alignment;
  } else if (next?.canStart === false) {
    note.textContent = `Длинные детали продолжатся на проходе ${next.number}.`
      + (next.incomingSeams?.length ? " Точки стыка отмечены на чертеже." : "");
  } else {
    note.textContent = "Проходы с продолжением доступны после реза предыдущего прохода.";
  }
}

function uploadAllowed() {
  const autoRunOff = machine.autoRunReadback === true ? live.autoRunOff === true
    : needsManualAutoRunConfirmation() && live.autoRunOff !== false && $("manual-auto-run-off").checked;
  return machine.verifiedJobUpload === true && controlActive() && autoRunOff
    && !stopNeedsVerification && !coordinateMoveBlocksControl() && !jobError && !busy() && !drawingLocked() && !simulation.active && activeRun?.state !== "transferring"
    && live.responded && !live.busy && live.positionFresh && live.operations.upload !== false
    && Boolean(uploadPageNumber()) && !currentUpload && workspaceRevision !== null
    && (!needsPagePositionConfirmation(uploadPageNumber()) || $("roll-advance-confirmed").checked);
}

function renderLayoutStatistics() {
  const plan = cutPlan?.pages.length ? cutPlan : null;
  const empty = $("layout-empty");
  $("layout-statistics").hidden = !plan;
  empty.hidden = Boolean(plan);
  if (!plan) {
    empty.textContent = !source ? "Откройте чертёж для расчёта"
      : !includedBounds ? "Добавьте контуры к раскладке"
      : cutPlanError ? "Проверьте раскладку на канвасе" : "Рассчитываем раскладку…";
    return;
  }
  if (!simulation.active) layoutStatistics.setPlan(plan, false);
}

function renderControls() {
  const armed = controlActive();
  const locked = busy();
  const importing = drawingLocked();
  const inProgress = runInProgress();
  renderLayoutStatistics();
  if (importing) {
    closePartMenu();
    if (drag?.type === "move" || drag?.type === "rotate") cancelCanvasGesture();
    endFreeRotation();
  }
  $("import-button").disabled = $("canvas-import").disabled = importing;
  const editing = selectedBounds && !importing && drawingPartsReady;
  for (const id of [...placementFields, "rotate-button", "exclude-button", "delete-button"]) $(id).disabled = !editing;
  if (editing) {
    $("size-width").disabled = selectedBounds.maxY - selectedBounds.minY < 0.001;
    $("size-height").disabled = selectedBounds.maxX - selectedBounds.minX < 0.001;
  }
  $("select-all-tool").disabled = importing || !placed.length;
  const previous = drawingUndo.at(-1);
  $("contour-settings").disabled = importing || (!selectedBounds && !previous);
  $("drawing-undo").hidden = !previous;
  $("drawing-undo").title = previous ? `Отменить ${previous.label} (Ctrl+Z)` : "";
  $("drawing-undo").disabled = importing;
  const includedCount = placed.length - excluded.size;
  $("nest-button").disabled = importing || !includedCount;
  $("optimize-button").disabled = importing || !includedCount;
  if (nestDialog.open || optimizeDialog.open) updateOptimizationControls();

  const armButton = $("arm-button");
  armButton.disabled = armed ? Boolean(activeHold) || pendingCommand === "arm" : locked || !live.responded || live.busy || (!live.positionFresh && !runInProgress());
  armButton.textContent = pendingCommand === "arm" ? "Включаем…" : armed ? "Отключить управление" : "Включить управление";
  armButton.classList.toggle("armed", armed);
  armButton.setAttribute("aria-pressed", String(armed));
  $("motion-verification").hidden = !needsMotionVerification();
  $("verify-stop-button").disabled = !armed || locked || !live.responded || live.busy || machine.persistentControl !== true;
  $("motion-verification-status").textContent = machine.persistentControl === true
    ? "Остановка пока не подтверждена. Движение заблокировано."
    : "Обновите сервер плоттера, чтобы проверить остановку без отключения управления.";

  for (const button of holdButtons) {
    const held = activeHold?.button === button && !activeHold.released;
    const direction = button.dataset.controllerHold;
    const available = !stopNeedsVerification && !coordinateMoveBlocksControl() && live.positionFresh && live.hold[direction] === true;
    button.setAttribute("aria-pressed", String(held));
    button.disabled = !held && (!armed || locked || runInProgress() || activeRun?.state === "transferring" || !live.responded || live.busy || !available);
  }
  const pause = $("pause-button");
  pause.dataset.operation = activeRun?.state === "paused" ? "resume-cut" : "pause-cut";
  pause.querySelector("span").textContent = pause.dataset.operation === "resume-cut" ? "Продолжить" : "Пауза";
  pause.hidden = !inProgress;
  for (const button of operationButtons) button.disabled = !operationAllowed(button.dataset.operation);
  renderClamp();
  renderVacuum();

  $("simulation-button").disabled = importing || !includedBounds || Boolean(cutPlan && !cutPlan.pages.length);
  const uploadButton = $("controller-upload-button");
  const roll = controllerRoll?.planId === cutPlan?.planId ? controllerRoll : null;
  renderPagePicker(roll, importing || locked || Boolean(currentUpload));
  const uploadPage = uploadPageNumber();
  renderContinuationNote(roll, uploadPage ?? shownCutPage);
  const directRoll = cutPlan?.mode === "roll" && cutPlan.canUpload && machine.directRoll === true;
  const waitingForAdvance = roll?.state === "advance";
  const manualAutoRun = Boolean(uploadPage && !currentUpload && needsManualAutoRunConfirmation());
  const requiresAdvance = needsPagePositionConfirmation(uploadPage);
  const canResetRoll = Boolean(controllerRoll && ["completed", "cancelled", "unknown", "ready", "advance"].includes(controllerRoll.state));
  $("manual-auto-run").hidden = !manualAutoRun;
  $("manual-auto-run-off").disabled = importing || locked || !live.responded || live.busy;
  $("roll-advance").hidden = !requiresAdvance;
  $("roll-advance-confirmed").disabled = importing || locked || !armed || !live.responded || live.busy;
  if (requiresAdvance) {
    $("roll-advance-label").textContent = `Проверить начало прохода ${uploadPage} у нуля станка (X = 0)`;
  }
  $("roll-reset").hidden = !canResetRoll;
  $("roll-reset").disabled = !armed || locked || importing || live.busy || !live.positionFresh || stopNeedsVerification || coordinateMoveBlocksControl();
  $("roll-reset").textContent = roll && !cutPlan?.hasContinuations ? "Выбрать другой проход…" : "Завершить сеанс рулона";
  uploadButton.disabled = !uploadAllowed();
  if (uploadConfirmDialog.open) {
    if (inProgress || stopPending) closeUploadConfirmation();
    else $("upload-confirm-accept").disabled = !matchesUploadIntent(uploadConfirmation);
  }
  uploadButton.textContent = pendingCommand === "upload" ? "Загрузка…"
    : directRoll && uploadPage ? `Загрузить проход ${uploadPage}` : "Загрузить";
  const canDownloadXiao = cutPlan?.canPrepareXiao === true;
  const useXiao = canDownloadXiao && !currentUpload && !cutPlan.canUpload;
  $("job-prepare-actions").hidden = Boolean(currentUpload || inProgress || stopPending);
  $("job-title").closest(".job-section").classList.toggle("job-in-progress", inProgress || stopPending);
  const xiaoOpen = $("prepare-xiao-open");
  const xiaoDownload = $("prepare-xiao-download");
  uploadButton.hidden = useXiao;
  xiaoOpen.hidden = !useXiao || !machine.xiaoAvailable;
  xiaoOpen.disabled = importing || locked || Boolean(xiaoHandoff);
  xiaoDownload.disabled = importing || Boolean(xiaoHandoff);
  xiaoOpen.textContent = xiaoHandoff === "open" ? "Открываем…" : "Открыть в Xiao";
  xiaoDownload.textContent = xiaoHandoff === "download" ? "Готовим DXF…" : "Скачать DXF для Xiao";
  $("xiao-handoff").hidden = !canDownloadXiao;
  $("xiao-result").hidden = !xiaoResult;
  $("xiao-result").textContent = xiaoResult || "";
  const verificationRequired = Boolean(currentUpload);
  $("job-verification").hidden = !verificationRequired;
  $("job-verified").disabled = locked || !armed;
  $("job-verified-label").textContent = directRoll && currentUpload
    ? `Сверил проход ${currentUpload.page} из ${cutPlan.pages.length} и его положение на станке`
    : "Сверил деталь и её положение на станке";
  let status;
  if (stopPending) status = "Отправляем команду остановки…";
  else if (pendingCommand === "upload") status = "Передача чертежа…";
  else if (runInProgress()) status = runLabels[live.externalMotion || activeRun?.state] || "Проверьте штатный пульт";
  else if (stopNeedsVerification) status = "Ожидаем подтверждение остановки";
  else if (machine.verifiedJobUpload === false) status = "Перезапустите локальный сервер плоттера";
  else if (!source) status = "Откройте DXF или DWG";
  else if (!includedBounds) status = "Добавьте контур к заданию";
  else if (cutPlanError || jobError) status = cutPlanError || jobError;
  else if (!cutPlan) status = "Проверяем геометрию…";
  else if (controllerRoll && !roll) status = "На станке открыт другой рулон. Завершите его сеанс перед загрузкой.";
  else if (currentUpload) status = verificationRequired && !$("job-verified").checked
    ? "Сверьте проход на пульте и поставьте отметку. Затем нажмите «Старт» здесь."
    : "Проход загружен. Нажмите «Старт».";
  else if (roll?.state === "completed") status = `Раскрой завершён · ${roll.completedPages} из ${roll.pages - (roll.firstPage ?? 1) + 1} проходов`;
  else if (cutProgress?.planId === cutPlan.planId && cutProgress.state === "completed") status = "Раскрой завершён";
  else if (roll?.state === "cancelled" || roll?.state === "unknown") status = `Сверьте проход ${roll.page} и положение ткани перед продолжением.`;
  else if (waitingForAdvance) status = `Проход ${roll.page} завершён. Следующий проход — ${roll.nextPage}.`;
  else if (useXiao && cutPlan.uploadIssue) status = cutPlan.uploadIssue;
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
  else if (requiresAdvance && !$("roll-advance-confirmed").checked) status = "Отметьте проверку положения прохода для загрузки";
  else if (manualAutoRun || requiresAdvance) status = "Проверки подтвердите при загрузке";
  else if (machine.autoRunReadback === false) status = "Чтение Auto Run для платы ещё не настроено";
  else if (live.autoRunOff !== true) status = "Режим Auto Run не считан";
  else status = live.uploadUnavailableReason || "Можно загрузить";
  $("loaded-job").textContent = status;
  uploadButton.title = status;
  const identity = $("job-identity");
  identity.textContent = source?.filename || "";
  identity.title = identity.textContent;
  identity.hidden = !identity.textContent;
  const detail = $("job-detail");
  const dimensions = includedBounds
    ? `${number(includedBounds.maxY - includedBounds.minY)} × ${number(includedBounds.maxX - includedBounds.minX)} мм` : "";
  const passes = $("job-page-field").hidden && cutPlan?.pages.length ? ` · проходов: ${cutPlan.pages.length}` : "";
  detail.textContent = dimensions ? dimensions + passes : "";
  detail.hidden = !detail.textContent;
  $("run-acknowledge").hidden = !(runInProgress() && ["unknown", "error"].includes(activeRun?.state));
  $("run-acknowledge").disabled = !operationAllowed("acknowledge-run");
}

function renderClamp() {
  const button = $("clamp-button");
  const action = clampLowered ? "raise-clamp" : "clamp";
  const sending = pendingCommand === "clamp" ? "Опускаем…"
    : ["raise-clamp", "cancel-cut"].includes(pendingCommand) ? "Поднимаем…" : "";
  button.dataset.operation = action;
  button.querySelector(".material-action-label").textContent = sending || "Лапки";
  button.querySelector("use").setAttribute("href", clampLowered ? "#i-lift" : "#i-lower");
  button.setAttribute("aria-busy", String(Boolean(sending)));
  button.classList.toggle("lowered", clampLowered);
  button.setAttribute("aria-pressed", String(clampLowered));
  button.disabled = !operationAllowed(action);
  button.title = sending ? "Команда отправляется. Дождитесь завершения."
    : machine.clampControl !== true || !(machine.apiVersion >= 12) ? "Перезапустите сервер плоттера"
    : clampLowered ? "Лапки опущены. Поднять лапки — вакуум выключится, Z поднимется."
    : "Лапки подняты. Опустить лапки.";
}

function renderVacuum() {
  const button = $("vacuum-button");
  const action = vacuumEnabled ? "vacuum-off" : "vacuum";
  const sending = pendingCommand === "vacuum" ? "Включаем…"
    : ["vacuum-off", "raise-clamp", "cancel-cut"].includes(pendingCommand) ? "Выключаем…" : "";
  button.dataset.operation = action;
  button.querySelector(".material-action-label").textContent = sending || "Вакуум";
  button.setAttribute("aria-busy", String(Boolean(sending)));
  button.classList.toggle("enabled", vacuumEnabled);
  button.setAttribute("aria-pressed", String(vacuumEnabled));
  button.disabled = !operationAllowed(action);
  button.title = sending ? "Команда отправляется. Дождитесь завершения."
    : machine.independentVacuumControl !== true || !(machine.apiVersion >= 12) ? "Перезапустите сервер плоттера"
    : vacuumEnabled ? "Вакуум включён. Выключить вакуум." : "Вакуум выключен. Включить вакуум.";
}

function renderRun() {
  const run = activeRun;
  const partCounts = renderCutProgress();
  $("run-panel").hidden = !run && !cutProgress || !cutProgress && run?.state === "idle";
  if ($("run-panel").hidden) return;
  const state = live.externalMotion || cutProgress?.state || run?.state;
  const label = state === "advance" ? "Смена прохода" : runLabels[state] || "Сверьте пульт";
  if ($("run-state").textContent !== label) $("run-state").textContent = label;
  $("run-state").dataset.state = state || "";
  if (run) renderRunStatistics(run);
  const page = cutProgress ?? controllerRoll ?? run;
  $("run-page").textContent = page?.page ? `${page.state === "advance" ? page.page + 1 : page.page} из ${page.pages}` : "—";
  $("run-done").textContent = partCounts ? `${partCounts.count} из ${partCounts.total}` : "—";
  const index = cutProgress ? cutProgress.index : run?.fresh ? run.index : null;
  const limit = cutProgress?.indexLimit ?? run?.indexLimit;
  $("run-index").textContent = Number.isInteger(index) && Number.isInteger(limit) ? `${index} / ${limit}` : "—";
  $("run-update").textContent = Number.isFinite(run?.updatedAt)
    ? (run.fresh ? "Последний ответ " : "Данные устарели · ") + clockFormat.format(run.updatedAt)
    : "Подтверждённых данных пока нет";
  $("run-coordinates").textContent = lastPositionAt === null ? "Координаты —" : [
    ...["x", "y", "z", "w", "a"].map((axis) => {
      const coordinate = live.position?.[axis];
      return `${axis.toUpperCase()} ${Number.isFinite(coordinate) ? formatCoordinate.format(coordinate) : "—"}`;
    }),
    ...(!live.positionFresh ? ["устарели " + clockFormat.format(lastPositionAt)] : []),
  ].join(" · ");
  renderRunElapsed();
}

function renderRunElapsed() {
  if (!activeRun?.startedAt) {
    $("run-elapsed").textContent = "—";
    return;
  }
  const finish = activeRun.endedAt ?? (runInProgress() ? Date.now() : activeRun.updatedAt);
  $("run-elapsed").textContent = Number.isFinite(finish) ? formatDuration((finish - activeRun.startedAt) / 1000) : "—";
}

function renderLive() {
  const state = $("machine-state");
  const link = live.busy ? "busy" : live.responded ? "connected" : liveObserved ? "offline" : "";
  state.className = ("machine-state " + link).trim();
  state.title = live.message || "";
  $("machine-state-label").textContent = live.busy ? "Контроллер занят" : live.responded ? "На связи"
    : liveObserved ? "Нет связи" : "Проверяем связь";
  $("machine-address").textContent = live.ip || machine.ip;
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
  if (response.position && !response.busy) lastPositionAt = Date.now();
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
  try {
    const { machine: configuration, apiVersion, roll, cutProgress: progress } = await getJson("/api/status", { signal: AbortSignal.timeout(3500) });
    if (!configuration) return;
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
  } catch {
    // The live controller request reports local connectivity independently.
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
  liveReading = new Promise((resolve) => { finish = resolve; });
  try {
    const response = await getJson("/api/controller-live", jsonRequest({}, AbortSignal.timeout(3500)));
    if (epoch === liveEpoch && !busy()) applyLive(response);
  } catch (error) {
    if (epoch === liveEpoch && !busy()) applyLive({ responded: false, busy: error.status === 409, message: messageFrom(error) });
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
}

function applyPosition(response) {
  if (typeof response.motionInhibited === "boolean") stopNeedsVerification = response.motionInhibited;
  if (response.position) {
    live.position = response.position;
    live.positionFresh = true;
    lastPositionAt = Date.now();
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
      const detail = response.message || (unverified ? "Проверьте положение на штатном пульте."
        : response.phase === "expired" ? "Удержание прервано." : "");
      setMachineError([reason, detail].filter(Boolean).join(" "));
    }
    if (unverified) stopNeedsVerification = true;
    applyPosition(response);
  } catch {
    stopNeedsVerification = true;
    setMachineError("Ответ на остановку не получен. Проверьте станок на штатном пульте.");
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
    sessionStorage.setItem("plotter.runStatistics", JSON.stringify(trackedRun));
  } catch { /* Session storage is optional. */ }
}

async function runControllerOperation(action) {
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
  }
  if (action === "acknowledge-run") payload.jobVerified = true;
  setPending(action);
  try {
    // Let an existing read release the UDP channel before these commands.
    // Stop/pause commands must never wait behind polling.
    if (materialOperations.has(action)) {
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
    if (!response.sent && !response.acknowledged) {
      setMachineError(response.message || "Контроллер отклонил команду.");
    }
  } catch (error) {
    if (stopAtStart !== stopRevision) return;
    const rejected = error.status >= 400 && error.status < 500;
    if (action === "cut") clearCurrentUpload();
    const message = rejected ? messageFrom(error) : "Результат отправки неизвестен. Проверьте штатный пульт.";
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
  try {
    const payload = { action: "cancel-cut", ...(controlActive() ? { controlToken: control.token } : {}) };
    const response = await getJson("/api/controller-operation", jsonRequest(payload, AbortSignal.timeout(5000)));
    applyJobStatus(response);
    if (response.sent) {
      clearCurrentUpload();
      if (response.run?.state === "cancelling") live.externalMotion = null;
    } else {
      setMachineError(response.message || "Команда остановки отклонена. Используйте физический стоп.");
    }
  } catch (error) {
    setMachineError(`${messageFrom(error)} Используйте физический стоп.`);
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

function matchesUploadIntent(intent) {
  return Boolean(intent && uploadAllowed() && focusEpoch === intent.focus
    && control?.token === intent.token && cutPlan?.planId === intent.planId
    && uploadPageNumber() === intent.page && needsManualAutoRunConfirmation() === intent.checkAutoRun
    && needsPagePositionConfirmation(intent.page) === intent.checkZero);
}

function requestControllerUpload() {
  if (!uploadAllowed() || !focused()) return;
  cancelCanvasGesture();
  const page = uploadPageNumber();
  const intent = {
    planId: cutPlan.planId, page, token: control.token, focus: focusEpoch,
    checkAutoRun: needsManualAutoRunConfirmation(), checkZero: needsPagePositionConfirmation(page),
  };
  if (!intent.checkAutoRun && !intent.checkZero) {
    void uploadControllerDrawing(intent, false);
    return;
  }
  uploadConfirmation = intent;
  $("upload-confirm-target").textContent = cutPlan.mode === "roll"
    ? `Перед загрузкой прохода ${page} подтвердите:` : "Перед загрузкой подтвердите:";
  $("upload-confirm-auto-run").hidden = !intent.checkAutoRun;
  $("upload-confirm-zero").hidden = !intent.checkZero;
  $("upload-confirm-zero").textContent = `Начало прохода ${page} подведено к нулю станка (X = 0).`;
  $("upload-confirm-seam").hidden = !cutPlan.pages[page - 1]?.incomingSeams?.length;
  $("upload-confirm-accept").disabled = false;
  uploadConfirmDialog.showModal();
}

async function uploadControllerDrawing(intent, confirmed) {
  if (!matchesUploadIntent(intent) || !focused() || (intent.checkAutoRun || intent.checkZero) && !confirmed) return;
  const plan = cutPlan;
  const { page, focus, planId, token, checkAutoRun, checkZero } = intent;
  const payload = {
    planId, controlToken: token, page,
    autoRunOff: confirmed && checkAutoRun,
    advanceConfirmed: confirmed && checkZero,
  };
  clearCurrentUpload();
  setPending("upload");
  try {
    if (!await persistWorkspace() || !controlActive() || control?.token !== payload.controlToken || !focused()
        || focusEpoch !== focus || cutPlan?.planId !== payload.planId || uploadPageNumber(plan) !== page) {
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
    } else if (uploaded) {
      setMachineError("Сеанс закончился. Включите управление и загрузите текущий чертёж заново.");
    } else {
      setMachineError(response.uploaded !== true && response.message
        || "Передача текущего файла не подтверждена. Загрузите его заново.");
    }
  } catch (error) {
    const rejected = error.status >= 400 && error.status < 500;
    if (!rejected) uploadUncertain = true;
    setMachineError(rejected ? messageFrom(error) : "Результат передачи неизвестен. Проверьте задание на пульте.");
  } finally {
    renderRun();
    setPending(null);
  }
}

async function resetControllerRoll() {
  if (!controllerRoll || $("roll-reset").disabled || !focused()) return;
  const abandoning = cutPlan?.planId === controllerRoll.planId && cutPlan.hasContinuations && controllerRoll.state !== "completed";
  const message = abandoning
    ? "Завершить текущий сеанс? После сброса нельзя начать с прохода, который продолжает незавершённую деталь. Подтвердите, что станок остановлен."
    : "Завершить текущий сеанс рулона? Подтвердите, что станок остановлен. Перед следующей загрузкой подтяните начало выбранного прохода к нулю станка.";
  if (!window.confirm(message)) return;
  const { planId } = controllerRoll;
  const controlToken = control.token;
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

async function prepareXiaoDrawing(openInXiao) {
  if (xiaoHandoff || drawingLocked() || !cutPlan?.canPrepareXiao || (openInXiao && busy())) return;
  const { planId, filename } = cutPlan;
  xiaoHandoff = openInXiao ? "open" : "download";
  xiaoResult = null;
  renderControls();
  try {
    if (!await persistWorkspace() || cutPlan?.planId !== planId) {
      throw new Error("Дождитесь сохранения раскладки и повторите попытку.");
    }
    const response = await checkedFetch("/api/prepare-xiao", jsonRequest({ planId, openInXiao }, AbortSignal.timeout(30_000)));
    if (openInXiao) {
      const result = await response.json();
      xiaoResult = result.opened
        ? "Проверьте в Xiao размеры, рабочий ноль, SP4 и режим разбиения. Отправка и запуск выполняются в Xiao."
        : result.message || "Откройте сохранённый DXF в Xiao вручную.";
    } else {
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename.replace(/\.(dxf|dwg)$/i, "") + " — раскладка.dxf";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      xiaoResult = "DXF подготовлен. Откройте его в Xiao и проверьте размеры, рабочий ноль и SP4.";
    }
  } catch (error) {
    xiaoResult = messageFrom(error);
  } finally {
    xiaoHandoff = null;
    renderControls();
  }
}

async function playSimulation() {
  if (drawingLocked() || !includedBounds) return;
  cancelCanvasGesture();
  const plan = await requestCutPlan();
  if (drawingLocked() || !plan || !plan.pages.length) return;
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
  rotateSelection((selectionAngle + 90) % 360);
});
$("exclude-button").addEventListener("click", () => {
  if (!selection.size || drawingLocked() || !drawingPartsReady) return;
  const restore = [...selection].every((index) => excluded.has(index));
  rememberDrawing(restore ? "возврат контуров" : "исключение");
  excluded = new Set(excluded);
  for (const index of selection) {
    if (restore) excluded.delete(index);
    else excluded.add(index);
  }
  updateDrawing();
});
$("delete-button").addEventListener("click", () => {
  deleteSelection();
  contourDialog.close();
  svg.focus({ preventScroll: true });
});
$("select-all-tool").addEventListener("click", () => {
  setTool(false);
  selectAllContours();
  svg.focus({ preventScroll: true });
});
const contourDialog = $("contour-dialog");
const nestDialog = $("nest-dialog");
const optimizeDialog = $("optimize-dialog");
$("contour-settings").addEventListener("click", () => {
  cancelCanvasGesture();
  contourDialog.showModal();
});
$("contour-close").addEventListener("click", () => contourDialog.close());
$("nest-button").addEventListener("click", () => {
  cancelCanvasGesture();
  applyNestingSettings();
  nestDialog.showModal();
  void checkNestingBackend();
});
$("nest-cancel").addEventListener("click", () => {
  nestRequest?.abort();
  nestDialog.close();
});
nestDialog.addEventListener("cancel", () => nestRequest?.abort());
function openOptimization() {
  cancelCanvasGesture();
  $("optimize-route").checked = routeApplied;
  showRepairResult("");
  updateOptimizationControls();
  optimizeDialog.showModal();
}
$("optimize-button").addEventListener("click", openOptimization);
$("repair-contours").addEventListener("click", () => void restoreClosedContours());
$("optimize-cancel").addEventListener("click", () => {
  contourRepair?.abort();
  optimizeDialog.close();
});
optimizeDialog.addEventListener("cancel", () => contourRepair?.abort());
function updateOptimizationControls() {
  const locked = drawingLocked();
  const includedCount = placed.length - excluded.size;
  $("optimize-apply").disabled = locked || !includedCount;
  $("repair-contours").disabled = locked || !includedCount;
  const routeResult = $("optimize-result");
  const result = cutPlan?.routeOptimization;
  const outdated = Number.isInteger(machine.apiVersion) && machine.apiVersion < 14;
  $("repair-contours").disabled ||= outdated;
  routeResult.hidden = !outdated && (!result || !routeApplied || !Number.isFinite(result.beforeMm) || !Number.isFinite(result.afterMm));
  if (outdated) {
    routeResult.textContent = "Перезапустите локальную программу плоттера, чтобы рассчитать новый маршрут.";
  } else if (result) {
    const saved = result.beforeMm > 0 ? 100 * (result.beforeMm - result.afterMm) / result.beforeMm : 0;
    routeResult.textContent = `Холостой ход: ${number(result.beforeMm / 1000)} → ${number(result.afterMm / 1000)} м${saved > 0.05 ? ` · −${number(saved)} %` : ""}`;
  }
  $("nest-apply").disabled = locked || nestingBackend !== "ready" || placed.length === excluded.size;
  $("nest-apply").textContent = nestRequest ? "Рассчитываем…" : "Разложить на полотне";
  const closeLabel = nestRequest ? "Отменить расчёт" : "Закрыть";
  $("nest-cancel").title = closeLabel;
  $("nest-cancel").setAttribute("aria-label", closeLabel);
  for (const id of ["nest-width", "nest-gap", "nest-margin", "nest-rotation", "optimize-route", "optimize-join"]) {
    $(id).disabled = locked;
  }
  $("optimize-route").disabled ||= outdated;
  $("nest-show-blocking").disabled = locked;
  $("nest-try-rotation").disabled = locked;
}
$("optimize-apply").addEventListener("click", () => {
  if (!optimizeDialog.open) return;
  optimizeDialog.close();
  applyContourOptimization();
  scheduleWorkspaceSave();
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
  setTool(false);
  setSelection(new Set(nestBlocking));
  nestDialog.close();
  svg.focus({ preventScroll: true });
});
$("drawing-undo").addEventListener("click", () => {
  undoDrawingEdit();
  $("contour-close").focus();
});
$("import-button").addEventListener("click", () => $("file-input").click());
$("canvas-import").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => void loadDrawing(event.target.files[0]));
$("toast-close").addEventListener("click", () => { $("toast").hidden = true; });
$("arm-button").addEventListener("click", () => void armControl());
$("verify-stop-button").addEventListener("click", () => void verifyStop());
$("simulation-button").addEventListener("click", () => void playSimulation());
$("run-acknowledge").addEventListener("click", () => void runControllerOperation("acknowledge-run"));
$("job-verified").addEventListener("change", renderControls);
$("job-page").addEventListener("change", (event) => {
  selectedCutPage = Number(event.currentTarget.value);
  previewCutPage = null;
  resetTransferChecklist();
  renderControls();
});
$("manual-auto-run-off").addEventListener("change", renderControls);
$("roll-advance-confirmed").addEventListener("change", renderControls);
$("roll-reset").addEventListener("click", () => void resetControllerRoll());
$("controller-upload-button").addEventListener("click", requestControllerUpload);
$("upload-confirm-cancel").addEventListener("click", closeUploadConfirmation);
uploadConfirmDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeUploadConfirmation();
});
$("upload-confirm-accept").addEventListener("click", () => {
  const intent = uploadConfirmation;
  closeUploadConfirmation();
  void uploadControllerDrawing(intent, true);
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
  const key = holdKey(event);
  if (key && activeHold?.input.key === key) {
    event.preventDefault();
    void stopControllerHold();
  }
}, true);
function loseFocus() {
  focusEpoch++;
  liveEpoch++;
  closeUploadConfirmation();
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
    void persistWorkspace();
    event.preventDefault();
    event.returnValue = "";
  }
});
function resumeControlPolling() {
  scheduleLiveRead(0);
  if (control && machine.persistentControl === true) void renewControl();
}
window.addEventListener("focus", resumeControlPolling);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") resumeControlPolling();
  else {
    clearTimeout(liveTimer);
    loseFocus();
  }
});

$("select-tool").addEventListener("click", () => setTool(false));
$("pan-tool").addEventListener("click", () => setTool(true));
$("fit-tool").addEventListener("click", () => fitCamera("overview"));
$("zoom-reset").addEventListener("click", () => fitCamera());
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
$("zoom-in").addEventListener("click", () => zoom(1.25));
$("zoom-out").addEventListener("click", () => zoom(0.8));
$("grid-tool").addEventListener("click", () => {
  gridVisible = !gridVisible;
  $("grid-tool").setAttribute("aria-pressed", String(gridVisible));
  renderTable();
  scheduleWorkspaceSave();
});
svg.addEventListener("wheel", (event) => {
  event.preventDefault();
  const [x, y] = screenFraction(event);
  zoom(event.deltaY < 0 ? 1.15 : 0.87, x, y);
}, { passive: false });
function boxFromPoints([x1, y1], [x2, y2]) {
  return { minX: Math.min(x1, x2), minY: Math.min(y1, y2), maxX: Math.max(x1, x2), maxY: Math.max(y1, y2) };
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
  // A dangling notch is part of the clearance check but not part of a closed outline.
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

function prepareManualPlacement() {
  const moving = [];
  const fixed = [];
  for (const part of drawingParts.values()) {
    const paths = part.indices.filter((index) => !excluded.has(index)).map((index) => placed[index]);
    if (!paths.length) continue;
    const shape = { paths, box: bounds(paths), outlines: null, tree: null };
    (selection.has(part.indices[0]) ? moving : fixed).push(shape);
  }
  const margin = nestingSettings?.marginMm ?? 0;
  const materialWidth = nestingSettings?.widthMm ?? machine.height;
  // Leave room for the controller's transverse coordinate offset at the machine edge.
  const maxY = Math.min(materialWidth - margin, machine.height - 0.01);
  return { moving, fixed, maxY, margin, gap: Math.max(nestingSettings?.gapMm ?? 0, 0.002) };
}

function placementBoxesNear(a, b, distance) {
  return a.minX <= b.maxX + distance && b.minX <= a.maxX + distance
    && a.minY <= b.maxY + distance && b.minY <= a.maxY + distance;
}

function eachPlacementSegment(paths, visit) {
  for (const { points, closed } of paths) {
    for (let i = closed ? 0 : 1; i < points.length; i++) {
      const first = points.at(i - 1), last = points[i];
      if (visit(first, last, boxFromPoints(first, last)) === false) return false;
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

function checkManualPlacement(placement, transform, snap = 0) {
  const { moving, fixed, maxY, margin, gap } = placement;
  const reach = gap + snap;
  let nearest = null;
  let valid = true;
  for (const part of moving) {
    const paths = part.paths.map((path) => mapPath(path, transform));
    const box = bounds(paths);
    if (box.minX < margin - placementEpsilon || box.minY < margin - placementEpsilon
        || box.maxY > maxY + placementEpsilon) return { valid: false, nearest: null };
    let outlines;
    for (const other of fixed) {
      if (!placementBoxesNear(box, other.box, reach)) continue;
      if (!other.tree) {
        const segments = [];
        eachPlacementSegment(other.paths, (first, last, box) => segments.push({ first, last, box }));
        other.tree = placementSegmentTree(segments);
      }
      const clear = eachPlacementSegment(paths, (a, b, edgeBox) => visitPlacementSegments(other.tree, edgeBox, reach,
        ({ first, last }) => {
          const offset = placementSegmentOffset(a, b, first, last);
          if (offset[2] < (gap - placementEpsilon) ** 2) valid = false;
          if (snap && offset[2] > placementEpsilon ** 2 && offset[2] <= reach ** 2
              && (!nearest || offset[2] < nearest[2])) nearest = offset;
          return valid || snap > 0;
        }));
      if (!clear) return { valid: false, nearest: null };
      if (placementBoxesNear(box, other.box, 0)) {
        other.outlines ??= manualOutlines(other.paths);
        part.outlines ??= manualOutlines(part.paths);
        outlines ??= part.outlines.map((outline) => {
          const points = outline.points.map(transform);
          return { points, box: bounds([{ points }]) };
        });
        if (paths.some((path) => other.outlines.some((outline) => pointInPlacementOutline(path.points[0], outline)))
            || other.paths.some((path) => outlines.some((outline) => pointInPlacementOutline(path.points[0], outline)))) {
          return { valid: false, nearest: null };
        }
      }
    }
  }
  return { valid, nearest };
}

function previewConstrainedMove(gesture, dx, dy) {
  const translate = (x, y) => ([u, v]) => [u + x, v + y];
  const snap = Math.min(35, camera.width / svg.clientWidth * 10);
  const { valid, nearest } = checkManualPlacement(gesture.placement, translate(dx, dy), snap);
  if (nearest) {
    const [x, y, square] = nearest;
    const distance = Math.sqrt(square);
    const correction = gesture.placement.gap - distance;
    if (Math.abs(correction) <= snap) {
      const snapped = [dx + x * correction / distance, dy + y * correction / distance];
      if (checkManualPlacement(gesture.placement, translate(...snapped)).valid) {
        gesture.offset = snapped;
        previewMove(...snapped);
        return;
      }
    }
  }
  if (valid) {
    gesture.offset = [dx, dy];
    previewMove(dx, dy);
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

function partAt(event, point) {
  const index = contourAt(event, point);
  if (index !== null) return drawingParts.get(placed[index]?.partNumber);
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
  $("part-menu-title").textContent = `Деталь №${part.number}`;
  for (const button of partMenu.querySelectorAll("[data-part-rotation]")) {
    button.setAttribute("aria-checked", String(button.dataset.partRotation === part.rotation));
  }
  partMenu.hidden = false;
  partMenu.style.left = `${Math.max(6, Math.min(x, document.documentElement.clientWidth - partMenu.offsetWidth - 6))}px`;
  partMenu.style.top = `${Math.max(6, Math.min(y, document.documentElement.clientHeight - partMenu.offsetHeight - 6))}px`;
  partMenuButtons[0].focus({ preventScroll: true });
}

async function openPartMenuAtPointer(event) {
  const boxes = pathBoxes;
  const point = drawingPoint(event);
  try {
    await identifyDrawingParts();
    if (boxes === pathBoxes) openPartMenu(partAt(event, point), event.clientX, event.clientY);
  } catch (error) {
    notifyError(error.status === 404 ? "Для настроек поворота перезапустите программу плоттера." : messageFrom(error));
  }
}

for (const button of partMenuButtons) {
  button.addEventListener("click", () => {
    const part = drawingParts.get(menuPartNumber);
    closePartMenu(true);
    if (!part || drawingLocked()) return;
    if (button.dataset.rotatePart) {
      setTool(false);
      setSelection(new Set(part.indices));
      if (button.dataset.rotatePart === "free") startFreeRotation();
      else rotateSelection((selectionAngle + Number(button.dataset.rotatePart)) % 360);
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
  const index = partMenuButtons.indexOf(event.target);
  const last = partMenuButtons.length - 1;
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? last
      : (index + (event.key === "ArrowDown" ? 1 : last)) % partMenuButtons.length;
    partMenuButtons[next].focus();
  } else if (event.key === "Escape" || event.key === "Tab") {
    event.preventDefault();
    closePartMenu(true);
  }
});
document.addEventListener("pointerdown", (event) => {
  if (!partMenu.contains(event.target)) closePartMenu();
}, true);
window.addEventListener("resize", () => closePartMenu());
window.addEventListener("scroll", () => closePartMenu(), true);

svg.addEventListener("contextmenu", (event) => {
  if (!camera || !placed.length) return;
  event.preventDefault();
  if (!drawingLocked()) void openPartMenuAtPointer(event);
});
function drawMarquee(from, to, crossing) {
  const rect = $("selection-marquee");
  for (const [key, value] of Object.entries(svgBox(boxFromPoints(from, to)))) rect.setAttribute(key, value);
  rect.classList.toggle("crossing", crossing);
  rect.removeAttribute("hidden");
}

function previewSelection(transform) {
  const groups = $("path-layer").children;
  for (const index of selection) groups[index].setAttribute("transform", transform);
  $("part-label-layer").replaceChildren();
}

function previewMove(dx, dy) {
  const [x, y] = projectPoint(dx, dy);
  previewSelection(`translate(${x} ${y})`);
}

function endFreeRotation() {
  if (!freeRotation) return;
  freeRotation = null;
  renderFreeRotation();
}

function startFreeRotation() {
  if (!selectedBounds || !camera) return;
  const { x, y, width, height } = svgBox(selectedBounds);
  const cx = x + width / 2, cy = y + height / 2;
  const pad = 32 * camera.width / svg.clientWidth;
  const left = camera.x + pad, right = camera.x + camera.width - pad;
  const top = camera.y + pad, bottom = camera.y + camera.height - pad;
  const candidates = [[cx, y - pad], [x + width + pad, cy], [cx, y + height + pad], [x - pad, cy]];
  const handle = candidates.find(([u, v]) => u >= left && u <= right && v >= top && v <= bottom)
    ?? [Math.min(right, Math.max(left, cx)), Math.min(bottom, Math.max(top, y - pad))];
  freeRotation = { pivot: projectPoint(cx, cy), handle: projectPoint(...handle) };
  renderFreeRotation();
}

function renderFreeRotation() {
  const layer = $("rotation-layer");
  if (!freeRotation || !camera || drawingLocked()) {
    layer.replaceChildren();
    return;
  }
  const delta = drag?.type === "rotate" ? drag.angle : 0;
  const { pivot, handle } = freeRotation;
  const radians = delta * Math.PI / 180;
  const [x, y] = projectPoint(...pivot);
  const [u, v] = projectPoint(...rotationTransform(radians, pivot)(handle));
  const units = camera.width / svg.clientWidth;
  const hx = (u - x) / units, hy = (v - y) / units;
  const group = element("g", { class: "rotation-guide", transform: `translate(${x} ${y}) scale(${units})` });
  const title = element("title");
  title.textContent = "Потяните ручку для поворота. Esc закрывает инструмент.";
  const grip = element("g", { class: "rotation-handle", "data-rotation-handle": "", transform: `translate(${hx} ${hy})` });
  grip.append(title, element("circle", { r: 11 }), element("use", { href: "#i-turn-cw", x: -7, y: -7, width: 14, height: 14 }));
  const angle = (selectionAngle + delta) % 360;
  group.append(element("line", { x1: 0, y1: 0, x2: hx, y2: hy }), element("circle", { r: 4, class: "rotation-pivot" }),
    grip, rulerText(`${number(angle)}°`, hx, hy - 16, 13, "middle", "rotation-angle"));
  layer.replaceChildren(group);
}

function rotationPointerAngle([x, y]) {
  return Math.atan2(y - freeRotation.pivot[1], x - freeRotation.pivot[0]);
}

function previewFreeRotation(gesture, point) {
  const radians = rotationPointerAngle(point) - gesture.startAngle;
  const angle = Math.round(Math.atan2(Math.sin(radians), Math.cos(radians)) * 1800 / Math.PI) / 10;
  const transform = rotationTransform(angle * Math.PI / 180, freeRotation.pivot);
  if (!checkManualPlacement(gesture.placement, transform).valid) return;
  gesture.angle = angle;
  const [x, y] = projectPoint(...freeRotation.pivot);
  previewSelection(`rotate(${-angle} ${x} ${y})`);
  renderFreeRotation();
}

function cancelCanvasGesture() {
  const gesture = drag;
  if (!gesture) return;
  drag = null;
  svg.classList.remove("dragging", "moving-contours");
  $("selection-marquee").setAttribute("hidden", "");
  if (["move", "rotate"].includes(gesture.type) && gesture.moved) {
    const groups = $("path-layer").children;
    for (const index of selection) groups[index]?.removeAttribute("transform");
    renderPartLabels();
    if (gesture.type === "rotate") renderFreeRotation();
  }
  if (svg.hasPointerCapture(gesture.pointerId)) svg.releasePointerCapture(gesture.pointerId);
}

svg.addEventListener("pointerdown", (event) => {
  if (!camera || drag || !event.isPrimary) return;
  if (event.button === 0 && event.target.closest("[data-canvas-page]")) return;
  const panning = event.button === 1 || (event.button === 0 && panMode);
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
  if (rotating) Object.assign(drag, { startAngle: rotationPointerAngle(start), angle: 0, placement: prepareManualPlacement() });
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
      drawCamera();
    }
  } else if (drag?.pointerId === event.pointerId) {
    if (!drag.moved && Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) >= 4) {
      drag.moved = true;
      if (drag.type === "move") {
        if (!selection.has(drag.index)) setSelection(new Set([drag.index]));
        drag.placement = prepareManualPlacement();
        drag.offset = [0, 0];
        svg.classList.add("moving-contours");
      }
    }
    if (drag.moved) {
      const point = drawingPoint(event);
      if (drag.type === "move") previewConstrainedMove(drag, point[0] - drag.start[0], point[1] - drag.start[1]);
      else if (drag.type === "rotate") previewFreeRotation(drag, point);
      else drawMarquee(drag.start, point, event.clientX < drag.clientX);
    }
  }
});

svg.addEventListener("pointerup", (event) => {
  const gesture = drag;
  if (gesture?.pointerId !== event.pointerId) return;
  if (!drawingLocked() && gesture.moved && (gesture.type === "move" || gesture.type === "rotate")) {
    const point = drawingPoint(event);
    if (gesture.type === "move") previewConstrainedMove(gesture, point[0] - gesture.start[0], point[1] - gesture.start[1]);
    else previewFreeRotation(gesture, point);
  }
  cancelCanvasGesture();
  if (gesture.type === "pan") {
    scheduleWorkspaceSave();
    return;
  }
  if (drawingLocked()) return;
  if (gesture.type === "rotate") {
    if (!gesture.angle) return;
    const { pivot, handle } = freeRotation;
    const transform = rotationTransform(gesture.angle * Math.PI / 180, pivot);
    editSelection((path) => mapPath(path, transform), "поворот", (selectionAngle + gesture.angle) % 360);
    freeRotation = { pivot, handle: transform(handle) };
    renderFreeRotation();
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
});
document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || event.altKey || event.shiftKey || event.repeat || drawingLocked()) return;
  const target = event.target;
  if (!(target instanceof Element) || !target.closest(".drawing, #contour-dialog")
      || target.closest("input, select, textarea, [contenteditable]")) return;
  if (event.key === "Delete" && !event.ctrlKey && !event.metaKey && selection.size) {
    event.preventDefault();
    $("delete-button").click();
  } else if (event.key.toLowerCase() === "z" && (event.ctrlKey || event.metaKey) && drawingUndo.length) {
    event.preventDefault();
    undoDrawingEdit();
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
    if (drag) cancelCanvasGesture();
    else if (freeRotation) endFreeRotation();
    else setSelection(new Set());
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    setTool(false);
    selectAllContours();
    return;
  }
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
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


new ResizeObserver(resizeCamera).observe(svg);
renderLive();
void readConfiguration();
void restoreWorkspace();
setInterval(renderRunElapsed, 1000);
scheduleLiveRead(0);
