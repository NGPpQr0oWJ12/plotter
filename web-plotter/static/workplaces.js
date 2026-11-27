import { mountCadEditor } from "/designer-cad.js";
import { confirmAction } from "/confirm-action.js";
import { createLabelDesigner } from "/labels.js";

const $ = (id) => document.getElementById(id);
const labels = { folder: "Папка", drawing: "Чертёж", part: "Деталь", kit: "Комплект" };
const states = { pending: "Ожидает", active: "В работе", done: "Выполнен" };
const currentOrderGroups = [["active", "В работе"], ["pending", "Ожидают"], ["draft", "Черновики"]];
const orderStateRank = { active: 0, pending: 1, draft: 2, done: 3 };
const ordersPerPage = 10;
const kitPartDragType = "application/x-plotter-kit-part";

function registryIcon(kind) {
  const paths = {
    folder: "M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
    drawing: "M5 3h9l5 5v13H5ZM14 3v6h5m-11 8 3-5 4 5Z",
    part: "M4 7l4-3h9l3 4v12H4ZM4 14h3m10 0h3",
    kit: "m12 3 9 5-9 5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
    chevronDown: "m6 9 6 6 6-6",
    search: "M15.5 10a5.5 5.5 0 1 1-11 0 5.5 5.5 0 0 1 11 0Zm-1.6 3.9 5.6 5.6",
    filter: "M4 6h16l-6 7v5l-4 2v-7Z",
    check: "m5 12 4 4L19 6",
    open: "M3 18V6a2 2 0 0 1 2-2h4l2 3h7a2 2 0 0 1 2 2m-17 9 3-8h15l-3 9H5a2 2 0 0 1-2-1Z",
    view: "M2.5 12c2.1-3.6 5.4-6 9.5-6s7.4 2.4 9.5 6c-2.1 3.6-5.4 6-9.5 6s-7.4-2.4-9.5-6Zm12 0a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z",
    edit: "m4 16-.7 4.7 4.7-.7L20 8l-4-4Zm9.5-9.5 4 4",
    copy: "M9 8h11v13H9Zm6-3V3H4v13h2",
    cut: "M9 8a3 3 0 1 0-6-1 3 3 0 0 0 6 1Zm0 8a3 3 0 1 1-6 1 3 3 0 0 1 6-1ZM8 9l12 11M8 15 20 4",
    paste: "M9 5H5v16h14V5h-4M9 3h6v4H9Zm0 9h6m-6 4h6",
    trash: "M4 6h16M9 6V4h6v2M6 6l1 14h10l1-14m-8 4v6m4-6v6",
    layout: "M3 3h18v18H3Zm3 3h6v5H6Zm0 8h6v4H6Zm9-8h3v12h-3Z",
    sewing: "M4 3h10l5 5v13H4Zm10 0v5h5m-12 4h9m-9 4h2m2 0h2m2 0h1",
    zoom: "M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0Zm-2 4 5 5m-12-9h6m-3-3v6",
    close: "m6 6 12 12M18 6 6 18",
    analysis: "M4 4v16h16M8 16v-4m5 4V7m5 9v-6",
    clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 6v6l4 2",
    calendar: "M4 6h16v15H4ZM8 3v5m8-5v5M4 11h16m-12 4h3",
    material: "M4 18V7a3 3 0 0 1 6 0v11a3 3 0 0 1-6 0Zm6-10h10v13H7m-3-3a3 3 0 0 1 6 0",
    back: "m11 5-7 7 7 7M4 12h16",
    plus: "M12 5v14M5 12h14",
    more: "M4 12h2m5 0h2m5 0h2",
    orders: "M9 4H6a2 2 0 0 0-2 2v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V6a2 2 0 0 0-2-2h-3M9 3h6v4H9Zm2 8h5m-5 5h5m-8-5h.01M8 16h.01",
    labels: "M4 5h11l5 7-5 7H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm3 4v6m3-6v6m3-6v6m4-3h.01",
    cad: "M4 4h16v16H4ZM7 16l5-8 5 8ZM4 10H2m2 4H2m20-4h-2m2 4h-2",
    download: "M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4",
    upload: "M12 15V3m-5 5 5-5 5 5M4 17v4h16v-4",
    exit: "M10 4H4v16h6m3-8h8m-3-3 3 3-3 3",
    minimize: "M5 17h14",
    maximize: "M5 5h14v14H5Z",
    restore: "M8 5h11v11M5 8h11v11H5Z",
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon", "registry-icon");
  if (kind === "orders" || kind === "labels" || kind === "cad") svg.classList.add("registry-app-icon", "is-" + kind);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[kind]);
  svg.append(path);
  return svg;
}

function registryTrashIcon(filled) {
  const svg = registryIcon("trash");
  const shell = svg.firstElementChild;
  shell.classList.add("registry-trash-shell");
  shell.setAttribute("d", "M5.4 8.5h13.2l-1 11.1A1.5 1.5 0 0 1 16.1 21H7.9a1.5 1.5 0 0 1-1.5-1.4Z");
  const path = (data, className) => {
    const shape = document.createElementNS(svg.namespaceURI, "path");
    shape.setAttribute("d", data);
    if (className) shape.classList.add(className);
    else shape.setAttribute("fill", "none");
    svg.append(shape);
  };
  if (filled) {
    path("m5.7 7.5 2.8-3.4 3.6 1.4 1.1 3.8-3 3.3-3.8-1.2Zm6.8-1.4 3.8-2.7 3.1 2.9-1.7 4.9-3.6.9Zm-3.5 8.3 2.6-2 4.5 1.6-.7 3.9-4.6.8Z", "registry-trash-paper");
    path("m7.6 7.6 2 .1 1.1 2m4.4-3.3 1.2.7-.7 2.2m-3.6 5.2 1.6.5-.2 1.5");
  } else path("M4.2 8.5h15.6M9 6V4.5h6V6M9.8 12v5.5m4.4-5.5v5.5");
  return svg;
}

function el(tag, content = "", className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(content);
  return node;
}

function button(title, action, { primary = false, disabled = false, icon, iconOnly = false } = {}) {
  const node = el("button", iconOnly ? "" : title, `button${primary ? " primary" : ""}`);
  if (icon) node.prepend(registryIcon(icon));
  if (iconOnly) {
    node.classList.add("workplace-icon-action");
    node.setAttribute("aria-label", title);
    node.title = title;
  }
  node.type = "button";
  node.disabled = disabled;
  node.addEventListener("click", action);
  return node;
}

function enableCardOpen(card, label, onOpen) {
  const trigger = el("button", "", "workplace-card-trigger");
  trigger.type = "button";
  trigger.setAttribute("aria-label", label);
  card.classList.add("workplace-clickable-card");
  card.prepend(trigger);
  card.addEventListener("click", event => {
    const control = event.target.closest("button, a");
    if (!control || control === trigger) onOpen();
  });
}

function input(type, value = "", attrs = {}) {
  const node = document.createElement("input");
  node.type = type;
  node.value = value;
  return Object.assign(node, attrs);
}

function field(title, control) {
  const label = el("label", title, "workplace-field");
  label.append(control);
  return label;
}

function select(options, selected) {
  const node = document.createElement("select");
  for (const [id, label] of options) node.add(new Option(label, id));
  node.value = selected ?? "";
  return node;
}

function drawPartOutline(canvas, paths) {
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.clearRect(0, 0, canvas.width, canvas.height);
  let left = Infinity, bottom = Infinity, right = -Infinity, top = -Infinity;
  for (const path of paths) {
    for (const [x, y] of path.points) {
      left = Math.min(x, left); right = Math.max(x, right);
      bottom = Math.min(y, bottom); top = Math.max(y, top);
    }
  }
  if (!Number.isFinite(left)) return null;
  const scale = Math.min((canvas.width - 16) / (right - left || 1), (canvas.height - 16) / (top - bottom || 1));
  const centerX = (left + right) / 2, centerY = (bottom + top) / 2;
  const project = ([x, y]) => [canvas.width / 2 + (x - centerX) * scale, canvas.height / 2 - (y - centerY) * scale];
  for (const path of paths) {
    context.beginPath();
    path.points.forEach((point, index) => context[index ? "lineTo" : "moveTo"](...project(point)));
    if (path.closed) context.closePath();
    context.strokeStyle = path.notch ? "#284994" : "#17252d";
    context.stroke();
  }
  return project;
}

function progressPercent(done, total) {
  return total ? Math.floor(done * 100 / total) : 0;
}

const showDate = (value) => value.split("-").reverse().join(".");

function orderDates(order) {
  const dates = el("span", "", "workplace-order-dates");
  const item = el("span", el("span", "Поставлен", "workplace-order-label"));
  const date = order.placedOn ? el("time", showDate(order.placedOn)) : el("strong", "Дата не указана");
  if (order.placedOn) date.dateTime = order.placedOn;
  item.append(date);
  dates.append(registryIcon("calendar"), item);
  return dates;
}

function orderProduct(order) {
  return order?.designation || order?.modelName || "";
}

function orderContext(order) {
  const product = orderProduct(order);
  if (!Number.isFinite(order?.quantity) || (!product && order.quantity === 1)) return product;
  return [product, `${order.quantity} шт.`].filter(Boolean).join(" · ");
}

function orderState(order) {
  if (!order.published) return "draft";
  if (order.total > 0 && order.done === order.total) return "done";
  return order.active || order.done ? "active" : "pending";
}

function normalizeOrderSearch(text) {
  return text.toLocaleLowerCase("ru-RU").replaceAll("ё", "е");
}

const metricNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const metricDate = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });
const noMetric = "Нет данных";

function metricValue(value, unit, divisor = 1) {
  return Number.isFinite(value) ? `${metricNumber.format(value / divisor)} ${unit}` : noMetric;
}

function workDuration(seconds) {
  if (!Number.isFinite(seconds)) return noMetric;
  const total = Math.round(Math.max(0, seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const parts = [];
  if (hours) parts.push(`${hours} ч`);
  if (minutes) parts.push(`${minutes} мин`);
  if (total % 60 || !parts.length) parts.push(`${total % 60} с`);
  return parts.join(" ");
}

function workDelta(seconds) {
  if (!Number.isFinite(seconds)) return noMetric;
  if (Math.round(Math.abs(seconds)) === 0) return el("span", "По расчёту", "workplace-delta-even");
  const late = seconds > 0;
  return el("span", `${late ? "+" : "−"}${workDuration(Math.abs(seconds))} · ${late ? "дольше плана" : "быстрее плана"}`,
    late ? "workplace-delta-late" : "workplace-delta-early");
}

function workTimestamp(value) {
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return noMetric;
  const node = el("time", metricDate.format(ms));
  node.dateTime = value;
  node.title = new Date(ms).toLocaleString("ru-RU");
  return node;
}

function workMetrics(title, icon, values, headingTag = "h3") {
  const group = el("section", "", "workplace-metrics");
  const heading = el(headingTag, registryIcon(icon), "workplace-metrics-title");
  heading.append(title);
  const list = el("dl");
  for (const [label, value] of values) {
    const metric = el("div", el("dt", label));
    metric.append(el("dd", value ?? noMetric));
    list.append(metric);
  }
  group.append(heading, list);
  return group;
}

function materialValues(stats) {
  return [
    ["Полотно", metricValue(stats.lengthMm, "пог. м", 1000)],
    [stats.approximate ? "КИМ (прибл.)" : "КИМ", metricValue(stats.utilizationPercent, "%")],
    ["Рез", metricValue(stats.cutDistanceMm, "м", 1000)],
    ["Холостой ход", metricValue(stats.travelDistanceMm, "м", 1000)],
  ];
}

function workStats(stats = {}, { order = false, finished, draft = false } = {}) {
  const group = el("div", "", "workplace-stats");
  const headingTag = order ? "h3" : "h4";
  const material = workMetrics("Материал", "material", materialValues(stats), headingTag);
  const since = Date.parse(stats.startedAt);
  let actual = Number.isFinite(stats.actualSeconds) ? workDuration(stats.actualSeconds) : noMetric;
  if (!finished && !draft && !stats.finishedAt && Number.isFinite(since)) {
    actual = el("span", workDuration((Date.now() - since) / 1000));
    actual.dataset.workStarted = String(since);
  }
  const values = [
    ["Чистое расчётное", workDuration(stats.estimatedWorkSeconds)],
    [order ? "Первый старт" : "Начало", workTimestamp(stats.startedAt)],
    ["Закрытие", workTimestamp(stats.finishedAt)],
    [order ? "Факт наряда" : "Факт ткани", actual],
  ];
  if (order) values.push(["Сумма по тканям", workDuration(stats.fabricActualSeconds)]);
  values.push(["Отклонение от расчёта", workDelta(stats.deltaSeconds)]);
  group.append(material, workMetrics("Время", "clock", values, headingTag));
  const notes = [];
  if (order && stats.plannedCount > 0 && stats.plannedCount < stats.totalCount) {
    notes.push(`По ${stats.plannedCount} из ${stats.totalCount} тканей.`);
  } else if (!Number.isFinite(stats.estimatedWorkSeconds)) {
    notes.push(finished ? "Расчёт раскладки отсутствует." : "Ожидает раскладки.");
  }
  if (order && stats.timedCount > 0 && stats.timedCount < stats.totalCount) {
    notes.push(`Время записано для ${stats.timedCount} из ${stats.totalCount} тканей.`);
  }
  if (!finished && !draft && Number.isFinite(since)) notes.push("Отсчёт продолжается.");
  if (notes.length) group.append(el("p", notes.join(" "), "workplace-metric-note"));
  return group;
}

function localDate(value) {
  const two = number => String(number).padStart(2, "0");
  return `${value.getFullYear()}-${two(value.getMonth() + 1)}-${two(value.getDate())}`;
}

function defaultAnalysisPeriod() {
  const today = new Date();
  const first = new Date(today);
  first.setDate(first.getDate() - 29);
  return { from: localDate(first), to: localDate(today) };
}

const ROLE_STORAGE_KEY = "plotter.workplace";
const DESIGNER_ACCESS_KEY = "plotter.designerAccess";
const REGISTRY_POSITIONS_KEY = "plotter.registry.desktopPositions";
const REGISTRY_WINDOW_BOUNDS_KEY = "plotter.registry.windowBounds";
const CAD_PART_FOLDER_KEY = "plotter.registry.cadPartFolder";
const designerWorkspaceName = "Рабочая область конструктора";
const registryWindowEdges = ["n", "ne", "e", "se", "s", "sw", "w", "nw"];
const registryTrash = { id: "registry-trash", name: "Корзина", icon: "trash", kind: "trash" };
const ordersShortcut = { id: "desktop-orders", name: "Наряды", icon: "orders", kind: "app-orders", app: "orders" };
const labelsShortcut = { id: "desktop-labels", name: "Конструктор бирок", icon: "labels", kind: "app-labels", app: "labels" };
const registryCad = { id: "desktop-cad", name: "LaroccaCAD", icon: "cad", kind: "app-cad", app: "cad" };
const pinnedShortcuts = [ordersShortcut, labelsShortcut, registryCad];
const desktopShortcuts = [...pinnedShortcuts, registryTrash];
const findShortcut = id => desktopShortcuts.find(shortcut => shortcut.id === id);
const folderFilterOptions = [["", "Все записи"], ["folder", "Папки"], ["part", "Детали"],
  ["drawing", "Чертежи"], ["kit", "Комплекты"]];
const desktopClockFormat = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const folderColors = {
  yellow: ["Жёлтый", "#ffda80", "#94601f"],
  blue: ["Голубой", "#a6d9ff", "#2e6598"],
  green: ["Зелёный", "#acddba", "#396c4f"],
  pink: ["Розовый", "#f7bfce", "#995767"],
  violet: ["Фиолетовый", "#d8c4f3", "#74549c"],
  gray: ["Серый", "#d7dce3", "#657081"],
};
const folderSortOptions = [
  ["name", "Название А–Я"], ["reverse-name", "Название Я–А"],
  ["newest", "Сначала новые"], ["oldest", "Сначала старые"],
  ["updated", "Недавно изменённые"], ["updated-oldest", "Давно изменённые"],
  ["type", "Тип А–Я"], ["reverse-type", "Тип Я–А"],
];
const registryNameOrder = new Intl.Collator("ru-RU", { numeric: true, sensitivity: "base" });
const registryDate = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });
const roleNames = { operator: "Оператор", designer: "Конструктор" };

const registryEntryType = entry => entry.model ? "Изделие" : labels[entry.kind];

function applyFolderColor(node, color) {
  const [, fill, stroke] = Object.hasOwn(folderColors, color) ? folderColors[color] : folderColors.yellow;
  node.classList.add("registry-colored-folder");
  node.style.setProperty("--registry-folder-fill", fill);
  node.style.setProperty("--registry-folder-stroke", stroke);
}

function compareRegistryEntries(a, b, sort) {
  const byName = registryNameOrder.compare(a.name, b.name);
  if (sort === "reverse-name") return -byName;
  if (sort === "type" || sort === "reverse-type") {
    const byType = registryNameOrder.compare(registryEntryType(a), registryEntryType(b));
    return (sort === "reverse-type" ? -byType : byType) || byName;
  }
  if (sort === "name") return byName;
  const field = sort === "updated" || sort === "updated-oldest" ? "updatedAt" : "createdAt";
  const dateA = Date.parse(a[field]), dateB = Date.parse(b[field]);
  if (!Number.isFinite(dateA)) return Number.isFinite(dateB) ? 1 : byName;
  if (!Number.isFinite(dateB)) return -1;
  return (sort === "oldest" || sort === "updated-oldest" ? dateA - dateB : dateB - dateA) || byName;
}

function registryDateCell(value) {
  const date = Date.parse(value);
  if (!Number.isFinite(date)) return el("td", "—");
  const time = el("time", registryDate.format(date));
  time.dateTime = value;
  return el("td", time);
}

function storedRegistryPositions(key, dimensions) {
  try {
    const values = JSON.parse(localStorage.getItem(key));
    return new Map(Object.entries(values ?? {}).filter(([, cell]) => Array.isArray(cell) && cell.length === dimensions
      && cell.every(position => Number.isInteger(position) && position >= 0 && position < 10000)));
  } catch {
    return new Map();
  }
}

function saveRegistryPositions(key, positions) {
  try { localStorage.setItem(key, JSON.stringify(Object.fromEntries(positions))); }
  catch { /* The desktop keeps this geometry in memory when storage is unavailable. */ }
}

export function mountWorkplaces({ request, navigateLayout, addRegisteredParts, finishRegisteredCut, releaseRegisteredCut, openFile, flushLayout,
  getActiveCut, onExit, layoutWidths }) {
  const page = $("workplace-page");
  const dock = $("workplace-dock");
  const xiaoActions = $("xiao-handoff");
  const printerActions = $("label-actions");
  const picker = $("role-picker");
  const pickerStatus = $("role-picker-status");
  const topbar = $("workplace-topbar");
  const roleLabel = $("workplace-role");
  const exit = $("workplace-exit");
  const defaultPageTitle = document.title;
  const status = el("p", "", "workplace-status");
  status.setAttribute("role", "alert");
  const panel = el("div", "", "workplace-content");
  page.replaceChildren(panel, status);
  const modal = el("dialog", "", "plotter-dialog workplace-dialog");
  modal.setAttribute("aria-labelledby", "workplace-dialog-title");
  document.body.append(modal);
  let entries = [];
  let sketchupDesign = false;
  let entriesById = new Map();
  let registryTrashFilled = null;
  let trashRead = 0;
  const registryPositions = storedRegistryPositions(REGISTRY_POSITIONS_KEY, 2);
  const registryWindowBounds = storedRegistryPositions(REGISTRY_WINDOW_BOUNDS_KEY, 4);
  const registryWindows = new Map();
  const registrySelection = new Set();
  let registryClipboard = null;
  let registryDispose = null;
  let registryWindowOrder = 0;
  let tab = null;
  let designerOrderView = "orders";
  let labelDesigner = null;
  let layoutCad = null;
  const cadInstances = () => [layoutCad, registryWindows.get(registryCad.id)?.cadEditor?.instance].filter(Boolean);
  let cadPartFolder;
  try { cadPartFolder = localStorage.getItem(CAD_PART_FOLDER_KEY); }
  catch { /* The current editor can remember the folder without browser storage. */ }
  let selectedOrder = null;
  const orderList = { query: "", filter: "", page: 1 };
  let analysisPeriod = defaultAnalysisPeriod();
  let analysisDeltaDescending = true;
  let analysisSeries = "fabricMetersPerHour";
  let workClock = 0;
  let currentLayout = null;
  let renderEpoch = 0;
  let orderRenderEpoch = 0;
  let pending = false;

  const post = (url, payload) => request(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const get = (url, params = {}) => request(`${url}?${new URLSearchParams(params)}`);
  const find = (id) => entriesById.get(id);
  const folders = () => entries.filter(entry => entry.kind === "folder");

  function trailOf(entry) {
    const trail = [];
    const seen = new Set();
    for (let current = entry; current && !seen.has(current.id); current = find(current.parent)) {
      seen.add(current.id);
      trail.push(current);
    }
    return trail.reverse();
  }

  function pathOf(entry) {
    return entry ? trailOf(entry).map(item => item.name).join(" / ") : designerWorkspaceName;
  }

  function folderSelect(value) {
    return select([["", designerWorkspaceName], ...folders().map(folder => [folder.id, pathOf(folder)])], value);
  }

  async function readRegistryTrash() {
    const read = ++trashRead;
    const { entries } = await get("/api/registry", { mode: "trash" });
    if (read === trashRead) registryTrashFilled = entries.length > 0;
    return entries;
  }

  async function refreshRegistry() {
    const [registry] = await Promise.all([get("/api/registry"), readRegistryTrash().catch(() => {})]);
    entries = registry.entries;
    sketchupDesign = registry.sketchupDesign === true;
    entriesById = new Map(entries.map(entry => [entry.id, entry]));
    for (const id of registrySelection) if (!find(id)) registrySelection.delete(id);
    for (const id of registryWindows.keys()) {
      const kind = find(id)?.kind;
      if (!findShortcut(id) && kind !== "folder" && kind !== "kit") registryWindows.delete(id);
    }
  }

  function closeRegistryView() {
    registryDispose?.();
    registryDispose = null;
    if (page.classList.contains("is-registry-desktop")) {
      document.title = defaultPageTitle;
      page.classList.remove("is-registry-desktop");
    }
  }

  function execute(action) {
    return async (...args) => {
      if (pending) return;
      pending = true;
      status.textContent = "";
      pickerStatus.hidden = true;
      try { await action(...args); } catch (error) {
        if (modal.open) modal.querySelector(".workplace-dialog-error").textContent = error.message;
        else if (currentLayout) dock.querySelector(".workplace-dock-error").textContent = error.message;
        else if (!picker.hidden) {
          pickerStatus.textContent = error.message;
          pickerStatus.hidden = false;
        }
        else status.textContent = error.message;
      } finally { pending = false; }
    };
  }

  function formDialog(title, content, submit, save, { lockWhileSaving = false } = {}) {
    const heading = el("h2", title);
    heading.id = "workplace-dialog-title";
    const header = el("header", heading, "drawing-dialog-heading");
    const form = el("form");
    const body = el("div", content, "drawing-dialog-body workplace-dialog-body");
    const error = el("p", "", "workplace-dialog-error");
    error.setAttribute("role", "alert");
    body.append(error);
    const cancel = button("Отмена", () => modal.close());
    const footer = el("footer", cancel, "dialog-actions");
    const action = el("button", submit, "button primary");
    action.type = "submit";
    footer.append(action);
    form.append(body, footer);
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (action.disabled) return;
      error.textContent = "";
      action.disabled = true;
      const preventCancel = event => event.preventDefault();
      if (lockWhileSaving) {
        cancel.disabled = true;
        modal.addEventListener("cancel", preventCancel);
      }
      try {
        await save();
        if (modal.contains(form)) modal.close("saved");
      } catch (failure) { error.textContent = failure.message; }
      finally {
        action.disabled = false;
        if (lockWhileSaving) {
          cancel.disabled = false;
          modal.removeEventListener("cancel", preventCancel);
        }
      }
    };
    modal.replaceChildren(header, form);
    modal.returnValue = "";
    if (!modal.open) modal.showModal();
  }

  function workHeading(title, detail = "", actions = []) {
    const heading = el("header", "", "workplace-heading");
    const titles = el("div", el("h1", title));
    if (detail) titles.append(el("p", detail));
    heading.append(titles, el("div", "", "workplace-actions"));
    heading.lastChild.append(...actions);
    return heading;
  }

  function itemPicker(kinds, previous = [], { mode = "list", folder = null } = {}) {
    const cards = mode !== "list";
    const fabric = mode === "fabric";
    const kitMode = mode === "kit";
    const catalogLabel = fabric ? "Детали и комплекты" : "Каталог деталей";
    const compositionLabel = fabric ? "Состав ткани" : "Детали в комплекте";
    const destination = fabric ? "в состав ткани" : "в комплект";
    const chosen = new Map(previous.map(item => [item.id, item.quantity]));
    const section = el("div", "", `workplace-picker${cards ? " registry-kit-picker" : ""}${fabric ? " workplace-fabric-picker" : ""}`);
    const browser = el("div", "", "workplace-picker-browser");
    const search = input("search", "", { placeholder: kitMode ? "Название детали" : "Название или путь к папке" });
    const results = el("div", "", "workplace-picker-results");
    const count = el("small", "", cards ? "registry-picked-count" : "");
    const selected = el("div", "", "registry-picked-list");
    const composition = cards ? el("section", el("h3", compositionLabel), "registry-picked") : null;
    const notice = cards ? el("span", "", "visually-hidden registry-kit-notice") : null;
    const catalogNodes = new Map();
    const quantities = new Map();
    const missingEntries = new Map();
    let dragging = null;
    const directory = kitMode ? null : folderSelect(null);
    if (directory) directory.options[0].textContent = "Все папки";
    const inFolder = (entry) => kitMode ? entry.parent === folder
      : !directory.value || trailOf(find(entry.parent)).some(parent => parent.id === directory.value);
    function itemText(entry) {
      const text = el("span", el("strong", entry.name), cards ? "registry-node-text" : "");
      const path = pathOf(find(entry.parent));
      if (fabric) {
        const meta = el("small", el("span", labels[entry.kind], "registry-kind"), "workplace-fabric-meta");
        meta.append(el("span", path));
        text.append(meta);
      } else if (!kitMode || entry.parent !== folder) {
        text.append(el("small", cards ? path : `${labels[entry.kind]} · ${path}`));
      }
      return text;
    }
    function amountInput(entry, value, disabled = false) {
      const amount = input("number", value, { min: 1, max: 50000, required: true, disabled,
        ariaLabel: `Количество: ${entry.name}` });
      amount.oninput = () => chosen.set(entry.id, amount.valueAsNumber);
      return amount;
    }

    function clearDrag() {
      dragging?.node.classList.remove("is-dragging");
      dragging = null;
      browser.classList.remove("is-drop-target");
      composition.classList.remove("is-drop-target");
    }

    function kitNode(entry, add) {
      const variant = add ? "registry-kit-choice" : "registry-picked-row";
      const node = el("div", "", `workplace-pick-row registry-kit-node ${variant}`);
      node.dataset.kind = entry.kind;
      node.setAttribute("role", "listitem");
      const grip = el("span", "", "registry-node-grip");
      grip.setAttribute("aria-hidden", "true");
      node.append(grip, itemText(entry));
      node.draggable = true;
      node.ondragstart = event => {
        if (!node.draggable || !event.dataTransfer || event.target.closest("input, button")) {
          event.preventDefault();
          return;
        }
        clearDrag();
        dragging = { entry, add, node };
        event.dataTransfer.setData(kitPartDragType, entry.id);
        event.dataTransfer.effectAllowed = add ? "copy" : "move";
        node.classList.add("is-dragging");
      };
      node.ondragend = clearDrag;
      return node;
    }

    function kitTools(entry, ...buttons) {
      const tools = el("div", "", "registry-node-tools");
      if (!fabric && entry.kind === "part" && entry.revision != null) tools.append(partViewButton(entry));
      tools.append(...buttons);
      return tools;
    }

    function syncCatalogNode(id) {
      const card = catalogNodes.get(id);
      if (!card) return;
      const included = chosen.has(id);
      card.node.classList.toggle("is-in-kit", included);
      card.node.draggable = !included;
      card.add.hidden = included;
      card.state.hidden = !included;
    }

    function changeKit(entry, add, restoreFocus = false) {
      if (chosen.has(entry.id) === add) return;
      if (add) chosen.set(entry.id, entry.patternQuantity || 1);
      else chosen.delete(entry.id);
      syncCatalogNode(entry.id);
      drawChosen();
      notice.textContent = add ? `Добавлено ${destination}: «${entry.name}».` : `Убрано: «${entry.name}».`;
      if (restoreFocus) {
        const target = add ? quantities.get(entry.id) : catalogNodes.get(entry.id)?.add || search;
        target?.focus();
      }
    }

    function enableKitDrop(target, add) {
      const accepts = event => dragging?.add === add && event.dataTransfer?.types.includes(kitPartDragType);
      target.ondragover = event => {
        if (!accepts(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = add ? "copy" : "move";
        target.classList.add("is-drop-target");
      };
      target.ondragleave = event => {
        if (!target.contains(event.relatedTarget)) target.classList.remove("is-drop-target");
      };
      target.ondrop = event => {
        if (!accepts(event)) return;
        event.preventDefault();
        const { entry } = dragging;
        const dropped = event.dataTransfer.getData(kitPartDragType);
        clearDrag();
        if (dropped === entry.id) changeKit(entry, add);
      };
    }

    const removeLabel = entry => `Убрать ${entry.name} ${fabric ? "из состава ткани" : "из комплекта"}`;

    function chosenEntry(id) {
      const existing = find(id) ?? missingEntries.get(id);
      if (existing) return existing;
      const entry = { id, name: "Недоступная деталь", kind: "part" };
      missingEntries.set(id, entry);
      void get("/api/registry", { id }).then(detail => {
        if (detail?.id !== id || !kinds.includes(detail.kind) || !chosen.has(id)) return;
        const { name, kind, parent } = detail;
        Object.assign(entry, { name, kind, parent });
        const amount = quantities.get(id);
        if (!amount?.isConnected) return;
        const row = amount.parentElement;
        row.dataset.kind = kind;
        row.querySelector(".registry-node-text").replaceWith(itemText(entry));
        amount.setAttribute("aria-label", `Количество: ${name}`);
        row.querySelector(".registry-node-action").setAttribute("aria-label", removeLabel(entry));
      }, () => {});
      return entry;
    }

    function drawChosen() {
      count.textContent = `Выбрано: ${chosen.size}`;
      if (!cards) return;
      quantities.clear();
      selected.replaceChildren();
      for (const [id, quantity] of chosen) {
        const entry = chosenEntry(id);
        const row = kitNode(entry, false);
        const amount = amountInput(entry, quantity);
        quantities.set(id, amount);
        const remove = button("Убрать", () => changeKit(entry, false, true));
        remove.classList.add("registry-node-action");
        remove.setAttribute("aria-label", removeLabel(entry));
        row.append(amount, kitTools(entry, remove));
        selected.append(row);
      }
      if (!selected.children.length) {
        const empty = fabric ? "Добавьте детали или комплекты из каталога" : "Перетащите сюда детали из каталога";
        selected.append(el("div", el("strong", empty), "registry-kit-empty"));
      }
    }

    function drawMatches() {
      const needle = normalizeOrderSearch(search.value.trim());
      catalogNodes.clear();
      results.replaceChildren();
      for (const entry of entries) {
        if (!kinds.includes(entry.kind) || !inFolder(entry)
            || !normalizeOrderSearch(kitMode ? entry.name : pathOf(entry)).includes(needle)) continue;
        if (cards) {
          const node = kitNode(entry, true);
          const add = button("Добавить", () => changeKit(entry, true, true));
          add.classList.add("registry-node-action");
          add.setAttribute("aria-label", `Добавить ${entry.name} ${destination}`);
          const state = el("span", fabric ? "В составе" : "В комплекте", "registry-node-state");
          node.append(kitTools(entry, add, state));
          catalogNodes.set(entry.id, { node, add, state });
          syncCatalogNode(entry.id);
          results.append(node);
          continue;
        }
        const row = el("label", "", "workplace-pick-row");
        const toggle = input("checkbox", "", { checked: chosen.has(entry.id) });
        const amount = amountInput(entry, chosen.get(entry.id) ?? 1, !toggle.checked);
        toggle.onchange = () => {
          amount.disabled = !toggle.checked;
          if (toggle.checked) chosen.set(entry.id, amount.valueAsNumber);
          else chosen.delete(entry.id);
          drawChosen();
        };
        row.append(toggle, itemText(entry), amount);
        results.append(row);
      }
      if (!results.children.length) results.append(el("p", "Подходящих записей нет.", "workplace-empty"));
    }
    search.oninput = drawMatches;
    if (cards) {
      browser.setAttribute("role", "region");
      browser.setAttribute("aria-label", catalogLabel);
      results.setAttribute("role", "list");
      browser.append(el("h3", catalogLabel));
    }
    if (directory) {
      directory.onchange = drawMatches;
      browser.append(field("Фильтр по папке", directory));
    }
    browser.append(field(kitMode ? "Поиск по названию" : "Поиск по реестру", search), results);
    section.append(browser);
    if (cards) {
      composition.setAttribute("aria-label", compositionLabel);
      selected.setAttribute("role", "list");
      notice.setAttribute("role", "status");
      composition.append(count, selected);
      section.append(composition, notice);
      enableKitDrop(browser, false);
      enableKitDrop(composition, true);
    } else section.append(count);
    drawMatches();
    drawChosen();
    return {
      section,
      values: () => Array.from(chosen, ([id, quantity]) => ({ id, quantity })),
      refresh: drawMatches,
    };
  }

  function createKitDialog(parentId) {
    const name = input("text", "", { required: true, maxLength: 255 });
    const picker = itemPicker(["part"], [], { mode: "kit", folder: parentId });
    const fields = el("div", field("Название", name), "workplace-form-grid");
    fields.append(picker.section);
    formDialog(labels.kit, fields, "Сохранить", async () => {
      await post("/api/registry", { action: "save", kind: "kit", name: name.value, parent: parentId, members: picker.values() });
      await render();
    });
  }

  function renameRegistryEntry(entry) {
    const name = input("text", entry.name, { required: true, maxLength: 255, autofocus: true });
    formDialog(`Переименовать «${entry.name}»`, field("Название", name), "Сохранить", async () => {
      await post("/api/registry", { action: "rename", id: entry.id, revision: entry.revision, name: name.value });
      await render();
    }, { lockWhileSaving: true });
    name.select();
  }

  async function deleteRegistryEntry(entry) {
    if (!await confirmAction({
      title: `Переместить «${entry.name}» в корзину?`,
      message: entry.kind === "folder" ? "Папку и её содержимое можно будет восстановить из корзины."
        : "Запись можно будет восстановить из корзины.",
      confirmLabel: "В корзину",
    })) return;
    await post("/api/registry", { action: "trash", id: entry.id, revision: entry.revision });
    await render();
    focusWorkplaceHeading();
  }

  async function kitLayoutDialog(entry) {
    const kit = await get("/api/registry", { id: entry.id });
    const layouts = kit.layouts ?? [];
    const previous = layouts.find(layout => layout.valid && layout.quantity === 1)
      ?? layouts.find(layout => layout.valid) ?? layouts[0];
    const { defaultWidthMm, maxWidthMm } = layoutWidths?.() ?? {};
    const width = input("number", previous?.widthMm ?? defaultWidthMm ?? "", {
      min: 1, max: maxWidthMm ?? "", step: "any", required: true,
    });
    const quantity = input("number", previous?.quantity ?? 1, { min: 1, max: 50000, required: true });
    const content = el("div", el("p", kit.name, "workplace-form-note"), "workplace-form-grid");
    if (layouts.length > 1) {
      const saved = select([["", "Выбрать раскладку"], ...layouts.map((layout, index) => [index,
        `${layout.widthMm} мм · ${layout.quantity} компл.${layout.valid ? "" : " · состав изменён"}`])], layouts.indexOf(previous));
      saved.onchange = () => {
        if (!saved.value) return;
        const layout = layouts[Number(saved.value)];
        width.value = layout.widthMm;
        quantity.value = layout.quantity;
      };
      width.oninput = quantity.oninput = () => {
        const match = layouts.findIndex(layout => layout.widthMm === width.valueAsNumber && layout.quantity === quantity.valueAsNumber);
        saved.value = match >= 0 ? String(match) : "";
      };
      content.append(field("Сохранённые раскладки", saved));
    }
    content.append(field("Рабочая ширина ткани, мм", width), field("Количество комплектов", quantity));
    formDialog("Раскладка комплекта", content, "Открыть раскладку", () => showLayout({ kitId: kit.id, name: kit.name, mode: "kit",
      widthMm: width.valueAsNumber, quantity: quantity.valueAsNumber }));
  }

  async function kitCompositionDialog(id) {
    const kit = await get("/api/registry", { id });
    const rows = await Promise.all(kit.members.map(async ({ id, quantity }) => {
      const part = find(id) ?? await get("/api/registry", { id }).catch(() => null);
      const row = el("li");
      row.append(part?.name ?? "Недоступная деталь", " — ", Number(quantity).toLocaleString("ru-RU"), " шт.");
      return row;
    }));
    const dialog = el("dialog", "", "plotter-dialog workplace-dialog");
    const heading = el("h2", `Состав комплекта · ${kit.name}`);
    heading.id = "kit-composition-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const list = el("ul");
    list.append(...rows);
    const close = button("Закрыть", () => dialog.close());
    close.autofocus = true;
    dialog.append(el("header", heading, "drawing-dialog-heading"),
      el("div", list, "drawing-dialog-body workplace-dialog-body"), el("footer", close, "dialog-actions"));
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
  }

  function restoreRegistryBackup() {
    const archive = input("file", "", { accept: ".rar,application/vnd.rar,application/x-rar-compressed", required: true });
    const content = el("div", field("Резервная копия (.rar)", archive), "workplace-form-grid");
    content.append(el("p", "Восстановление заменит текущий реестр содержимым архива. Перед заменой скачайте копию текущего реестра.", "workplace-form-note"));
    formDialog("Восстановить бэкап", content, "Заменить реестр", async () => {
      const response = await fetch("/api/registry-backup", {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: archive.files[0],
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(data?.error || "Не удалось восстановить резервную копию реестра.");
      }
      for (const id of registryWindows.keys()) if (!findShortcut(id)?.app) registryWindows.delete(id);
      registrySelection.clear();
      registryClipboard = null;
      await render();
      status.textContent = "Реестр восстановлен.";
    }, { lockWhileSaving: true });
  }

  function registryTrashContent(onChanged, onLoaded) {
    const content = el("div", "", "registry-window-body registry-trash-content");
    content.tabIndex = 0;
    content.setAttribute("aria-label", "Содержимое корзины");
    const info = el("p", "Загружаем корзину…");
    info.setAttribute("role", "status");
    const rows = el("div", "", "registry-trash-entries");
    const error = el("p", "", "workplace-dialog-error");
    error.setAttribute("role", "alert");
    const empty = button("Очистить корзину", () => confirmPurge(null), { icon: "trash", disabled: true });
    empty.classList.add("registry-delete");
    const toolbar = el("div", info, "registry-trash-header");
    toolbar.append(empty);
    content.append(toolbar, error, rows);
    let busy = false;

    async function showEntries() {
      const entries = await readRegistryTrash();
      onLoaded();
      info.textContent = entries.length ? "Записей в корзине " + entries.length : "Корзина пуста";
      empty.disabled = !entries.length;
      rows.replaceChildren(...entries.map(entry => {
        const text = el("div", el("strong", entry.name));
        const date = Date.parse(entry.trashedAt);
        text.append(el("small", [registryEntryType(entry), entry.parentName || designerWorkspaceName,
          Number.isFinite(date) ? registryDate.format(date) : ""].filter(Boolean).join(" · ")));
        const identity = el("div", registryIcon(entry.kind), "registry-trash-identity");
        if (entry.kind === "folder") applyFolderColor(identity, entry.color);
        identity.append(text);
        const restore = button("Восстановить", async () => {
          if (busy) return;
          busy = restore.disabled = true;
          error.textContent = "";
          try {
            await post("/api/registry", { action: "restore-trash", id: entry.id, revision: entry.revision });
            await onChanged();
          } catch (failure) { error.textContent = failure.message; }
          finally { busy = restore.disabled = false; }
        });
        const purge = button("Удалить навсегда", () => confirmPurge(entry));
        purge.classList.add("registry-delete");
        const actions = el("div", restore, "registry-trash-actions");
        actions.append(purge);
        const row = el("article", identity, "registry-trash-entry");
        row.dataset.kind = entry.kind;
        row.append(actions);
        return row;
      }));
    }

    function confirmPurge(entry) {
      if (busy) return;
      const pin = input("password", "", { inputMode: "numeric", autoComplete: "off",
        pattern: "[0-9]{4}", minLength: 4, maxLength: 4, required: true, autofocus: true });
      const scope = !entry ? "Все записи исчезнут из корзины."
        : entry.kind === "folder" ? "Папка и её содержимое исчезнут из корзины." : "Запись исчезнет из корзины.";
      const description = `${scope} Восстановление из корзины станет недоступно. Записи, используемые в комплектах или нарядах, останутся доступны в их составе.`;
      const fields = el("div", el("p", description), "workplace-form-grid");
      fields.append(field("ПИН-код корзины", pin));
      formDialog(entry ? `Удалить «${entry.name}» из корзины?` : "Очистить корзину?", fields,
        entry ? "Удалить навсегда" : "Очистить корзину", async () => {
          try {
            await post("/api/registry", entry
              ? { action: "purge-trash", id: entry.id, revision: entry.revision, pin: pin.value }
              : { action: "empty-trash", pin: pin.value });
          } finally { pin.value = ""; }
          await onChanged();
        }, { lockWhileSaving: true });
    }

    void showEntries().catch(failure => {
      info.textContent = "";
      error.textContent = failure.message;
    });
    return content;
  }

  function renderRegistry() {
    page.classList.add("is-registry-desktop");
    document.title = "Плоттер · " + designerWorkspaceName;
    topbar.hidden = true;

    const desktop = el("section", "", "registry-desktop");
    desktop.setAttribute("aria-label", designerWorkspaceName);
    const selectionCount = el("small", "", "registry-selection-count");
    selectionCount.setAttribute("role", "status");
    selectionCount.hidden = true;
    const stage = el("div", "", "registry-desktop-stage");
    const icons = el("div", "", "registry-desktop-items");
    const surface = el("div", icons, "registry-desktop-surface");
    surface.tabIndex = 0;
    surface.setAttribute("aria-label", "Рабочая область");
    const taskbar = el("nav", "", "registry-desktop-taskbar");
    taskbar.setAttribute("aria-label", "Панель задач");
    const pinnedApps = el("div", "", "registry-pinned-apps");
    pinnedApps.setAttribute("role", "group");
    pinnedApps.setAttribute("aria-label", "Закреплённые приложения");
    const openWindows = el("div", "", "registry-open-windows");
    openWindows.setAttribute("aria-label", "Открытые окна");
    const clock = el("time", "", "registry-desktop-clock");
    clock.setAttribute("aria-label", "Местное время");
    const date = el("time", "", "registry-desktop-date");
    date.setAttribute("aria-label", "Местная дата");
    const dateTime = el("div", clock, "registry-desktop-datetime");
    dateTime.append(date);
    const menu = el("div", "", "registry-context-menu");
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    const marquee = el("div", "", "registry-selection-rectangle");
    marquee.hidden = true;
    marquee.setAttribute("aria-hidden", "true");
    const start = button("Пуск", () => {
      if (!menu.hidden && menuOwner === start) { closeMenu(true); return; }
      const bounds = start.getBoundingClientRect();
      showMenu([
        ...pinnedShortcuts.map(shortcut => [shortcut.name, shortcut.icon, () => launchShortcut(shortcut)]),
        null,
        ["Скачать бэкап", "download", execute(() =>
          downloadAttachment("/api/registry-backup", "Реестр Plotter.rar", "Не удалось скачать бэкап реестра."))],
        ["Восстановить бэкап", "upload", restoreRegistryBackup],
        null, ["Выйти", "exit", leaveRole],
      ], bounds.left, bounds.top, start);
      start.setAttribute("aria-expanded", "true");
    }, { icon: "layout" });
    start.classList.add("registry-start-button");
    start.setAttribute("aria-haspopup", "menu");
    start.setAttribute("aria-expanded", "false");
    const desktopButton = button("Показать рабочую область", () => {
      for (const model of registryWindows.values()) model.minimized = true;
      drawWindows();
      surface.focus({ preventScroll: true });
    }, { icon: "layout", iconOnly: true });
    desktopButton.classList.add("registry-task-button");
    taskbar.append(start, desktopButton, pinnedApps, openWindows, selectionCount, dateTime);
    stage.append(surface, marquee, menu);
    desktop.append(stage, taskbar);
    panel.append(desktop);

    const events = new AbortController();
    const windowNodes = new Map();
    const taskButtons = new Map();
    const iconWidth = 150, iconHeight = 128, desktopInset = 12;
    const dragType = "application/x-plotter-registry-entry";
    let dragged = null;
    let selectedShortcut = null;
    let dropHighlight = null;
    let menuOwner = null;
    let keyboardFolder;
    let desktopCells = new Map();
    let measuredWidth = -1;
    const columnCount = () => Math.max(1, Math.floor((surface.clientWidth - 2 * desktopInset) / iconWidth));
    const clamp = (value, maximum) => Math.max(0, Math.min(value, maximum));
    const selectionModifier = event => event.ctrlKey || event.metaKey;
    const isCutEntry = id => Boolean(registryClipboard?.cut && registryClipboard.ids.has(id));

    function closeMenu(restoreFocus = false) {
      if (menu.hidden) return;
      menu.hidden = true;
      start.setAttribute("aria-expanded", "false");
      if (restoreFocus && menuOwner?.isConnected) menuOwner.focus({ preventScroll: true });
      menuOwner = null;
    }

    function updateRegistrySelection() {
      for (const control of desktop.querySelectorAll(".registry-desktop-icon")) {
        const selected = control.dataset.shortcutId ? selectedShortcut === control.dataset.shortcutId
          : registrySelection.has(control.dataset.registryId);
        control.setAttribute("aria-pressed", String(selected));
        control.classList.toggle("is-cut", isCutEntry(control.dataset.registryId));
      }
      selectionCount.hidden = registrySelection.size < 2;
      selectionCount.textContent = selectionCount.hidden ? "" : `Выбрано ${registrySelection.size}`;
    }

    function replaceRegistrySelection(ids, shortcutId = null) {
      registrySelection.clear();
      for (const id of ids) registrySelection.add(id);
      selectedShortcut = shortcutId;
      updateRegistrySelection();
    }

    function toggleRegistrySelection(id, event) {
      if (!selectionModifier(event)) { replaceRegistrySelection([id]); return; }
      selectedShortcut = null;
      if (!registrySelection.delete(id)) registrySelection.add(id);
      updateRegistrySelection();
    }

    function bindMarquee(node) {
      let gesture = null;
      node.addEventListener("pointerdown", event => {
        if (event.button !== 0 || !event.isPrimary || event.pointerType === "touch"
            || event.target.closest("button, input, select, a, thead")) return;
        const rect = node.getBoundingClientRect();
        if (event.clientX >= rect.left + node.clientWidth || event.clientY >= rect.top + node.clientHeight) return;
        const previous = selectionModifier(event) ? [...registrySelection] : [];
        replaceRegistrySelection(previous);
        gesture = { pointer: event.pointerId, x: event.clientX, y: event.clientY, previous };
        event.preventDefault();
        node.focus({ preventScroll: true });
        node.setPointerCapture(event.pointerId);
      });
      node.addEventListener("pointermove", event => {
        if (gesture?.pointer !== event.pointerId) return;
        if (marquee.hidden && Math.max(Math.abs(event.clientX - gesture.x), Math.abs(event.clientY - gesture.y)) < 4) return;
        const rect = node.getBoundingClientRect();
        const x = rect.left + clamp(event.clientX - rect.left, node.clientWidth);
        const y = rect.top + clamp(event.clientY - rect.top, node.clientHeight);
        const left = Math.min(gesture.x, x), right = Math.max(gesture.x, x);
        const top = Math.min(gesture.y, y), bottom = Math.max(gesture.y, y);
        const origin = stage.getBoundingClientRect();
        marquee.style.left = left - origin.left + "px";
        marquee.style.top = top - origin.top + "px";
        marquee.style.width = right - left + "px";
        marquee.style.height = bottom - top + "px";
        marquee.hidden = false;
        const hits = [...node.querySelectorAll(".registry-desktop-icon[data-registry-id]")].filter(control => {
          const box = (control.closest("tr") ?? control).getBoundingClientRect();
          return box.left < right && box.right > left && box.top < bottom && box.bottom > top;
        }).map(control => control.dataset.registryId);
        replaceRegistrySelection([...gesture.previous, ...hits]);
      });
      node.addEventListener("lostpointercapture", event => {
        if (gesture?.pointer !== event.pointerId) return;
        gesture = null;
        marquee.hidden = true;
      });
    }

    function showMenu(actions, x, y, owner) {
      menuOwner = owner;
      menu.classList.toggle("is-start-menu", owner === start);
      menu.replaceChildren(...actions.map(action => {
        if (!action) return el("hr");
        const [title, icon, callback, palette] = action;
        const item = button(title, () => { closeMenu(true); callback(); }, { icon });
        item.classList.add("registry-menu-item");
        item.setAttribute("role", palette ? "menuitemradio" : "menuitem");
        if (palette) {
          item.classList.add("registry-color-option");
          applyFolderColor(item, palette.color);
          item.setAttribute("aria-checked", String(palette.selected));
        }
        return item;
      }));
      menu.hidden = false;
      const rect = stage.getBoundingClientRect();
      menu.style.left = clamp(x - rect.left, stage.clientWidth - menu.offsetWidth) + "px";
      menu.style.top = clamp(y - rect.top, stage.clientHeight - menu.offsetHeight) + "px";
      menu.querySelector("button")?.focus({ preventScroll: true });
    }

    function showMenuFor(actions, owner, event) {
      const bounds = owner.getBoundingClientRect();
      showMenu(actions, event?.clientX ?? bounds.left + 12, event?.clientY ?? bounds.top + 12, owner);
    }

    function openBackgroundMenu(folder, owner, event) {
      const actions = [
        ["Создать папку", "folder", () => createFolder(folder)],
        ["Создать комплект", "kit", () => createKitDialog(folder)],
      ];
      if (registryClipboard) actions.push(null, ["Вставить", "paste", () => pasteRegistryClipboard(folder)]);
      showMenuFor(actions, owner, event);
    }

    function registryControl(id) {
      return desktop.querySelector(`[data-registry-id="${CSS.escape(id)}"]`);
    }

    const createFolder = execute(async parent => {
      const names = new Set(entries.filter(entry => entry.parent === parent).map(entry => normalizeOrderSearch(entry.name)));
      let name = "Новая папка";
      for (let number = 2; names.has(normalizeOrderSearch(name)); number++) name = `Новая папка (${number})`;
      const saved = await post("/api/registry", { action: "save", kind: "folder", parent, name });
      await refreshRegistry();
      if (!desktop.isConnected) return;
      const model = registryWindows.get(parent);
      if (model?.filter && model.filter !== "folder") model.filter = "";
      if (model?.query && !normalizeOrderSearch(name).includes(normalizeOrderSearch(model.query.trim()))) model.query = "";
      replaceRegistrySelection([saved.id]);
      drawViews();
      const entry = find(saved.id);
      if (entry) editFolderName(entry, registryControl(entry.id));
    });

    const saveFolderName = execute(async (entry, name) => {
      const saved = await post("/api/registry", { action: "rename", id: entry.id, revision: entry.revision, name });
      const current = find(entry.id);
      if (!current || current.revision > saved.revision) return;
      Object.assign(current, { name: saved.name, revision: saved.revision, updatedAt: saved.updatedAt });
      const control = registryControl(entry.id);
      if (control) {
        control.title = pathOf(current);
        control.setAttribute("aria-label", registryEntryType(current) + " · " + saved.name);
        control.querySelector(".registry-icon-label").textContent = saved.name;
        control.closest("tr")?.lastElementChild.replaceWith(registryDateCell(saved.updatedAt));
      }
      const title = windowNodes.get(entry.id)?.querySelector(".registry-window-title");
      if (title) {
        title.title = pathOf(current);
        title.querySelector("span").textContent = saved.name;
      }
      const task = taskButtons.get(entry.id);
      if (task) {
        task.title = pathOf(current);
        task.setAttribute("aria-label", saved.name);
        task.querySelector("span").textContent = saved.name;
      }
    });

    function editFolderName(entry, control) {
      if (!control) return;
      const editor = el("div", registryIcon(entry.kind), control.className + " registry-icon-editor");
      editor.dataset.kind = entry.kind;
      editor.style.cssText = control.style.cssText;
      const name = input("text", entry.name, { maxLength: 255, ariaLabel: "Название папки" });
      name.classList.add("registry-inline-name");
      editor.append(name);
      let finished = false;
      const finish = (commit, focus) => {
        if (finished) return;
        finished = true;
        if (!editor.isConnected) return;
        editor.replaceWith(control);
        if (focus) control.focus({ preventScroll: true });
        const value = name.value.trim();
        if (commit && value && value !== entry.name) void saveFolderName(entry, value);
      };
      name.addEventListener("keydown", event => {
        if (event.isComposing || (event.key !== "Enter" && event.key !== "Escape")) return;
        event.preventDefault();
        event.stopPropagation();
        finish(event.key === "Enter", true);
      });
      name.addEventListener("blur", () => finish(true, false));
      control.replaceWith(editor);
      name.focus();
      name.select();
    }

    function renameEntry(entry, control) {
      if (entry.kind === "folder") editFolderName(entry, control);
      else renameRegistryEntry(entry);
    }

    const openCadWindow = execute(async entry => {
      const editor = registryWindows.get(registryCad.id)?.cadEditor;
      if (entry && editor?.document?.id === entry.id) {
        openRegistryWindow(registryCad);
        return;
      }
      if (editor?.instance?.isBusy() || !await confirmLeaveCad([editor?.instance])) return;
      const document = entry ? await get("/api/registry", { id: entry.id }) : null;
      if (registryWindows.get(registryCad.id)?.cadEditor === editor) editor?.instance?.destroy();
      openRegistryWindow(registryCad, { cadEditor: { document } });
    });
    const openKitLayout = execute(entry => kitLayoutDialog(entry));
    const openSewing = execute(entry => sewingMap(entry));
    const deleteEntry = execute(entry => deleteRegistryEntry(entry));
    const updateFolder = execute(async (entry, changes) => {
      const { id, revision, name, parent, model } = entry;
      await post("/api/registry", { action: "save", kind: "folder", id, revision, name, parent, model, ...changes });
      await refreshRegistry();
      drawViews();
    });

    function openEntry(entry) {
      if (entry.kind === "folder" || entry.kind === "kit") openRegistryWindow(entry);
      else void openCadWindow(entry);
    }

    function openEntryMenu(entry, owner, event) {
      if (!registrySelection.has(entry.id)) replaceRegistrySelection([entry.id]);
      const ids = [...registrySelection];
      const clipboardActions = [
        ["Копировать", "copy", () => copyRegistrySelection(ids)],
        ["Вырезать", "cut", () => copyRegistrySelection(ids, true)],
      ];
      if (ids.length > 1) {
        showMenuFor([...clipboardActions, null, [`В корзину (${ids.length})`, "trash", () => trashSelection(ids)]], owner, event);
        return;
      }
      const actions = [["Открыть", "open", () => openEntry(entry)]];
      if (entry.kind === "part") actions.push(["Быстрый просмотр", "view", () => openPartDrawing(entry)]);
      actions.push(["Переименовать…", "edit", () => renameEntry(entry, owner)]);
      if (entry.kind === "folder") {
        const currentColor = entry.color ?? "yellow";
        actions.push(["Цвет папки…", "folder", () => showMenuFor(
          Object.entries(folderColors).map(([color, [title]]) => [title, null, () => {
            if (currentColor !== color) void updateFolder(entry, { color });
          }, { color, selected: currentColor === color }]), owner, event)]);
        actions.push([entry.model ? "Снять признак изделия" : "Обозначить как изделие", entry.model ? "close" : "check",
          () => updateFolder(entry, { model: !entry.model })]);
      }
      if (entry.kind === "kit") actions.push(["Раскладка комплекта", "layout", () => openKitLayout(entry)],
        ["Карта пошива", "sewing", () => openSewing(entry)]);
      actions.push(null, ...clipboardActions);
      if (entry.kind === "folder" && registryClipboard) {
        actions.push(["Вставить", "paste", () => pasteRegistryClipboard(entry.id)]);
      }
      actions.push(null, ["В корзину", "trash", () => deleteEntry(entry)]);
      showMenuFor(actions, owner, event);
    }

    function menuKey(event) {
      return event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
    }

    function bindBackground(node, folder, desktopDrop = false) {
      node.addEventListener("contextmenu", event => {
        if (event.target.closest(".registry-desktop-icon")) return;
        event.preventDefault();
        replaceRegistrySelection([]);
        openBackgroundMenu(folder, node, event);
      });
      node.addEventListener("keydown", event => {
        if (event.target === node && menuKey(event)) {
          event.preventDefault();
          openBackgroundMenu(folder, node);
        }
      });
      if (desktopDrop) bindRegistryCommands(node, folder);
      bindMarquee(node);
      bindDrop(node, folder, desktopDrop);
    }

    function acceptsRegistryCommand(event) {
      const target = event.target;
      return !event.defaultPrevented && target instanceof Element && menu.hidden && !document.querySelector("dialog[open]")
        && !target.isContentEditable && !target.closest("input, select, textarea, [role=textbox], [data-shortcut-id]");
    }

    function bindRegistryCommands(node, folder) {
      node.addEventListener("keydown", event => handleRegistryKeys(event, folder));
      for (const action of ["copy", "cut"]) node.addEventListener(action, event => {
        if (!acceptsRegistryCommand(event) || !registrySelection.size) return;
        event.preventDefault();
        copyRegistrySelection(registrySelection, action === "cut");
        const names = [...registrySelection].map(id => find(id)?.name).filter(Boolean);
        event.clipboardData?.setData("text/plain", names.join("\n"));
      });
      node.addEventListener("paste", event => {
        if (!acceptsRegistryCommand(event) || !registryClipboard) return;
        event.preventDefault();
        void pasteRegistryClipboard(folder);
      });
    }

    function handleRegistryKeys(event, folder) {
      if (event.repeat || event.isComposing || event.altKey || event.shiftKey || !acceptsRegistryCommand(event)) return;
      if (selectionModifier(event)) {
        const key = /^Key[CVX]$/.test(event.code) ? event.code.at(-1).toLowerCase() : event.key.toLowerCase();
        if ((key === "c" || key === "x") && registrySelection.size) {
          event.preventDefault();
          copyRegistrySelection(registrySelection, key === "x");
        } else if (key === "v" && registryClipboard) {
          event.preventDefault();
          void pasteRegistryClipboard(folder);
        }
        return;
      }
      if ((event.key !== "Delete" && event.code !== "Delete") || !registrySelection.size) return;
      event.preventDefault();
      void trashSelection([...registrySelection]).then(() => {
        if (!desktop.isConnected || document.activeElement !== document.body) return;
        const origin = windowNodes.get(folder)?.querySelector(".registry-window-body") ?? surface;
        origin.focus({ preventScroll: true });
      });
    }

    function setDropHighlight(control) {
      dropHighlight?.classList.remove("registry-drop-target");
      dropHighlight = control;
      dropHighlight?.classList.add("registry-drop-target");
    }

    function registryVersions(selected) {
      return selected.map(({ id, revision }) => ({ id, revision }));
    }

    function copyRegistrySelection(ids, cut = false) {
      const selected = new Set([...ids].filter(id => find(id)));
      if (!selected.size) return;
      registryClipboard = { ids: selected, cut };
      updateRegistrySelection();
    }

    const pasteRegistryClipboard = execute(async parent => {
      const clipboard = registryClipboard;
      if (!clipboard) return;
      const selected = [...clipboard.ids].map(find);
      if (selected.some(entry => !entry)) throw new Error("Одна из записей больше недоступна. Выберите записи заново.");
      const result = await post("/api/registry", { action: clipboard.cut ? "move-selection" : "copy-selection",
        entries: registryVersions(selected), parent });
      if (clipboard.cut && clipboard === registryClipboard) registryClipboard = null;
      await refreshRegistry();
      if (!desktop.isConnected) return;
      const resultIds = result.entries.length ? result.entries.map(entry => entry.id) : [...clipboard.ids];
      replaceRegistrySelection(resultIds);
      drawViews();
      const target = registryControl(resultIds[0])
        ?? windowNodes.get(parent)?.querySelector(".registry-window-body") ?? registryControl(parent) ?? surface;
      target.focus({ preventScroll: true });
    });

    const trashSelection = execute(async ids => {
      const selected = ids.map(find).filter(Boolean);
      if (selected.length === 1) return deleteRegistryEntry(selected[0]);
      if (!selected.length || !await confirmAction({
        title: "Переместить выбранные записи в корзину?",
        message: `Выбрано ${selected.length}. Записи можно будет восстановить из корзины.`,
        confirmLabel: "В корзину",
      })) return;
      await post("/api/registry", { action: "trash-selection", entries: registryVersions(selected) });
      await refreshRegistry();
      if (desktop.isConnected) drawViews();
    });

    function moveDesktopIcons(ids, anchor, cell) {
      for (const [key, position] of desktopCells) {
        if (!findShortcut(key) && !registryPositions.has(key)) registryPositions.set(key, position);
      }
      const columns = columnCount();
      const moving = new Set(ids);
      const occupied = new Set([...desktopCells].filter(([id]) => !moving.has(id)).map(([, position]) => position.join(",")));
      const start = desktopCells.get(anchor);
      const ordered = moving.has(anchor) ? [anchor, ...ids.filter(id => id !== anchor)] : ids;
      for (const id of ordered) {
        const original = desktopCells.get(id);
        const [dx, dy] = original && start ? [original[0] - start[0], original[1] - start[1]] : [0, 0];
        const target = [clamp(cell[0] + dx, columns - 1), Math.max(0, cell[1] + dy)];
        const free = findFreeCell(target, occupied, columns);
        registryPositions.set(id, free);
        occupied.add(free.join(","));
      }
    }

    const moveSelection = execute(async ({ ids, anchor }, parent, cell) => {
      if (!findShortcut(anchor)) {
        const selected = ids.map(find).filter(Boolean);
        if (!selected.length || (!cell && selected.every(entry => entry.parent === parent))) return;
        if (selected.some(entry => entry.parent !== parent)) {
          await post("/api/registry", { action: "move-selection", entries: registryVersions(selected), parent });
          await refreshRegistry();
        }
      }
      const moved = ids.filter(id => findShortcut(id) || find(id)?.parent === parent);
      if (cell) {
        moveDesktopIcons(moved, anchor, cell);
      } else if (parent !== null) {
        for (const id of moved) registryPositions.delete(id);
      }
      saveRegistryPositions(REGISTRY_POSITIONS_KEY, registryPositions);
      if (desktop.isConnected) drawViews();
    });

    function bindDrop(control, parent, desktopDrop = false) {
      const toTrash = parent === registryTrash.id;
      const dragging = event => dragged && Array.from(event.dataTransfer?.types ?? []).includes(dragType);
      const valid = () => findShortcut(dragged.anchor) ? desktopDrop
        : toTrash || !parent || !trailOf(find(parent)).some(folder => dragged.ids.includes(folder.id));
      const acceptDrop = event => {
        if (!dragging(event)) return;
        event.stopPropagation();
        if (!valid()) {
          event.dataTransfer.dropEffect = "none";
          setDropHighlight(null);
          return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setDropHighlight(control);
      };
      control.addEventListener("dragenter", acceptDrop);
      control.addEventListener("dragover", acceptDrop);
      control.addEventListener("dragleave", event => {
        if (dropHighlight === control && !control.contains(event.relatedTarget)) setDropHighlight(null);
      });
      control.addEventListener("drop", event => {
        if (!dragging(event)) return;
        event.preventDefault();
        event.stopPropagation();
        const selection = dragged;
        const allowed = valid();
        dragged = null;
        setDropHighlight(null);
        if (!allowed) return;
        if (toTrash) { void trashSelection(selection.ids); return; }
        const rect = icons.getBoundingClientRect();
        const cell = desktopDrop ? [
          clamp(Math.floor((event.clientX - rect.left - desktopInset) / iconWidth), columnCount() - 1),
          Math.max(0, Math.floor((event.clientY - rect.top - desktopInset) / iconHeight)),
        ] : null;
        void moveSelection(selection, parent, cell);
      });
    }

    function bindIconDrag(control, id) {
      control.addEventListener("dragstart", event => {
        closeMenu();
        const shortcut = findShortcut(id);
        if (shortcut) replaceRegistrySelection([], id);
        else if (!registrySelection.has(id)) replaceRegistrySelection([id]);
        const ids = shortcut ? [id] : [...registrySelection];
        if (ids.length > 1) control.dataset.dragCount = ids.length;
        control.classList.add("is-dragging");
        dragged = { ids, anchor: id };
        event.dataTransfer.setData(dragType, id);
        event.dataTransfer.effectAllowed = "move";
      });
      control.addEventListener("dragend", () => {
        control.classList.remove("is-dragging");
        delete control.dataset.dragCount;
        dragged = null;
        setDropHighlight(null);
      });
    }

    function makeIcon(entry) {
      const control = button("", event => {
        toggleRegistrySelection(entry.id, event);
        control.focus({ preventScroll: true });
      });
      control.classList.add("registry-desktop-icon");
      control.classList.toggle("is-cut", isCutEntry(entry.id));
      control.dataset.kind = entry.kind;
      control.dataset.registryId = entry.id;
      control.title = pathOf(entry);
      control.setAttribute("aria-label", registryEntryType(entry) + " · " + entry.name);
      control.setAttribute("aria-pressed", String(registrySelection.has(entry.id)));
      control.draggable = true;
      control.append(registryIcon(entry.kind), el("span", entry.name, "registry-icon-label"));
      if (entry.kind === "folder") applyFolderColor(control, entry.color);
      control.addEventListener("dblclick", event => { if (!selectionModifier(event)) openEntry(entry); });
      control.addEventListener("contextmenu", event => {
        event.preventDefault();
        event.stopPropagation();
        openEntryMenu(entry, control, event);
      });
      control.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === "F2" || menuKey(event)) {
          event.preventDefault();
          if (event.key === "Enter") openEntry(entry);
          else if (event.key === "F2") renameEntry(entry, control);
          else openEntryMenu(entry, control);
        }
      });
      bindIconDrag(control, entry.id);
      if (entry.kind === "folder") bindDrop(control, entry.id);
      return control;
    }

    function findFreeCell(cell, occupied, columns) {
      let [column, row] = cell;
      while (occupied.has(column + "," + row)) {
        column += 1;
        if (column === columns) { column = 0; row += 1; }
      }
      return [column, row];
    }

    function makeShortcutIcon(shortcut) {
      const open = () => launchShortcut(shortcut);
      const control = button("", () => replaceRegistrySelection([], shortcut.id));
      control.classList.add("registry-desktop-icon");
      control.dataset.kind = shortcut.kind;
      control.dataset.shortcutId = shortcut.id;
      control.title = shortcut.name;
      control.setAttribute("aria-pressed", String(selectedShortcut === shortcut.id));
      control.draggable = true;
      control.append(el("span", shortcut.name, "registry-icon-label"));
      if (shortcut === registryTrash) setTrashShortcutIcon(control);
      else control.prepend(registryIcon(shortcut.icon));
      control.addEventListener("dblclick", open);
      const openMenu = event => {
        replaceRegistrySelection([], shortcut.id);
        showMenuFor([["Открыть", "open", open]], control, event);
      };
      control.addEventListener("contextmenu", event => {
        event.preventDefault();
        event.stopPropagation();
        openMenu(event);
      });
      control.addEventListener("keydown", event => {
        if (event.key !== "Enter" && !menuKey(event)) return;
        event.preventDefault();
        if (event.key === "Enter") open();
        else openMenu();
      });
      if (shortcut === registryTrash) bindDrop(control, shortcut.id);
      bindIconDrag(control, shortcut.id);
      return control;
    }

    function setTrashShortcutIcon(control) {
      if (registryTrashFilled !== null) {
        control.dataset.trashState = registryTrashFilled ? "filled" : "empty";
        control.title = registryTrashFilled ? "Корзина · есть записи" : "Корзина · пуста";
      }
      const icon = registryTrashIcon(registryTrashFilled);
      const previous = control.querySelector(":scope > .registry-icon");
      if (previous) previous.replaceWith(icon);
      else control.prepend(icon);
    }

    function updateTrashShortcuts() {
      for (const control of desktop.querySelectorAll(
        '.registry-desktop-icon[data-kind="trash"], .registry-task-button[data-kind="trash"]',
      )) setTrashShortcutIcon(control);
    }

    function placeDesktopIcon(icon, cell) {
      icon.style.left = desktopInset + cell[0] * iconWidth + "px";
      icon.style.top = desktopInset + cell[1] * iconHeight + "px";
      icons.append(icon);
    }

    function drawDesktop() {
      const root = entries.filter(entry => entry.parent === null);
      icons.replaceChildren();
      desktopCells = new Map();
      const columns = columnCount();
      const occupied = new Set();
      let rows = 1;
      function place(entry, desired) {
        const cell = findFreeCell([clamp(desired[0], columns - 1), desired[1]], occupied, columns);
        occupied.add(cell.join(","));
        desktopCells.set(entry.id, cell);
        rows = Math.max(rows, cell[1] + 1);
        return cell;
      }
      desktopShortcuts.forEach((shortcut, index) => {
        const home = shortcut === registryTrash ? [columns - 1, 0] : [index % columns, Math.floor(index / columns)];
        placeDesktopIcon(makeShortcutIcon(shortcut), place(shortcut, registryPositions.get(shortcut.id) ?? home));
      });
      for (const entry of root) {
        const saved = registryPositions.get(entry.id);
        if (saved) place(entry, saved);
      }
      let next = [0, 0];
      for (const entry of root) {
        let cell = desktopCells.get(entry.id);
        if (!cell) next = cell = place(entry, next);
        placeDesktopIcon(makeIcon(entry), cell);
      }
      icons.style.minHeight = rows * iconHeight + 2 * desktopInset + "px";
    }

    function setWindowBounds(model, node) {
      if (model.maximized || !stage.clientWidth || !stage.clientHeight) return;
      const [x, y, width, height] = model.bounds;
      if (width !== undefined) node.style.width = Math.min(width, stage.clientWidth) + "px";
      if (height !== undefined) node.style.height = Math.min(height, stage.clientHeight) + "px";
      node.style.left = clamp(x, stage.clientWidth - node.offsetWidth) + "px";
      node.style.top = clamp(y, stage.clientHeight - node.offsetHeight) + "px";
    }

    function resizeWindowBounds([x, y, width, height], edge, dx, dy, isCad) {
      const minWidth = Math.min(isCad ? 600 : 300, stage.clientWidth);
      const minHeight = Math.min(isCad ? 400 : 190, stage.clientHeight);
      const right = x + width, bottom = y + height;
      const left = edge.includes("w") ? clamp(x + dx, right - minWidth) : x;
      const top = edge.includes("n") ? clamp(y + dy, bottom - minHeight) : y;
      const newRight = edge.includes("e") ? Math.min(stage.clientWidth, Math.max(x + minWidth, right + dx)) : right;
      const newBottom = edge.includes("s") ? Math.min(stage.clientHeight, Math.max(y + minHeight, bottom + dy)) : bottom;
      return [left, top, newRight - left, newBottom - top];
    }

    function updateActiveWindow() {
      const active = [...registryWindows].filter(([, model]) => !model.minimized)
        .sort((left, right) => right[1].z - left[1].z)[0]?.[0];
      for (const [id, node] of windowNodes) {
        node.classList.toggle("is-active", id === active);
      }
      for (const [id, task] of taskButtons) task.setAttribute("aria-pressed", String(id === active));
    }

    function bringForward(id) {
      const model = registryWindows.get(id);
      model.z = ++registryWindowOrder;
      windowNodes.get(id).style.zIndex = model.z;
      updateActiveWindow();
    }

    function launchShortcut(entry) {
      if (entry === registryCad && !registryWindows.has(entry.id)) void openCadWindow(null);
      else openRegistryWindow(entry);
    }

    function openRegistryWindow(entry, changes) {
      const offset = 22 + (registryWindows.size % 7) * 28;
      const bounds = registryWindowBounds.get(entry.id) ?? [offset + 132, offset];
      const model = registryWindows.get(entry.id) ?? { bounds, maximized: false, scroll: 0 };
      Object.assign(model, changes);
      model.minimized = false;
      model.z = ++registryWindowOrder;
      registryWindows.set(entry.id, model);
      drawViews();
      const node = windowNodes.get(entry.id);
      (node?.querySelector(".cad-view canvas") ?? node)?.focus({ preventScroll: true });
    }

    function folderEntryList(items, sort, changeSort) {
      const list = el("table", "", "registry-entry-list");
      const headings = el("tr");
      for (const [label, ascending, descending, initial = ascending] of [
        ["Название", "name", "reverse-name"], ["Тип", "type", "reverse-type"],
        ["Создано", "oldest", "newest", "newest"], ["Изменено", "updated-oldest", "updated", "updated"],
      ]) {
        const column = el("th");
        column.scope = "col";
        const isAscending = sort === ascending;
        const isDescending = sort === descending;
        const next = isAscending ? descending : isDescending ? ascending : initial;
        const control = button(label, () => changeSort(next, ascending));
        control.className = "registry-column-sort";
        control.dataset.sortColumn = ascending;
        const direction = el("span", isDescending ? "↓" : isAscending ? "↑" : "", "registry-column-sort-direction");
        direction.setAttribute("aria-hidden", "true");
        control.append(direction);
        column.append(control);
        if (isDescending || isAscending) column.setAttribute("aria-sort", isDescending ? "descending" : "ascending");
        headings.append(column);
      }
      const rows = el("tbody");
      for (const entry of items) {
        const row = el("tr");
        const name = makeIcon(entry);
        name.classList.add("registry-list-entry");
        row.append(el("td", name), el("td", registryEntryType(entry)),
          registryDateCell(entry.createdAt), registryDateCell(entry.updatedAt));
        rows.append(row);
      }
      list.append(el("thead", headings), rows);
      return list;
    }

    function folderWindowContents(folder, model) {
      const folderSearch = input("search", model.query ?? "", { placeholder: "Найти в папке" });
      folderSearch.setAttribute("aria-label", "Поиск по названию в папке");
      const searchBox = el("label", registryIcon("search"), "registry-window-search");
      searchBox.append(folderSearch);
      const filter = select(folderFilterOptions, model.filter ?? "");
      filter.classList.add("registry-window-sort", "registry-window-filter");
      filter.setAttribute("aria-label", "Тип записи в папке");
      const sort = select(folderSortOptions, model.sort ?? "name");
      sort.classList.add("registry-window-sort");
      sort.setAttribute("aria-label", "Сортировка содержимого папки");
      const views = el("div", "", "registry-window-views");
      views.setAttribute("role", "group");
      views.setAttribute("aria-label", "Вид содержимого папки");
      const viewButtons = [["list", "Список"], ["icons", "Значки"]].map(([view, label]) => {
        const control = button(label, () => { model.view = view; drawFolderEntries(); });
        control.classList.add("registry-window-view");
        views.append(control);
        return control;
      });
      const toolbar = el("div", searchBox, "registry-window-toolbar");
      toolbar.append(filter, sort, views);
      const body = el("div", "", "registry-window-body");
      body.tabIndex = 0;
      body.setAttribute("aria-label", "Содержимое папки " + folder.name);
      const children = entries.filter(entry => entry.parent === folder.id);
      function drawFolderEntries() {
        const needle = folderSearch.value.trim().toLocaleLowerCase("ru-RU").replaceAll("ё", "е");
        const items = children.filter(entry => (!filter.value || entry.kind === filter.value)
          && entry.name.toLocaleLowerCase("ru-RU").replaceAll("ё", "е").includes(needle))
          .sort((a, b) => compareRegistryEntries(a, b, sort.value));
        const isList = model.view !== "icons";
        viewButtons[0].setAttribute("aria-pressed", String(isList));
        viewButtons[1].setAttribute("aria-pressed", String(!isList));
        if (!items.length) {
          body.replaceChildren(el("p", needle || filter.value ? "Совпадений нет" : "Папка пуста", "registry-empty-folder"));
        } else if (isList) {
          body.replaceChildren(folderEntryList(items, sort.value, changeSort));
        } else {
          const grid = el("div", "", "registry-icon-grid");
          grid.append(...items.map(entry => makeIcon(entry)));
          body.replaceChildren(grid);
        }
      }
      folderSearch.addEventListener("input", () => {
        model.query = folderSearch.value;
        replaceRegistrySelection([]);
        drawFolderEntries();
      });
      filter.addEventListener("change", () => {
        model.filter = filter.value;
        replaceRegistrySelection([]);
        drawFolderEntries();
      });
      function changeSort(value, column) {
        model.sort = sort.value = value;
        drawFolderEntries();
        if (column) body.querySelector(`[data-sort-column="${column}"]`)?.focus({ preventScroll: true });
      }
      sort.addEventListener("change", () => changeSort(sort.value));
      drawFolderEntries();
      bindBackground(body, folder.id);
      return [toolbar, body];
    }

    function applicationBody(shortcut, model) {
      if (model.appBody) return model.appBody;
      const body = el("div", "", "registry-window-body registry-app-content");
      body.tabIndex = 0;
      body.setAttribute("aria-label", shortcut.name);
      model.appBody = body;
      if (shortcut === ordersShortcut) {
        body.append(el("p", "Загружаем наряды…", "workplace-empty"));
        void renderOrdersView(body, false).catch(error => {
          const message = el("p", error.message, "workplace-empty");
          message.setAttribute("role", "alert");
          body.replaceChildren(message);
        });
      } else {
        labelDesigner ??= createLabelDesigner({ get, post });
        body.append(labelDesigner.element);
        void labelDesigner.open();
      }
      return body;
    }

    function kitEditorContents(entry, model) {
      if (model.kitEditor) {
        model.kitEditor.refresh?.();
        return model.kitEditor.body;
      }
      const loading = el("p", "Загружаем состав комплекта…", "workplace-empty");
      loading.setAttribute("role", "status");
      const body = el("div", loading, "registry-window-body registry-kit-editor-content");
      const editor = { body, saving: false };
      model.kitEditor = editor;

      void get("/api/registry", { id: entry.id }).then(kit => {
        if (registryWindows.get(entry.id) !== model) return;
        const picker = itemPicker(["part"], kit.members, { mode: "kit", folder: kit.parent });
        const fields = el("div", picker.section, "registry-kit-editor-fields");
        editor.refresh = picker.refresh;
        let revision = kit.revision;
        let savedMembers = JSON.stringify(picker.values());
        editor.dirty = () => JSON.stringify(picker.values()) !== savedMembers;
        const error = el("p", "", "registry-kit-editor-error");
        error.setAttribute("role", "alert");
        const cancel = button("Отмена", () => model.closeEditor());
        const save = el("button", "Сохранить", "button primary");
        save.type = "submit";
        const actions = el("footer", error, "registry-kit-editor-actions");
        actions.append(cancel, save);
        const form = el("form", fields, "registry-kit-editor");
        form.append(actions);
        form.onsubmit = async event => {
          event.preventDefault();
          if (editor.saving) return;
          error.textContent = "";
          editor.saving = save.disabled = cancel.disabled = true;
          try {
            const members = picker.values();
            const result = await post("/api/registry", { action: "save", kind: "kit", id: entry.id, revision,
              name: kit.name, parent: kit.parent, members });
            revision = result.revision;
            savedMembers = JSON.stringify(members);
            await refreshRegistry();
            model.redrawEditor?.();
          } catch (failure) {
            error.textContent = failure.message;
          } finally {
            editor.saving = save.disabled = cancel.disabled = false;
          }
        };
        body.replaceChildren(form);
      }).catch(failure => {
        loading.setAttribute("role", "alert");
        loading.textContent = failure.message;
      });
      return body;
    }

    function cadEditorContents(model) {
      const editor = model.cadEditor;
      if (!editor.body) {
        const host = el("div", "", "workplace-cad");
        editor.body = el("div", host, "registry-window-body registry-cad-editor-content");
        editor.instance = mountRegistryCad(host, editor, () => model.closeEditor(), () => model.redrawEditor());
        host.querySelector(".cad-actions").prepend(button("Новый чертёж", () => openCadWindow(null), { icon: "plus" }));
      }
      return editor.body;
    }

    function desktopWindowName(entry, model) {
      const documentName = model?.cadEditor?.document?.name;
      return documentName ? `${entry.name} · ${documentName}` : entry.name;
    }

    function makeWindow(folder, model) {
      const isTrash = folder === registryTrash;
      const isApp = Boolean(folder.app);
      const isKit = folder.kind === "kit";
      const isCad = folder === registryCad;
      const node = el("section", "", "registry-window");
      node.tabIndex = -1;
      node.dataset.folderId = folder.id;
      node.classList.toggle("is-trash", isTrash);
      node.classList.toggle("is-app", isApp);
      node.classList.toggle("is-kit-editor", isKit);
      node.classList.toggle("is-cad-editor", isCad);
      if (isApp) node.dataset.app = folder.app;
      node.classList.toggle("is-maximized", model.maximized);
      node.style.zIndex = model.z;
      const title = el("h2", registryIcon(folder.icon ?? folder.kind), "registry-window-title");
      title.append(el("span", isKit ? `Состав комплекта · ${folder.name}` : desktopWindowName(folder, model)));
      if (folder.kind === "folder") applyFolderColor(title, folder.color);
      title.id = "registry-window-title-" + folder.id;
      title.title = folder.icon ? desktopWindowName(folder, model) : pathOf(folder);
      node.setAttribute("aria-labelledby", title.id);
      const titlebar = el("header", title, "registry-window-titlebar");
      const controls = el("div", "", "registry-window-controls");
      const windowControl = (label, icon, action) => {
        const control = button(label, action, { icon, iconOnly: true });
        control.classList.add("registry-window-control");
        controls.append(control);
        return control;
      };
      const measureBounds = () => [node.offsetLeft, node.offsetTop, node.offsetWidth, node.offsetHeight];
      const saveBounds = () => {
        registryWindowBounds.set(folder.id, model.bounds);
        saveRegistryPositions(REGISTRY_WINDOW_BOUNDS_KEY, registryWindowBounds);
      };
      const toggleSize = () => {
        if (!model.maximized) model.bounds = measureBounds();
        model.maximized = !model.maximized;
        drawWindows();
        windowNodes.get(folder.id)?.focus({ preventScroll: true });
      };
      if (folder.kind === "folder") {
        const more = windowControl("Действия в папке", "more", () => openBackgroundMenu(folder.id, more));
        more.setAttribute("aria-haspopup", "menu");
        bindRegistryCommands(node, folder.id);
      }
      windowControl("Свернуть", "minimize", () => {
        model.minimized = true;
        drawWindows();
        taskButtons.get(folder.id)?.focus({ preventScroll: true });
      });
      windowControl(model.maximized ? "Восстановить размер" : "Развернуть", model.maximized ? "restore" : "maximize", toggleSize);
      const closeWindow = async () => {
        if (folder === labelsShortcut && !await confirmLeaveLabels()) return;
        if (isKit && !await confirmLeaveKits([model])) return;
        if (isCad && (model.cadEditor.instance.isBusy() || !await confirmLeaveCad([model.cadEditor.instance]))) return;
        if (registryWindows.get(folder.id) !== model) return;
        if (folder === labelsShortcut) labelDesigner = null;
        if (folder === ordersShortcut) { selectedOrder = null; designerOrderView = "orders"; }
        if (isCad) model.cadEditor.instance.destroy();
        if (!model.maximized) model.bounds = measureBounds();
        saveBounds();
        registryWindows.delete(folder.id);
        drawWindows();
        surface.focus({ preventScroll: true });
      };
      if (isKit || isCad) {
        model.closeEditor = closeWindow;
        model.redrawEditor = () => {
          if (!desktop.isConnected || !page.classList.contains("is-registry-desktop")) return;
          const focal = document.activeElement;
          drawViews();
          if (focal?.isConnected) focal.focus({ preventScroll: true });
        };
      }
      windowControl("Закрыть", "close", closeWindow);
      titlebar.append(controls);
      const contents = isCad ? [cadEditorContents(model)] : isKit ? [kitEditorContents(folder, model)]
        : isApp ? [applicationBody(folder, model)] : isTrash ? [registryTrashContent(async () => {
        await refreshRegistry();
        if (desktop.isConnected) drawViews();
      }, updateTrashShortcuts)] : folderWindowContents(folder, model);
      const body = contents.at(-1);
      body.onscroll = () => { model.scroll = body.scrollTop; };
      node.append(titlebar, ...contents);
      node.addEventListener("pointerdown", () => bringForward(folder.id));
      node.addEventListener("focusin", () => bringForward(folder.id));
      function bindWindowGesture(handle, edge = "") {
        let drag = null;
        handle.addEventListener("pointerdown", event => {
          if (event.button !== 0 || !event.isPrimary || model.maximized || event.target.closest("button")) return;
          drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, start: measureBounds(), changed: false };
          if (!edge) node.classList.add("is-moving");
          handle.setPointerCapture(event.pointerId);
        });
        handle.addEventListener("pointermove", event => {
          if (drag?.pointer !== event.pointerId) return;
          const dx = Math.round(event.clientX - drag.x), dy = Math.round(event.clientY - drag.y);
          if (!dx && !dy && !drag.changed) return;
          const [x, y, width, height] = drag.start;
          model.bounds = edge ? resizeWindowBounds(drag.start, edge, dx, dy, isCad) : [
            clamp(x + dx, stage.clientWidth - width), clamp(y + dy, stage.clientHeight - height),
            model.bounds[2] ?? width, model.bounds[3] ?? height,
          ];
          drag.changed = true;
          setWindowBounds(model, node);
        });
        handle.addEventListener("lostpointercapture", event => {
          if (drag?.pointer !== event.pointerId) return;
          if (drag.changed) saveBounds();
          node.classList.remove("is-moving");
          drag = null;
        });
      }
      bindWindowGesture(titlebar);
      for (const edge of registryWindowEdges) {
        const handle = el("div", "", "registry-window-resize");
        handle.dataset.edge = edge;
        handle.setAttribute("aria-hidden", "true");
        bindWindowGesture(handle, edge);
        node.append(handle);
      }
      titlebar.addEventListener("dblclick", event => {
        if (!event.target.closest("button")) toggleSize();
      });
      windowNodes.set(folder.id, node);
      stage.append(node);
      setWindowBounds(model, node);
      body.scrollTop = model.scroll;
    }

    function drawWindows() {
      closeMenu();
      for (const node of windowNodes.values()) node.remove();
      windowNodes.clear();
      taskButtons.clear();
      pinnedApps.replaceChildren();
      openWindows.replaceChildren();
      for (const shortcut of pinnedShortcuts) addTaskButton(shortcut, registryWindows.get(shortcut.id), true);
      for (const [id, model] of registryWindows) {
        const folder = findShortcut(id) ?? find(id);
        if (!folder.app) addTaskButton(folder, model);
        if (!model.minimized) makeWindow(folder, model);
      }
      updateActiveWindow();
      updateWorkClocks();
    }

    function addTaskButton(folder, model, pinned = false) {
      const { id, icon, kind } = folder;
      const name = desktopWindowName(folder, model);
      const task = button("", () => {
        if (model && task.getAttribute("aria-pressed") === "true") {
          model.minimized = true;
          drawWindows();
          taskButtons.get(id)?.focus({ preventScroll: true });
        } else launchShortcut(folder);
      }, { icon: icon ?? kind });
      task.append(el("span", name));
      task.dataset.kind = kind;
      if (kind === "folder") applyFolderColor(task, folder.color);
      task.title = icon ? name : pathOf(folder);
      task.classList.add("registry-task-button");
      task.classList.toggle("is-pinned", pinned);
      task.classList.toggle("is-running", Boolean(model));
      task.setAttribute("aria-label", name);
      if (folder === registryTrash) setTrashShortcutIcon(task);
      taskButtons.set(id, task);
      (pinned ? pinnedApps : openWindows).append(task);
      if (kind === "folder" || folder === registryTrash) bindDrop(task, id);
    }

    function drawViews() {
      drawDesktop();
      drawWindows();
      updateRegistrySelection();
    }

    bindBackground(surface, null, true);
    bindDrop(desktopButton, null);
    desktop.addEventListener("focusin", event => {
      const folder = event.target.closest(".registry-window")?.dataset.folderId;
      keyboardFolder = surface.contains(event.target) ? null : find(folder)?.kind === "folder" ? folder : undefined;
    }, { signal: events.signal });
    document.addEventListener("pointerdown", event => {
      if (!menu.contains(event.target) && !(menuOwner === start && start.contains(event.target))) closeMenu();
    }, { capture: true, signal: events.signal });
    document.addEventListener("keydown", event => {
      if (event.target === document.body && keyboardFolder !== undefined && !page.hidden && desktop.isConnected
          && (event.key === "Delete" || event.code === "Delete")) handleRegistryKeys(event, keyboardFolder);
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        closeMenu(true);
      }
    }, { signal: events.signal });
    menu.addEventListener("keydown", event => {
      const items = [...menu.querySelectorAll("button")];
      const index = items.indexOf(document.activeElement);
      const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (step) {
        event.preventDefault();
        items[(index + step + items.length) % items.length].focus();
      }
    });
    const observer = new ResizeObserver(() => {
      if (measuredWidth !== surface.clientWidth) {
        measuredWidth = surface.clientWidth;
        drawDesktop();
      }
      for (const [id, node] of windowNodes) setWindowBounds(registryWindows.get(id), node);
    });
    observer.observe(stage);
    observer.observe(surface);
    function tickDesktopClock() {
      const now = new Date();
      clock.dateTime = now.toISOString();
      clock.textContent = desktopClockFormat.format(now);
      const today = localDate(now);
      if (date.dateTime !== today) {
        date.dateTime = today;
        date.textContent = showDate(today);
      }
    }
    tickDesktopClock();
    const clockTimer = window.setInterval(tickDesktopClock, 1000);
    registryDispose = () => { observer.disconnect(); events.abort(); clearInterval(clockTimer); };
    drawViews();
  }

  function partViewButton(entry) {
    return button("Просмотр", () => openPartDrawing(entry), { icon: "view", iconOnly: true });
  }

  function openPartDrawing(entry) {
    const dialog = el("dialog", "", "plotter-dialog workplace-dialog registry-drawing-dialog");
    dialog.setAttribute("aria-label", `Просмотр детали «${entry.name}»`);
    const close = button("Закрыть", () => dialog.close(), { icon: "close", iconOnly: true });
    close.autofocus = true;
    const status = el("p", "Загружаем контур…", "registry-drawing-status");
    status.setAttribute("role", "status");
    const sheet = new Image();
    sheet.alt = `Контур детали «${entry.name}» с габаритами, насечками и штампом`;
    sheet.className = "registry-drawing-sheet";
    sheet.hidden = true;
    const body = el("div", status, "registry-drawing-body");
    body.tabIndex = 0;
    body.setAttribute("role", "region");
    body.setAttribute("aria-label", "Контур и штамп детали");
    body.append(sheet);
    const zoom = button("Увеличить", () => {
      const enlarged = dialog.classList.toggle("is-enlarged");
      const label = enlarged ? "Вписать" : "Увеличить";
      zoom.setAttribute("aria-label", label);
      zoom.title = label;
      body.scrollTo(0, 0);
    }, { icon: "zoom", iconOnly: true });
    zoom.hidden = true;
    const actions = el("header", zoom, "registry-drawing-actions");
    actions.append(close);
    sheet.addEventListener("load", () => {
      status.hidden = true;
      sheet.hidden = false;
      zoom.hidden = false;
    }, { once: true });
    sheet.addEventListener("error", () => { status.textContent = "Не удалось загрузить контур детали."; }, { once: true });
    dialog.append(actions, body);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
    sheet.src = `/api/part-preview.svg?${new URLSearchParams({ id: entry.id })}`;
  }

  function mountRegistryCad(host, editor, onBack, onSaved) {
    let record = editor.document;
    const savePart = ({ entities, pattern, partLegend }) => new Promise((resolve, reject) => {
      const hasRememberedFolder = cadPartFolder === "" || find(cadPartFolder)?.kind === "folder";
      const parent = folderSelect(hasRememberedFolder ? cadPartFolder : record?.parent);
      const previous = () => pattern && entries.find(item => item.kind === "part" &&
        item.patternId === pattern.id && item.parent === (parent.value || null));
      const title = input("text", previous()?.name || (pattern ? pattern.number + " · " + pattern.name : ""),
        { required: true, maxLength: 255 });
      const content = el("div", field("Название детали", title), "workplace-form-grid");
      const destination = el("p", "", "workplace-form-note");
      content.append(field("Сохранить в папку", parent), destination);
      const legendEnabled = input("checkbox", "", { checked: Boolean(partLegend) });
      const developer = input("text", partLegend?.developer || "", { maxLength: 255 });
      const comments = el("textarea");
      comments.value = partLegend?.comments || "";
      comments.maxLength = 2000;
      const legendFields = el("div", "", "workplace-form-grid registry-part-legend-form");
      legendFields.append(field("Разработчик", developer), field("Дата составления", input("text", partLegend?.date || "", { readOnly: true })),
        field("Особые комментарии", comments));
      legendFields.hidden = !legendEnabled.checked;
      legendEnabled.onchange = () => { legendFields.hidden = !legendEnabled.checked; };
      content.append(field("Добавить легенду детали", legendEnabled), legendFields);
      const update = el("small");
      const showDestination = () => {
        destination.textContent = "Путь сохранения · " + pathOf(find(parent.value));
        update.textContent = previous() ? "Обновится сохранённая деталь в этой папке." : "";
      };
      if (pattern) {
        content.append(update);
        if (pattern.checkSewing) content.append(el("p", "Линия стачивания менялась в 2D. Сверьте парные кромки и насечки."));
      }
      parent.onchange = showDestination;
      showDestination();
      modal.addEventListener("close", () => { if (modal.returnValue !== "saved") reject(new Error("Сохранение детали отменено.")); }, { once: true });
      formDialog(record?.kind === "part" ? "Сохранить как новую деталь" : "Сохранить деталь", content, "Сохранить", async () => {
        const old = previous();
        const folder = parent.value;
        const part = await post("/api/registry", { action: "save", kind: "part", parent: folder || null,
          name: title.value, entities, ...(pattern ? { pattern } : {}),
          ...(legendEnabled.checked ? { partLegend: { developer: developer.value, comments: comments.value } } : {}),
          ...(old ? { id: old.id, revision: old.revision } : {}) });
        cadPartFolder = folder;
        try { localStorage.setItem(CAD_PART_FOLDER_KEY, folder); } catch { /* Use the in-memory value. */ }
        await refreshRegistry();
        onSaved();
        resolve(part);
      });
    });
    return mountCadEditor(host, { document: record, request, sketchupDesign: sketchupDesign && record?.kind !== "part",
      onBack, onSavePart: savePart, onSaveDocument: async (value) => {
      record = await post("/api/registry", { action: "save", kind: record?.kind ?? "drawing", parent: record?.parent ?? null,
        ...(record ? { id: record.id, revision: record.revision } : {}), ...value });
      editor.document = record;
      await refreshRegistry();
      onSaved();
      return record;
    } });
  }

  async function downloadAttachment(endpoint, fallbackName, failureMessage) {
    const response = await fetch(endpoint, { cache: "no-store" });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.error || failureMessage);
    }
    const filename = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get("Content-Disposition") || "")?.[1];
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename ? decodeURIComponent(filename) : fallbackName;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadSewingPdf(params) {
    return downloadAttachment(`/api/sewing-map.pdf?${new URLSearchParams(params)}`, "Карта пошива.pdf", "Не удалось скачать карту пошива.");
  }

  async function sewingMap(entry) {
    const dialog = el("dialog", "", "plotter-dialog workplace-dialog sewing-map-dialog");
    const heading = el("h2", "Карта пошива · " + entry.name);
    heading.id = "sewing-map-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const loading = el("p", "Загружаем детали…");
    loading.setAttribute("role", "status");
    const body = el("div", loading, "drawing-dialog-body sewing-map-body");
    body.tabIndex = 0;
    body.setAttribute("role", "region");
    body.setAttribute("aria-label", "Детали комплекта");
    body.setAttribute("aria-busy", "true");
    const sheet = el("div", "", "sewing-map-sheet");
    const downloadError = el("small", "", "workplace-dialog-error");
    downloadError.setAttribute("role", "alert");
    const savedLayout = select([["", "Без раскладки"]], "");
    const layoutField = field("Раскладка в PDF", savedLayout);
    layoutField.hidden = true;
    const layouts = [];
    const download = button("Скачать PDF", async () => {
      download.disabled = true;
      downloadError.textContent = "";
      try {
        const chosen = savedLayout.value === "" ? null : layouts[Number(savedLayout.value)];
        await downloadSewingPdf({ kitId: entry.id, ...(chosen ? { widthMm: chosen.widthMm, quantity: chosen.quantity } : {}) });
      } catch (error) { downloadError.textContent = error.message; }
      finally { download.disabled = false; }
    }, { primary: true, disabled: true });
    const close = button("Закрыть", () => dialog.close());
    close.autofocus = true;
    const footer = el("footer", close, "dialog-actions");
    footer.append(layoutField, downloadError, download);
    dialog.append(el("header", heading, "drawing-dialog-heading"), body, footer);
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
    try {
      const kit = await get("/api/registry", { id: entry.id });
      if (!dialog.open) return;
      const parts = await Promise.all(kit.members.map(async member =>
        ({ ...await get("/api/registry", { id: member.id }), amount: member.quantity })));
      if (!dialog.open) return;
      heading.textContent = "Карта пошива · " + kit.name;
      for (const layout of kit.layouts?.filter((item) => item.valid) ?? []) {
        savedLayout.add(new Option(`${layout.widthMm} мм · ${layout.quantity} компл.`, String(layouts.length)));
        layouts.push(layout);
      }
      layoutField.hidden = !layouts.length;
      if (!parts.some(part => part.pattern?.sewing?.length)) {
        sheet.append(el("p", "В комплекте нет сохранённых швов.", "sewing-map-note"));
      }
      const numbers = new Set(parts.map(part => part.pattern?.number));
      for (const part of parts) {
        const pattern = part.pattern;
        const title = pattern ? pattern.number + " · " + pattern.name : part.name;
        const article = el("article", "", "sewing-map-part");
        const outline = el("canvas");
        outline.width = 360; outline.height = 220;
        outline.setAttribute("role", "img");
        outline.setAttribute("aria-label", "Контур детали " + title);
        const context = outline.getContext("2d");
        if (context) context.lineWidth = 2;
        const project = drawPartOutline(outline, part.paths || []);
        if (project) {
          for (const mark of (part.entities || []).filter(item => item.type === "point" && item.layer.startsWith("Метка "))) {
            const [x, y] = project(mark.point);
            context.fillStyle = "#284994";
            context.fillRect(x - 3, y - 3, 6, 6);
            context.font = "20px sans-serif";
            context.fillText(mark.layer.slice(6), Math.min(x + 8, 270), Math.max(y - 8, 20), 90);
          }
        }
        const side = ({ left: "левая", right: "правая" })[pattern?.side];
        const details = el("div", el("h3", title));
        details.append(el("p", [pattern?.fabricName, side, part.amount + " шт."].filter(Boolean).join(" · "), "sewing-map-meta"));
        if (pattern?.checkSewing) details.append(el("strong", "Проверьте сопряжения после правки в 2D."));
        for (const seam of pattern?.sewing || []) {
          details.append(el("p", seam.name + " → " + seam.other + (seam.method === "closure" ? " · застёжка" : "") +
            (seam.marks.length ? " · " + seam.marks.join(", ") : "") +
            (numbers.has(seam.other) ? "" : " · ответная деталь отсутствует в комплекте")));
        }
        article.append(details, outline);
        sheet.append(article);
      }
      body.replaceChildren(sheet);
      download.disabled = false;
    } catch (error) {
      if (dialog.open) {
        const message = el("p", error.message, "workplace-dialog-error");
        message.setAttribute("role", "alert");
        body.replaceChildren(message);
      }
    } finally {
      body.removeAttribute("aria-busy");
    }
  }

  function openLayoutPart(document, { onBack, onSaveDocument }) {
    const host = el("div", "", "workplace-cad");
    const back = execute(async () => {
      if (!await confirmLeaveCad([layoutCad])) return;
      layoutCad?.destroy();
      layoutCad = null;
      page.hidden = true;
      page.classList.remove("is-cad");
      panel.replaceChildren();
      onBack();
    });
    layoutCad?.destroy();
    page.classList.add("is-cad");
    panel.replaceChildren(host);
    page.hidden = false;
    layoutCad = mountCadEditor(host, { document, request, layoutPart: true, onBack: back, onSaveDocument });
  }

  function orderDialog(old) {
    const number = input("text", old?.number ?? "", { required: true, maxLength: 255 });
    const designation = input("text", old?.designation ?? "", { maxLength: 255, placeholder: "Например, Диван Астон, левый модуль" });
    const notes = Object.assign(el("textarea"), { value: old?.notes ?? "", maxLength: 2000, rows: 3,
      placeholder: "Например, сначала раскроить подкладку" });
    const fields = el("div", field("Номер заказа", number), "workplace-form-grid");
    fields.append(field("Что кроим", designation), field("Комментарий для оператора", notes));
    formDialog(old ? "Изменить наряд" : "Новый наряд", fields, "Сохранить", async () => {
      const result = await post("/api/orders", { action: old ? "update-meta" : "save", number: number.value, notes: notes.value,
        designation: designation.value, ...(old ? { id: old.id, revision: old.revision } : {}) });
      selectedOrder = result.id;
      await render();
    });
  }

  function cutDialog(order, old) {
    const title = input("text", old?.name ?? "", { required: true, maxLength: 255, placeholder: "Например, Подкладка" });
    const picker = itemPicker(["part", "kit"], old?.members, { mode: "fabric" });
    const content = el("div", field("Название ткани", title), "workplace-form-grid workplace-fabric-form");
    content.append(picker.section);
    formDialog(old ? "Изменить ткань" : "Добавить ткань", content, "Сохранить", async () => {
      await post("/api/orders", { action: "save-cut", orderId: order.id, orderRevision: order.revision, id: old?.id,
        name: title.value, members: picker.values() });
      await render();
    });
  }

  async function showLayout(next) {
    const result = await navigateLayout(next);
    if (result === false) return;
    closeRegistryView();
    topbar.hidden = false;
    clearInterval(workClock);
    currentLayout = next;
    const mode = next.kitId ? "kit" : result?.demo ? "demo" : next.mode;
    const order = result?.order || next.order;
    const fabric = result?.name || next.name;
    const caption = next.id ? [order?.number && `Наряд № ${order.number}`, orderProduct(order), fabric].filter(Boolean).join(" · ") : "";
    const heading = el("strong", mode === "kit" ? `Раскладка комплекта · ${result?.name || next.name}`
      : caption || fabric || "Самостоятельный раскрой");
    if (mode === "design" || mode === "preview") heading.append(` · ${mode === "design" ? "Подготовка" : "Предпросмотр"}`);
    if (mode === "demo") heading.append(" · Демо");
    const error = el("span", "", "workplace-dock-error");
    error.setAttribute("role", "alert");
    const back = button(mode === "kit" ? "К реестру" : next.id ? "К наряду" : "К нарядам", execute(async () => {
      if (!await leaveLayout()) return;
      await render();
      focusWorkplaceHeading();
    }));
    dock.replaceChildren(back, heading, error);
    if (mode === "kit") {
      dock.append(button("Состав комплекта", execute(() => kitCompositionDialog(next.kitId)), { icon: "kit" }));
    }
    dock.append(button("Скачать карту пошива", execute(async () => {
      if (mode !== "kit" && flushLayout && !await flushLayout()) {
        throw new Error("Сохраните раскладку перед скачиванием карты пошива.");
      }
      const params = mode === "kit" ? { kitId: next.kitId, widthMm: result.widthMm, quantity: result.quantity }
        : next.id ? { cutId: next.id, mode: next.previewSource ?? mode } : { workspace: 1 };
      await downloadSewingPdf(params);
    })));
    dock.append(xiaoActions, printerActions);
    if (mode === "design") {
      dock.append(button("Предпросмотр", execute(() => showLayout({ ...next, mode: "preview", editable: true }))));
    } else if (mode === "preview") {
      if (next.editable) dock.append(button("Редактировать раскладку", execute(() => showLayout({ ...next, mode: "design" }))));
    } else if (mode !== "kit" && (result?.status !== "done" || mode === "demo")) {
      dock.append(button("Добавить файл", execute(async () => {
        await refreshRegistry();
        const picker = itemPicker(["part", "kit"]);
        if (!next.id) picker.section.prepend(button("Открыть DXF / DWG с диска", () => { modal.close(); openFile(); }));
        formDialog("Добавить из реестра", picker.section, "Добавить на стол", async () => {
          const result = await post("/api/registry", { action: "collect", members: picker.values() });
          addRegisteredParts(result.paths);
        });
      })));
      if (mode === "operator" && result?.canReleaseCut) dock.append(button("Вернуть в очередь", execute(async () => {
        await releaseRegisteredCut();
        await leaveLayout();
        await render();
      })));
      if (mode === "operator") dock.append(button("Отметить раскрой выполненным", execute(async () => {
        if (!await confirmAction({
          title: "Отметить раскрой выполненным?",
          message: `Подтвердите, что раскрой «${fabric}» фактически выполнен.`,
          confirmLabel: "Отметить выполненным",
        })) return;
        await finishRegisteredCut();
        await leaveLayout();
        await render();
        focusWorkplaceHeading();
      }), { primary: true }));
    }
    dock.hidden = false;
  }

  async function leaveLayout() {
    if (!currentLayout) return true;
    if (await navigateLayout(null) === false) return false;
    currentLayout = null;
    return true;
  }

  function updateWorkClocks() {
    clearInterval(workClock);
    const running = panel.querySelectorAll("[data-work-started]");
    if (!running.length) return;
    const tick = () => {
      const now = Date.now();
      for (const timer of running) timer.textContent = workDuration((now - Number(timer.dataset.workStarted)) / 1000);
    };
    tick();
    workClock = window.setInterval(tick, 1000);
  }

  async function openOrder(id) {
    const operator = tab === "operator";
    const host = operator ? panel : registryWindows.get(ordersShortcut.id)?.appBody;
    if (!host) return;
    selectedOrder = id;
    await renderOrdersView(host, operator);
    const viewport = operator ? page : host;
    viewport.scrollTop = 0;
    focusWorkplaceHeading(host);
  }

  function renderOrder(order, host) {
    const operator = tab === "operator";
    const draft = !order.published;
    const done = order.cuts.filter(cut => cut.status === "done").length;
    const actions = [button(designerOrderView === "analysis" && !operator ? "К анализу" : "Все наряды",
      execute(() => openOrder(null)), { icon: "back" })];
    if (!operator) {
      const canReopen = order.cuts.every(cut => cut.status === "pending");
      const hasActiveCut = order.cuts.some(cut => cut.status === "active");
      actions.push(button("Изменить наряд", () => orderDialog(order)), button("Добавить ткань", () => cutDialog(order)));
      if (order.published && order.cuts.length && canReopen) actions.push(button("Вернуть в черновики", execute(async () => {
        formDialog("Изменить отправленный наряд?", el("p", "Наряд вернётся в черновики. После правок отправьте его оператору повторно."), "Вернуть в черновики", async () => {
          await post("/api/orders", { action: "reopen", id: order.id, revision: order.revision });
          await render();
        });
      })));
      actions.push(button("Удалить наряд", execute(async () => {
        if (hasActiveCut) throw new Error("В наряде есть раскрой в работе. Завершите его на рабочем месте оператора перед удалением.");
        formDialog(`Удалить наряд № ${order.number}?`, el("p", "Наряд исчезнет из списков. Лекала останутся в реестре, а история раскроя сохранится."), "Удалить наряд", async () => {
          await post("/api/orders", { action: "delete", id: order.id, revision: order.revision });
          selectedOrder = null;
          await render();
        });
      })));
    }
    if (!operator && draft && order.cuts.length) actions.push(button("Отправить оператору", execute(async () => {
      if (!await confirmAction({
        title: `Отправить наряд № ${order.number} оператору?`,
        confirmLabel: "Отправить",
      })) return;
      await post("/api/orders", { action: "publish", id: order.id, revision: order.revision });
      await render();
      focusWorkplaceHeading();
    }), { primary: true }));
    host.append(workHeading(`Наряд № ${order.number}`, orderContext(order), actions));
    const overview = el("section", orderDates(order), "workplace-order-overview");
    overview.append(progressRow(done, order.cuts.length));
    if (draft) overview.append(el("span", "Черновик", "workplace-chip"));
    if (order.notes) overview.append(el("p", order.notes, "workplace-order-notes"));
    host.append(overview);
    const finished = done > 0 && done === order.cuts.length;
    const totals = workStats(order.stats, { order: true, finished, draft });
    totals.classList.add("workplace-order-totals");
    host.append(totals);
    host.append(el("h2", `Ткани · ${order.cuts.length}`, "workplace-section-title"));
    const list = el("div", "", "workplace-cards workplace-cut-list");
    for (const cut of order.cuts) {
      const layout = { id: cut.id, order, name: cut.name };
      const row = el("article", "", "workplace-card workplace-cut-row");
      const header = el("header", el("h3", cut.name), "workplace-fabric-heading");
      const tools = el("div", "", "workplace-actions workplace-card-tools");
      const released = cut.released !== false;
      if (!operator || released) {
        const editable = !operator && (draft || (!released && cut.status === "pending"));
        enableCardOpen(row, `Открыть раскладку ткани «${cut.name}»`, execute(() =>
          showLayout({ ...layout, mode: "preview", previewSource: operator ? "operator" : undefined, editable })));
      }
      const draftState = cut.prepared ? "Готов к отправке" : cut.widthMm == null ? "Раскладка у оператора" : "Требуется раскладка";
      const state = draft ? draftState
        : released ? states[cut.status] : "Конструктор обновляет раскладку";
      header.append(el("span", state, `workplace-chip ${draft ? "draft" : cut.status}`));
      if (operator && released) {
        if (cut.status !== "done") tools.append(button(cut.status === "pending" ? "Приступить" : "Продолжить",
          execute(() => showLayout({ ...layout, mode: "operator" })), { primary: true }));
        tools.append(button("Демо", execute(() => showLayout({ ...layout, mode: "demo" }))));
      } else if (!operator && (draft || cut.status === "pending")) {
        tools.append(button("Изменить ткань и состав", () => cutDialog(order, cut)));
        if (!draft && !released) tools.append(button("Отправить раскладку", execute(async () => {
          await post("/api/orders", { action: "publish-cut", id: cut.id, orderId: order.id, orderRevision: order.revision });
          await render();
        }), { primary: true }));
        tools.append(button("Удалить ткань", execute(async () => {
          if (!await confirmAction({
            title: `Удалить пункт «${cut.name}»?`,
            message: "Состав и раскладка для этой ткани будут удалены из наряда.",
            confirmLabel: "Удалить",
          })) return;
          await post("/api/orders", { action: "remove-cut", id: cut.id, orderId: order.id, orderRevision: order.revision });
          await render();
          focusWorkplaceHeading();
        })));
      }
      row.append(header, workStats(cut.stats, { finished: cut.status === "done", draft }));
      if (tools.childElementCount) row.append(tools);
      list.append(row);
    }
    if (!list.children.length) list.append(el("p", "Добавьте ткань и выберите детали для раскладки.", "workplace-empty"));
    host.append(list);
  }

  function progressRow(done, total) {
    const percent = progressPercent(done, total);
    const row = el("div", "", "workplace-progress");
    const count = `${done} из ${total}`;
    const label = el("span", "Готово тканей ", "workplace-progress-label");
    label.append(el("strong", count));
    const bar = el("progress");
    Object.assign(bar, { max: 100, value: percent, ariaLabel: "Выполнение наряда" });
    bar.setAttribute("aria-valuetext", `Готово тканей: ${count} (${percent}%)`);
    row.append(label, el("span", `${percent}%`, "workplace-progress-percent"), bar);
    return row;
  }

  function analysisChart(days) {
    const chart = el("section", "", "workplace-analysis-section");
    const title = el("h2", "Выработка по дням");
    const controls = el("div", "", "workplace-chart-series");
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Показатель выработки");
    const header = el("header", title, "workplace-chart-heading");
    header.append(controls);
    const bars = el("ol", "", "workplace-chart-bars");
    bars.tabIndex = 0;
    chart.append(header, bars);
    const series = [
      ["fabricMetersPerHour", "Полотно", "пог. м/ч"],
      ["cutMetersPerHour", "Рез", "м/ч"],
    ];
    const switches = series.map(([key, label]) => button(label, () => { analysisSeries = key; draw(); }));
    controls.append(...switches);
    function draw() {
      const [, label, unit] = series.find(([key]) => key === analysisSeries);
      switches.forEach((control, index) => control.setAttribute("aria-pressed", String(series[index][0] === analysisSeries)));
      bars.setAttribute("aria-label", `${label} по дням, ${unit}`);
      bars.replaceChildren();
      const max = days.reduce((peak, day) => Math.max(peak, day[analysisSeries] ?? 0), 0);
      for (const day of days) {
        const value = day[analysisSeries];
        const date = el("time", showDate(day.date));
        date.dateTime = day.date;
        const bar = el("span", "", "workplace-chart-track");
        bar.setAttribute("aria-hidden", "true");
        if (Number.isFinite(value)) {
          const fill = el("span");
          fill.style.width = `${max > 0 ? value / max * 100 : 0}%`;
          bar.append(fill);
        }
        const row = el("li", date);
        row.append(bar, el("strong", metricValue(value, unit)));
        bars.append(row);
      }
      if (!days.length) bars.append(el("li", "Нет тканей с зафиксированной датой завершения в выбранном периоде.", "workplace-empty"));
    }
    draw();
    return chart;
  }

  function analysisOrders(orders) {
    const section = el("section", el("h2", "Наряды за период"), "workplace-analysis-section");
    if (!orders.length) {
      section.append(el("p", "Наряды появятся здесь, когда будет записана дата завершения ткани за выбранный период.", "workplace-empty"));
      return section;
    }
    const scroll = el("div", "", "workplace-analysis-table");
    scroll.tabIndex = 0;
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", "Таблица нарядов за период");
    const table = el("table");
    table.append(el("caption", "Показатели по наряду целиком. Отклонение доступно для нарядов, закрытых за выбранный период."));
    const heading = table.createTHead().insertRow();
    const sort = button("", () => { analysisDeltaDescending = !analysisDeltaDescending; draw(); });
    for (const label of ["Наряд", "Завершено тканей за период", "Полотно", "КИМ", "Расчёт", "Факт наряда", sort]) {
      const th = el("th", label);
      th.scope = "col";
      heading.append(th);
    }
    const body = table.createTBody();
    function draw() {
      const sorted = [...orders].sort((left, right) => {
        const a = left.stats?.deltaSeconds, b = right.stats?.deltaSeconds;
        if (!Number.isFinite(a)) return Number.isFinite(b) ? 1 : 0;
        if (!Number.isFinite(b)) return -1;
        return analysisDeltaDescending ? b - a : a - b;
      });
      sort.textContent = `Отклонение ${analysisDeltaDescending ? "↓" : "↑"}`;
      sort.setAttribute("aria-label", `Сортировать отклонение по ${analysisDeltaDescending ? "возрастанию" : "убыванию"}`);
      sort.parentElement.setAttribute("aria-sort", analysisDeltaDescending ? "descending" : "ascending");
      body.replaceChildren();
      for (const order of sorted) {
        const stats = order.stats ?? {};
        const row = body.insertRow();
        const identity = el("th", button(`№ ${order.number}`, execute(() => openOrder(order.id))));
        identity.scope = "row";
        if (orderContext(order)) identity.append(el("small", orderContext(order)));
        if (!order.closedInPeriod) identity.append(el("small", stats.finishedAt ? "Закрыт вне периода"
          : order.total && order.done === order.total ? "Дата закрытия неизвестна" : "В работе"));
        row.append(identity);
        for (const value of [order.periodCutCount ?? noMetric, metricValue(stats.lengthMm, "пог. м", 1000),
          metricValue(stats.utilizationPercent, "%"), workDuration(stats.estimatedWorkSeconds),
          workDuration(stats.actualSeconds), workDelta(stats.deltaSeconds)]) row.insertCell().append(value);
      }
    }
    draw();
    scroll.append(table);
    section.append(scroll);
    return section;
  }

  function renderAnalysis({ summary, days, orders }, host) {
    const back = button("Все наряды", execute(async () => {
      designerOrderView = "orders";
      await openOrder(null);
    }), { icon: "back" });
    host.append(workHeading("Анализ раскроя", "", [back]));
    const from = input("date", analysisPeriod.from, { required: true });
    const to = input("date", analysisPeriod.to, { required: true });
    const filter = el("form", "", "workplace-analysis-filter");
    const apply = el("button", "Применить", "button primary");
    apply.type = "submit";
    function limitPeriod() {
      from.max = to.value;
      to.min = from.value;
    }
    limitPeriod();
    from.oninput = to.oninput = limitPeriod;
    filter.append(field("С", from), field("По", to), apply);
    filter.onsubmit = event => {
      event.preventDefault();
      void execute(async () => {
        analysisPeriod = { from: from.value, to: to.value };
        await renderOrdersView(host, false);
        focusWorkplaceHeading(host);
      })();
    };
    host.append(filter);
    const summaryGrid = el("div", "", "workplace-analysis-summary");
    const speeds = values => values?.length
      ? `${values.map(value => metricNumber.format(value)).join(", ")} мм/с` : noMetric;
    const coverage = (known, total) => Number.isFinite(known) && Number.isFinite(total) ? `${known} из ${total}` : noMetric;
    summaryGrid.append(
      workMetrics("Завершено за период", "check", [
        ["Наряды", summary.completedOrders], ["Ткани", summary.completedCuts], ...materialValues(summary),
      ]),
      workMetrics("Время", "clock", [
        ["Чистое расчётное", workDuration(summary.estimatedWorkSeconds)],
        ["Факт закрытых нарядов", workDuration(summary.orderActualSeconds)],
        ["Сумма по тканям", workDuration(summary.fabricActualSeconds)],
        ["Ткани с фактом", coverage(summary.timedCount, summary.completedCuts)],
        ["Скорость реза", speeds(summary.cutSpeedsMmS)], ["Скорость хода", speeds(summary.travelSpeedsMmS)],
      ]),
      workMetrics("Выработка", "analysis", [
        ["Полотно в час", metricValue(summary.fabricMetersPerHour, "пог. м/ч")],
        ["Рез в час", metricValue(summary.cutMetersPerHour, "м/ч")],
        ["Эффективность (расчёт / факт)", metricValue(summary.efficiencyPercent, "%")],
        ["Ткани с расчётом и фактом", coverage(summary.pairedCount, summary.completedCuts)],
      ]),
    );
    if (summary.plannedCount < summary.completedCuts || summary.timedOrderCount < summary.completedOrders) {
      const details = [];
      if (summary.plannedCount < summary.completedCuts) {
        details.push(`Расчёт для ${coverage(summary.plannedCount, summary.completedCuts)} тканей.`);
      }
      if (summary.timedOrderCount < summary.completedOrders) {
        details.push(`Фактическое время для ${coverage(summary.timedOrderCount, summary.completedOrders)} нарядов.`);
      }
      summaryGrid.append(el("p", details.join(" "), "workplace-metric-note"));
    }
    host.append(summaryGrid, analysisChart(days), analysisOrders(orders));
  }

  function renderOrderCard(order, state, heading = "h2") {
    const card = el("article", "", "workplace-card workplace-order-card");
    card.dataset.orderStatus = state;
    enableCardOpen(card, `Открыть наряд № ${order.number}`, execute(() => openOrder(order.id)));
    const identity = el("div", el(heading, `Наряд № ${order.number}`), "workplace-card-identity");
    identity.prepend(el("span", state === "draft" ? "Черновик" : states[state], `workplace-chip ${state}`));
    const detail = orderContext(order);
    if (detail) identity.append(el("p", detail));
    const summary = el("div", "", "workplace-card-materials");
    summary.append(el("span", "Ткани", "workplace-order-label"));
    const fabrics = order.materials?.map(material => material.widthMm == null ? material.name : `${material.name} · ${material.widthMm} мм`);
    summary.append(el("span", fabrics?.join(", ") || "Ткани не добавлены"));
    const header = el("header", identity, "workplace-card-heading");
    header.append(orderDates(order));
    card.append(header, summary, progressRow(order.done, order.total), workStats(order.stats, {
      order: true, finished: state === "done", draft: !order.published,
    }));
    if (order.notes) card.append(el("p", order.notes, "workplace-card-notes"));
    return card;
  }

  function renderOrderList(orders, operator, host) {
    const sorted = orders.map(order => ({
      order,
      state: orderState(order),
      search: normalizeOrderSearch([`Наряд № ${order.number}`, order.designation, order.modelName,
        ...(order.materials ?? []).map(material => material.name)].join(" ")),
    })).sort((a, b) => orderStateRank[a.state] - orderStateRank[b.state]
      || (b.order.placedOn ?? "").localeCompare(a.order.placedOn ?? ""));

    const tools = el("div", "", "workplace-order-tools");
    const search = input("search", orderList.query, { placeholder: "Номер, изделие или ткань" });
    search.setAttribute("aria-label", "Поиск нарядов");
    const searchField = el("label", registryIcon("search"), "workplace-order-search");
    searchField.append(search);
    const filters = el("div", "", "workplace-order-filters");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "Статус наряда");
    const options = [["", "Текущие"], ["done", "Завершённые"]];
    const controls = options.map(([value, title]) => button(title, () => {
      orderList.filter = value;
      orderList.page = 1;
      showResults();
    }));
    filters.append(...controls);
    tools.append(searchField, filters);

    const cards = el("div", "", "workplace-cards workplace-order-list");
    cards.tabIndex = -1;
    const pages = el("nav", "", "workplace-order-pages");
    pages.setAttribute("aria-label", "Страницы нарядов");
    const pageStatus = el("span");
    pageStatus.setAttribute("role", "status");
    const previous = button("Назад", () => changePage(-1));
    const next = button("Далее", () => changePage(1));
    pages.append(previous, pageStatus, next);
    const results = el("div", pages, "workplace-order-results");
    results.append(cards);
    host.append(tools, results);

    search.addEventListener("input", () => {
      orderList.query = search.value;
      orderList.page = 1;
      showResults();
    });

    function changePage(direction) {
      orderList.page += direction;
      showResults();
      const viewport = operator ? page : host;
      viewport.scrollTop = 0;
      cards.focus({ preventScroll: true });
    }

    function showResults() {
      const words = normalizeOrderSearch(orderList.query).trim().split(/\s+/).filter(Boolean);
      const matches = sorted.filter(({ state, search }) => {
        if (!words.every(word => search.includes(word))) return false;
        if (orderList.filter === "done") return state === "done";
        return state !== "done" && (!operator || state !== "draft");
      });
      const pageCount = Math.max(1, Math.ceil(matches.length / ordersPerPage));
      orderList.page = Math.min(orderList.page, pageCount);
      const offset = (orderList.page - 1) * ordersPerPage;
      const visible = matches.slice(offset, offset + ordersPerPage);
      cards.replaceChildren();
      if (!visible.length) {
        const message = words.length ? "Наряды не найдены."
          : orderList.filter === "done" ? "Нет завершённых нарядов." : "Нет текущих нарядов.";
        cards.append(el("p", message, "workplace-empty workplace-order-empty"));
      } else if (operator || orderList.filter === "done") {
        cards.append(...visible.map(({ order, state }) => renderOrderCard(order, state)));
      } else {
        for (const [state, title] of currentOrderGroups) {
          const groupOrders = visible.filter(item => item.state === state);
          if (!groupOrders.length) continue;
          const heading = el("h2", title, "workplace-order-group-title");
          heading.id = `workplace-order-group-${state}`;
          const group = el("section", heading, "workplace-order-group");
          group.setAttribute("aria-labelledby", heading.id);
          group.dataset.state = state;
          const list = el("div", "", "workplace-cards workplace-order-list");
          list.append(...groupOrders.map(({ order }) => renderOrderCard(order, state, "h3")));
          group.append(list);
          cards.append(group);
        }
      }
      controls.forEach((control, index) => control.setAttribute("aria-pressed", String(options[index][0] === orderList.filter)));
      pages.hidden = pageCount === 1;
      previous.disabled = orderList.page === 1;
      next.disabled = orderList.page === pageCount;
      pageStatus.textContent = `Страница ${orderList.page} из ${pageCount}`;
      clearInterval(workClock);
      updateWorkClocks();
    }

    showResults();
  }

  async function renderOrdersView(host, operator) {
    const epoch = ++orderRenderEpoch;
    const id = selectedOrder;
    const showReport = !operator && designerOrderView === "analysis" && !id;
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const params = operator ? { published: 1 } : {};
    const result = showReport ? await get("/api/order-analysis", { ...analysisPeriod, tz })
      : await get("/api/orders", id ? { ...params, id } : params);
    if (epoch !== orderRenderEpoch || tab !== (operator ? "operator" : "designer")
        || (!operator && registryWindows.get(ordersShortcut.id)?.appBody !== host)) return;
    host.replaceChildren();
    if (showReport) { renderAnalysis(result, host); updateWorkClocks(); return; }
    if (id) { renderOrder(result, host); updateWorkClocks(); return; }
    const actions = operator ? [button("Новый раскрой", execute(() => showLayout({ mode: "standalone" })), { primary: true })]
      : [button("Анализ", execute(async () => { designerOrderView = "analysis"; await openOrder(null); }), { icon: "analysis" }),
        button("Новый наряд", () => orderDialog(null), { primary: true, icon: "plus" })];
    if (operator) actions.unshift(button("Продолжить раскрой", execute(async () => {
      const { id, mode } = await getActiveCut();
      const order = id && result.orders.find(order => order.materials?.some(material => material.id === id));
      const material = order?.materials.find(material => material.id === id);
      await showLayout(id && (mode !== "demo" || material) ? { mode, id, order, name: material?.name } : { mode: "standalone" });
    })));
    if (operator) host.append(workHeading("Наряды на раскрой", "", actions));
    else {
      const tools = el("div", "", "workplace-actions");
      tools.append(...actions);
      host.append(tools);
    }
    renderOrderList(result.orders, operator, host);
  }

  async function render() {
    const epoch = ++renderEpoch;
    clearInterval(workClock);
    page.classList.remove("is-cad");
    if (tab === "designer") await refreshRegistry();
    if (epoch !== renderEpoch || !tab) return;
    closeRegistryView();
    panel.replaceChildren();
    if (tab === "designer") {
      renderRegistry();
      const orders = registryWindows.get(ordersShortcut.id)?.appBody;
      if (orders) await renderOrdersView(orders, false);
    } else {
      topbar.hidden = false;
      await renderOrdersView(panel, true);
    }
  }

  function saveRole(role) {
    try {
      if (role) sessionStorage.setItem(ROLE_STORAGE_KEY, role);
      else sessionStorage.removeItem(ROLE_STORAGE_KEY);
    } catch {
      // The role still works when browser storage is unavailable.
    }
  }

  function storedRole() {
    try {
      const name = sessionStorage.getItem(ROLE_STORAGE_KEY);
      return Object.hasOwn(roleNames, name) ? name : null;
    } catch {
      return null;
    }
  }

  function saveDesignerAccess(token) {
    try {
      if (token) sessionStorage.setItem(DESIGNER_ACCESS_KEY, token);
      else sessionStorage.removeItem(DESIGNER_ACCESS_KEY);
    } catch { /* The role is still available until this page is reloaded. */ }
  }

  function showDesignerAccess() {
    const pin = input("password", "", { inputMode: "numeric", autoComplete: "off",
      pattern: "[0-9]{4}", minLength: 4, maxLength: 4, required: true, autofocus: true });
    formDialog("Вход конструктора", field("ПИН-код", pin), "Войти", async () => {
      let access;
      try { access = await post("/api/designer-access", { action: "enter", pin: pin.value }); }
      finally { pin.value = ""; }
      await enterRole("designer");
      saveDesignerAccess(access.token);
    }, { lockWhileSaving: true });
    modal.addEventListener("close", () => { if (tab === "designer") focusWorkplaceHeading(); }, { once: true });
  }

  function focusPicker() {
    $("role-picker-title").focus({ preventScroll: true });
  }

  function focusWorkplaceHeading(host = panel) {
    const heading = host.querySelector("h1") ?? host.querySelector(".registry-desktop-surface") ?? (host === panel ? null : host);
    if (!heading) return;
    if (heading.tagName === "H1") heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }

  function confirmLeaveCad(editors) {
    return !editors.some(editor => editor?.hasUnsavedChanges()) || confirmAction({
      title: "Закрыть чертёж?",
      message: "Несохранённые изменения будут потеряны.",
      confirmLabel: "Закрыть без сохранения",
      cancelLabel: "Продолжить редактирование",
    });
  }

  function confirmLeaveLabels() {
    return !labelDesigner?.dirty() || confirmAction({
      title: "Закрыть конструктор бирок?",
      message: "Несохранённые изменения шаблона будут потеряны.",
      confirmLabel: "Закрыть без сохранения",
      cancelLabel: "Продолжить редактирование",
    });
  }

  function confirmLeaveKits(models) {
    const editors = Array.from(models, model => model.kitEditor).filter(Boolean);
    if (editors.some(editor => editor.saving)) return false;
    return !editors.some(editor => editor.dirty?.()) || confirmAction({
      title: "Закрыть редактор состава?",
      message: "Несохранённые изменения комплекта будут потеряны.",
      confirmLabel: "Закрыть без сохранения",
      cancelLabel: "Продолжить редактирование",
    });
  }

  async function enterRole(name) {
    tab = name;
    try {
      await render();
    } catch (error) {
      tab = null;
      panel.replaceChildren();
      throw error;
    }
    saveRole(name);
    roleLabel.textContent = roleNames[name];
    picker.hidden = true;
    topbar.hidden = name === "designer";
    page.hidden = false;
    focusWorkplaceHeading();
  }

  const leaveRole = execute(async () => {
    if (cadInstances().some(editor => editor.isBusy())) return;
    if (!await confirmLeaveCad(cadInstances())) return;
    if (!await confirmLeaveLabels()) return;
    if (!await confirmLeaveKits(registryWindows.values())) return;
    if (!await leaveLayout()) return;
    await onExit?.();
    for (const editor of cadInstances()) editor.destroy();
    layoutCad = null;
    labelDesigner = null;
    tab = selectedOrder = null;
    registryWindows.clear();
    registrySelection.clear();
    registryClipboard = null;
    closeRegistryView();
    orderList.query = orderList.filter = "";
    orderList.page = 1;
    designerOrderView = "orders";
    analysisPeriod = defaultAnalysisPeriod();
    clearInterval(workClock);
    saveRole(null);
    saveDesignerAccess(null);
    page.classList.remove("is-cad");
    panel.replaceChildren();
    topbar.hidden = page.hidden = true;
    picker.hidden = false;
    focusPicker();
  });
  exit.onclick = leaveRole;
  $("operator-tab").onclick = execute(() => enterRole("operator"));
  $("designer-tab").onclick = showDesignerAccess;
  window.addEventListener("beforeunload", event => {
    if (!cadInstances().some(editor => editor.hasUnsavedChanges()) && !labelDesigner?.dirty()
        && ![...registryWindows.values()].some(model => model.kitEditor?.dirty?.())) return;
    event.preventDefault();
    event.returnValue = "";
  });
  const initialRole = storedRole();
  if (initialRole === "designer") void execute(async () => {
    try {
      const token = sessionStorage.getItem(DESIGNER_ACCESS_KEY);
      if (!token) throw new Error("Требуется вход конструктора.");
      await post("/api/designer-access", { action: "check", token });
    } catch {
      saveRole(null);
      saveDesignerAccess(null);
      focusPicker();
      return;
    }
    await enterRole("designer");
  })();
  else if (initialRole === "operator") void execute(() => enterRole("operator"))();
  else focusPicker();
  return { openLayoutPart, hasPendingCadEdits: () => cadInstances().some(editor => editor.hasUnsavedChanges() || editor.isBusy()) };
}
