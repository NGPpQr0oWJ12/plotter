import { mountCadEditor } from "/designer-cad.js";
import { confirmAction } from "/confirm-action.js";

const $ = (id) => document.getElementById(id);
const labels = { folder: "Папка", drawing: "Чертёж", part: "Деталь", kit: "Комплект" };
const states = { pending: "Ожидает", active: "В работе", done: "Выполнен" };
const kitPartDragType = "application/x-plotter-kit-part";

function registryIcon(kind) {
  const paths = {
    folder: "M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
    drawing: "M5 3h9l5 5v13H5ZM14 3v6h5m-11 8 3-5 4 5Z",
    part: "M4 7l4-3h9l3 4v12H4ZM4 14h3m10 0h3",
    kit: "m12 3 9 5-9 5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
    chevron: "m9 6 6 6-6 6",
    chevronDown: "m6 9 6 6 6-6",
    search: "M15.5 10a5.5 5.5 0 1 1-11 0 5.5 5.5 0 0 1 11 0Zm-1.6 3.9 5.6 5.6",
    filter: "M4 6h16l-6 7v5l-4 2v-7Z",
    check: "m5 12 4 4L19 6",
    sidebarCollapse: "M3 5h18v14H3ZM9 5v14m7-10-3 3 3 3",
    sidebarExpand: "M3 5h18v14H3ZM9 5v14m4-10 3 3-3 3",
    open: "M3 18V6a2 2 0 0 1 2-2h4l2 3h7a2 2 0 0 1 2 2m-17 9 3-8h15l-3 9H5a2 2 0 0 1-2-1Z",
    view: "M2.5 12c2.1-3.6 5.4-6 9.5-6s7.4 2.4 9.5 6c-2.1 3.6-5.4 6-9.5 6s-7.4-2.4-9.5-6Zm12 0a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z",
    edit: "m4 16-.7 4.7 4.7-.7L20 8l-4-4Zm9.5-9.5 4 4",
    properties: "M4 6h3m4 0h9M4 12h9m4 0h3M4 18h3m4 0h9M7 4h4v4H7Zm6 6h4v4h-4Zm-6 6h4v4H7Z",
    move: "M3 9V6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4m3 0h10m-3-3 3 3-3 3",
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
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon", "registry-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[kind]);
  svg.append(path);
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

function registryTypePicker(selected, onSelect) {
  const kinds = ["", "part", "drawing", "kit", "folder"];
  const label = el("span", "Тип записи", "registry-filter-label");
  label.id = "registry-type-label";
  const value = el("span", "", "registry-type-value");
  value.id = "registry-type-value";
  const icon = el("span", "", "registry-type-icon");
  const chevron = registryIcon("chevronDown");
  chevron.classList.add("registry-type-chevron");
  const trigger = button("", () => {
    if (menu.hidden) openMenu();
    else closeMenu(true);
  });
  trigger.classList.add("registry-type-trigger");
  trigger.setAttribute("aria-labelledby", `${label.id} ${value.id}`);
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  trigger.append(icon, value, chevron);

  const menu = el("div", "", "registry-type-menu");
  menu.id = "registry-type-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-labelledby", label.id);
  trigger.setAttribute("aria-controls", menu.id);
  const options = kinds.map(kind => {
    const option = button("", () => {
      setValue(kind);
      closeMenu(true);
      onSelect(kind);
    });
    option.classList.add("registry-type-option");
    option.tabIndex = -1;
    option.setAttribute("role", "menuitemradio");
    const optionIcon = el("span", registryIcon(kind || "filter"), "registry-type-icon");
    optionIcon.dataset.kind = kind;
    const check = registryIcon("check");
    check.classList.add("registry-type-check");
    option.append(optionIcon, el("span", labels[kind] ?? "Все типы"), check);
    return option;
  });
  menu.append(...options);
  const node = el("div", label, "registry-type");
  node.append(trigger, menu);

  function setValue(kind) {
    trigger.dataset.kind = icon.dataset.kind = kind;
    icon.replaceChildren(registryIcon(kind || "filter"));
    value.textContent = labels[kind] ?? "Все типы";
    options.forEach((option, index) => option.setAttribute("aria-checked", String(kinds[index] === kind)));
  }
  function openMenu(index = kinds.indexOf(trigger.dataset.kind)) {
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    options[index].focus();
  }
  function closeMenu(restoreFocus = false) {
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus({ preventScroll: true });
    menu.hidden = true;
  }
  trigger.onkeydown = event => {
    if (!menu.hidden || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    openMenu(event.key === "ArrowDown" ? 0 : options.length - 1);
  };
  menu.onkeydown = event => {
    if (event.key === "Tab" || event.key === "Escape") {
      if (event.key === "Escape") event.preventDefault();
      closeMenu(true);
      return;
    }
    const index = options.indexOf(document.activeElement);
    const destinations = {
      Home: 0, End: options.length - 1,
      ArrowUp: (index - 1 + options.length) % options.length, ArrowDown: (index + 1) % options.length,
    };
    if (!Object.hasOwn(destinations, event.key)) return;
    event.preventDefault();
    options[destinations[event.key]].focus();
  };
  node.addEventListener("focusout", event => {
    if (!menu.hidden && !node.contains(event.relatedTarget)) closeMenu();
  });
  setValue(selected);
  return { node, setValue };
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
const REGISTRY_FOLDERS_KEY = "plotter.registry.expandedFolders";
const roleNames = { operator: "Оператор", designer: "Конструктор" };

function storedFolders() {
  try {
    const values = JSON.parse(sessionStorage.getItem(REGISTRY_FOLDERS_KEY));
    return new Set(Array.isArray(values) ? values.filter(value => typeof value === "string") : []);
  } catch {
    return new Set();
  }
}

export function mountWorkplaces({ request, navigateLayout, addRegisteredParts, finishRegisteredCut, releaseRegisteredCut, openFile, flushLayout,
  getActiveCut, onExit, layoutWidths }) {
  const page = $("workplace-page");
  const dock = $("workplace-dock");
  const xiaoActions = $("xiao-handoff");
  const picker = $("role-picker");
  const pickerStatus = $("role-picker-status");
  const topbar = $("workplace-topbar");
  const roleLabel = $("workplace-role");
  const exit = $("workplace-exit");
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
  const expandedFolders = storedFolders();
  let tab = null;
  let designerView = "registry";
  let selectedFolder = null;
  let registrySearch = "";
  let registryKind = "";
  let registryTreeHidden = false;
  let cad = null;
  let selectedOrder = null;
  let analysisPeriod = defaultAnalysisPeriod();
  let analysisDeltaDescending = true;
  let analysisSeries = "fabricMetersPerHour";
  let workClock = 0;
  let currentLayout = null;
  let renderEpoch = 0;
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
    return entry ? trailOf(entry).map(item => item.name).join(" / ") : "Корень реестра";
  }

  function rememberFolders() {
    try { sessionStorage.setItem(REGISTRY_FOLDERS_KEY, JSON.stringify([...expandedFolders])); }
    catch { /* Expanding folders still works without browser storage. */ }
  }

  function revealFolder(id) {
    const trail = trailOf(find(id));
    if (trail.every(folder => expandedFolders.has(folder.id))) return;
    for (const folder of trail) expandedFolders.add(folder.id);
    rememberFolders();
  }

  function folderSelect(value, moving) {
    const valid = folders().filter(folder => moving?.kind !== "folder" || !trailOf(folder).some(item => item.id === moving.id));
    return select([["", "Корень реестра"], ...valid.map(folder => [folder.id, pathOf(folder)])], value);
  }

  async function refreshRegistry() {
    const registry = await get("/api/registry");
    entries = registry.entries;
    sketchupDesign = registry.sketchupDesign === true;
    entriesById = new Map(entries.map(entry => [entry.id, entry]));
    if (selectedFolder && !find(selectedFolder)) selectedFolder = null;
  }

  function execute(action) {
    return async () => {
      if (pending) return;
      pending = true;
      status.textContent = "";
      pickerStatus.hidden = true;
      try { await action(); } catch (error) {
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
    const catalogLabel = fabric ? "Детали и комплекты" : "Каталог деталей";
    const compositionLabel = fabric ? "Состав ткани" : "Детали в комплекте";
    const destination = fabric ? "в состав ткани" : "в комплект";
    const chosen = new Map(previous.map(item => [item.id, item.quantity]));
    const section = el("div", "", `workplace-picker${cards ? " registry-kit-picker" : ""}${fabric ? " workplace-fabric-picker" : ""}`);
    const browser = el("div", "", "workplace-picker-browser");
    const search = input("search", "", { placeholder: "Название или путь к папке" });
    const results = el("div", "", "workplace-picker-results");
    const count = el("small", "", cards ? "registry-picked-count" : "");
    const selected = el("div", "", "registry-picked-list");
    const composition = cards ? el("section", el("h3", compositionLabel), "registry-picked") : null;
    const notice = cards ? el("span", "", "visually-hidden registry-kit-notice") : null;
    const catalogNodes = new Map();
    const quantities = new Map();
    let dragging = null;
    const choices = entries.filter(entry => kinds.includes(entry.kind));
    const directory = folderSelect(folder);
    if (mode === "kit") {
      directory.options[0].value = "root";
      directory.add(new Option("Все папки", ""), 0);
      directory.value = folder || "root";
    } else directory.options[0].textContent = "Все папки";
    const inFolder = (entry) => directory.value === "root" ? entry.parent == null
      : !directory.value || trailOf(find(entry.parent)).some(parent => parent.id === directory.value);
    function itemText(entry) {
      const text = el("span", el("strong", entry.name), cards ? "registry-node-text" : "");
      const path = pathOf(find(entry.parent));
      if (fabric) {
        const meta = el("small", el("span", labels[entry.kind], "registry-kind"), "workplace-fabric-meta");
        meta.append(el("span", path));
        text.append(meta);
      } else text.append(el("small", cards ? path : `${labels[entry.kind]} · ${path}`));
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

    function drawChosen() {
      count.textContent = `Выбрано: ${chosen.size}`;
      if (!cards) return;
      quantities.clear();
      selected.replaceChildren();
      for (const [id, quantity] of chosen) {
        const entry = find(id) ?? { id, name: "Недоступная деталь", kind: "part" };
        const row = kitNode(entry, false);
        const amount = amountInput(entry, quantity);
        quantities.set(id, amount);
        const remove = button("Убрать", () => changeKit(entry, false, true));
        remove.classList.add("registry-node-action");
        remove.setAttribute("aria-label", `Убрать ${entry.name} ${fabric ? "из состава ткани" : "из комплекта"}`);
        row.append(amount, kitTools(entry, remove));
        selected.append(row);
      }
      if (!selected.children.length) {
        const empty = fabric ? "Добавьте детали или комплекты из каталога" : "Перетащите сюда детали из каталога";
        selected.append(el("div", el("strong", empty), "registry-kit-empty"));
      }
    }

    function drawMatches() {
      const needle = search.value.trim().toLocaleLowerCase();
      catalogNodes.clear();
      results.replaceChildren();
      for (const entry of choices) {
        if (!inFolder(entry) || !pathOf(entry).toLocaleLowerCase().includes(needle)) continue;
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
    directory.onchange = drawMatches;
    if (cards) {
      browser.setAttribute("role", "region");
      browser.setAttribute("aria-label", catalogLabel);
      results.setAttribute("role", "list");
      browser.append(el("h3", catalogLabel));
    }
    browser.append(field("Фильтр по папке", directory), field("Поиск по реестру", search), results);
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
      setFolder(value) {
        const followsFolder = directory.value === (folder || "root");
        folder = value;
        if (followsFolder) {
          directory.value = folder || "root";
          drawMatches();
        }
      },
    };
  }

  function recordDialog(kind, old, onSaved) {
    let record = old;
    const name = input("text", old?.name ?? "", { required: true, maxLength: 255 });
    const parent = folderSelect(old ? old.parent : selectedFolder, old);
    const model = input("checkbox", "", { checked: old?.model === true });
    const picker = kind === "kit" ? itemPicker(["part"], old?.members, { mode: "kit", folder: parent.value }) : null;
    const fields = el("div", "", "workplace-form-grid");
    fields.append(field("Название", name), field(kind === "folder" ? "Родительская папка" : "Папка записи", parent));
    if (kind === "folder") fields.append(field("Обозначить как изделие", model));
    if (picker) {
      fields.append(picker.section);
      parent.onchange = () => picker.setFolder(parent.value);
    }
    if (old) {
      const actions = el("div", "", "registry-property-actions");
      const remove = button("Удалить", execute(async () => {
        if (modal.querySelector('[type="submit"]').disabled) return;
        const restore = suspendDialog(remove);
        modal.close();
        try {
          if (await confirmRegistryDeletion(record)) {
            await removeRegistryRecord(record);
            return;
          }
        } catch (error) {
          restore();
          throw error;
        }
        restore();
      }), { icon: "trash", iconOnly: true });
      remove.classList.add("registry-delete");
      const move = button("Перенести в другую папку", () => {
        if (modal.querySelector('[type="submit"]').disabled) return;
        const restore = suspendDialog(move);
        moveRegistryEntry(record, {
          returnTo: restore,
          onMoved: (moved) => {
            record = { ...record, parent: moved.parent, revision: moved.revision };
            parent.value = record.parent ?? "";
            picker?.setFolder(parent.value);
          },
        });
      }, { icon: "move", iconOnly: true });
      actions.append(move, remove);
      if (picker) {
        const open = button("Раскладка комплекта", execute(async () => {
          if (modal.querySelector('[type="submit"]').disabled) return;
          if (name.value !== record.name || (parent.value || null) !== record.parent
            || JSON.stringify(picker.values()) !== JSON.stringify(record.members)) {
            throw new Error("Сохраните свойства комплекта перед открытием раскладки.");
          }
          await kitLayoutDialog(record, suspendDialog(open));
        }), { icon: "layout", iconOnly: true });
        actions.prepend(open);
      }
      fields.append(actions);
    }
    formDialog(old ? `Свойства · ${old.name}` : labels[kind], fields, "Сохранить", async () => {
      const result = await post("/api/registry", { action: "save", kind, name: name.value, parent: parent.value || null,
        ...(record ? { id: record.id, revision: record.revision } : {}), ...(kind === "folder" ? { model: model.checked } : {}),
        ...(picker ? { members: picker.values() } : {}) });
      await refreshRegistry();
      onSaved?.(result);
      await render();
    });
  }

  function suspendDialog(opener) {
    const contents = [...modal.childNodes];
    return () => {
      modal.replaceChildren(...contents);
      if (!modal.open) modal.showModal();
      opener.focus({ preventScroll: true });
    };
  }

  function moveRegistryEntry(entry, { returnTo, onMoved } = {}) {
    const destination = folderSelect(entry.parent, entry);
    const path = el("p", `Текущая папка: ${pathOf(find(entry.parent))}`, "workplace-form-note");
    const content = el("div", path, "workplace-form-grid");
    content.append(field("Новая папка", destination));
    formDialog(`Переместить «${entry.name}»`, content, "Переместить", async () => {
      const parent = destination.value || null;
      if (parent === entry.parent) throw new Error("Выберите другую папку.");
      const moved = await post("/api/registry", { action: "move", id: entry.id, revision: entry.revision, parent });
      onMoved?.(moved);
      selectedFolder = parent;
      registrySearch = registryKind = "";
      await render();
      if (!returnTo) focusWorkplaceHeading();
    });
    if (returnTo) modal.addEventListener("close", returnTo, { once: true });
  }

  function confirmRegistryDeletion(entry) {
    return confirmAction({
      title: `Удалить «${entry.name}»?`,
      message: entry.kind === "folder" ? "Удалить можно только пустую папку." : "Запись будет удалена из реестра.",
      confirmLabel: "Удалить",
    });
  }

  async function deleteRegistryEntry(entry) {
    if (await confirmRegistryDeletion(entry)) await removeRegistryRecord(entry);
  }

  async function removeRegistryRecord(entry) {
    await post("/api/registry", { action: "delete", id: entry.id, revision: entry.revision });
    expandedFolders.delete(entry.id);
    rememberFolders();
    await render();
    focusWorkplaceHeading();
  }

  async function kitLayoutDialog(entry, returnTo) {
    const kit = returnTo ? entry : await get("/api/registry", { id: entry.id });
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
    if (returnTo) modal.addEventListener("close", () => { if (modal.returnValue !== "saved") returnTo(); }, { once: true });
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
      registrySearch = registryKind = "";
      await render();
      status.textContent = "Реестр восстановлен.";
    }, { lockWhileSaving: true });
  }

  function renderRegistry() {
    revealFolder(selectedFolder);
    const search = input("search", registrySearch, { id: "registry-query", placeholder: "Название или путь к папке" });
    const searchLabel = el("label", "Поиск по всему реестру", "registry-filter-label");
    searchLabel.htmlFor = search.id;
    const clearSearch = button("Очистить поиск", () => {
      registrySearch = search.value = "";
      search.focus({ preventScroll: true });
      drawMatches();
    }, { icon: "close", iconOnly: true });
    clearSearch.classList.add("registry-query-clear");
    const query = el("div", registryIcon("search"), "registry-query");
    query.append(search, clearSearch);
    const searchField = el("div", searchLabel, "registry-search-field");
    searchField.append(query);
    const kind = registryTypePicker(registryKind, value => {
      registryKind = value;
      drawMatches();
    });
    const table = el("div", "", "registry-list");
    const tree = el("nav", "", "registry-tree");
    tree.id = "registry-folder-tree";
    tree.setAttribute("aria-label", "Папки реестра");
    const toggleTree = button("", () => {
      registryTreeHidden = !registryTreeHidden;
      updateTreeVisibility();
    }, { iconOnly: true });
    toggleTree.classList.add("registry-tree-toggle");
    toggleTree.setAttribute("aria-controls", tree.id);
    const sidebar = el("div", toggleTree, "registry-sidebar");
    sidebar.append(tree);
    const breadcrumbs = el("nav", "", "registry-breadcrumbs");
    breadcrumbs.setAttribute("aria-label", "Путь к папке");
    const filters = el("div", searchField, "registry-search");
    filters.append(kind.node);
    const content = el("section", breadcrumbs, "registry-body");
    content.append(filters, table);
    const columns = el("div", sidebar, "registry-columns");
    columns.append(content);
    const childrenByParent = new Map();
    for (const folder of folders()) {
      if (!childrenByParent.has(folder.parent)) childrenByParent.set(folder.parent, []);
      childrenByParent.get(folder.parent).push(folder);
    }
    const folderButtons = new Map();
    function updateTreeVisibility() {
      tree.hidden = registryTreeHidden;
      columns.classList.toggle("registry-tree-hidden", registryTreeHidden);
      const label = registryTreeHidden ? "Развернуть дерево папок" : "Свернуть дерево папок";
      toggleTree.title = label;
      toggleTree.setAttribute("aria-label", label);
      toggleTree.setAttribute("aria-expanded", String(!registryTreeHidden));
      toggleTree.replaceChildren(registryIcon(registryTreeHidden ? "sidebarExpand" : "sidebarCollapse"));
    }
    function browseFolder(id, focusTree = false) {
      selectedFolder = id;
      revealFolder(id);
      registrySearch = search.value = "";
      registryKind = "";
      kind.setValue(registryKind);
      drawTree();
      const current = drawBreadcrumbs();
      drawMatches();
      const target = focusTree && !registryTreeHidden ? folderButtons.get(id) : current;
      target?.focus({ preventScroll: true });
    }
    function drawBreadcrumbs() {
      const path = [{ id: null, name: "Корень реестра" }, ...trailOf(find(selectedFolder))];
      const current = el("span", path.pop().name);
      current.tabIndex = -1;
      current.setAttribute("aria-current", "location");
      breadcrumbs.replaceChildren();
      for (const folder of path) {
        const link = button(folder.name, () => browseFolder(folder.id));
        link.classList.add("registry-breadcrumb-link");
        const separator = el("span", "/", "registry-breadcrumb-separator");
        separator.setAttribute("aria-hidden", "true");
        breadcrumbs.append(link, separator);
      }
      breadcrumbs.append(current);
      return current;
    }
    function drawTree() {
      tree.replaceChildren();
      folderButtons.clear();
      function folderButton(entry) {
        const id = entry?.id ?? null;
        const row = button(entry?.name ?? "Корень реестра", () => browseFolder(id, true));
        row.prepend(registryIcon("folder"));
        row.setAttribute("aria-pressed", String(id === selectedFolder));
        folderButtons.set(id, row);
        return row;
      }
      function folderList(parent, depth) {
        const list = el("ul", "", "registry-folder-list");
        for (const folder of childrenByParent.get(parent) ?? []) {
          const item = el("li");
          const row = el("div", "", "registry-tree-row");
          row.style.setProperty("--folder-indent", `${Math.min(depth, 6) * 12}px`);
          const hasChildren = childrenByParent.has(folder.id);
          const toggle = hasChildren ? button("", () => {
            if (expandedFolders.has(folder.id)) expandedFolders.delete(folder.id);
            else expandedFolders.add(folder.id);
            updateDisclosure();
            rememberFolders();
          }) : el("span", "", "registry-folder-toggle-spacer");
          const nested = hasChildren ? folderList(folder.id, depth + 1) : null;
          function updateDisclosure() {
            const expanded = expandedFolders.has(folder.id);
            nested.hidden = !expanded;
            toggle.setAttribute("aria-expanded", String(expanded));
            toggle.setAttribute("aria-label", `${expanded ? "Свернуть" : "Развернуть"} ${folder.name}`);
          }
          if (nested) {
            nested.id = `registry-branch-${folder.id}`;
            toggle.classList.add("registry-folder-toggle");
            toggle.setAttribute("aria-controls", nested.id);
            toggle.append(registryIcon("chevron"));
            updateDisclosure();
          }
          row.append(toggle, folderButton(folder));
          item.append(row);
          if (nested) item.append(nested);
          list.append(item);
        }
        return list;
      }
      tree.append(folderButton(null), folderList(null, 0));
    }
    function drawMatches() {
      const needle = search.value.trim().toLocaleLowerCase();
      const searchKind = registryKind;
      clearSearch.hidden = !search.value;
      const searching = Boolean(needle || searchKind);
      const matches = entries.filter(item => {
        if (!searching) return item.parent === selectedFolder;
        return (!searchKind || item.kind === searchKind) && (!needle || pathOf(item).toLocaleLowerCase().includes(needle));
      });
      table.replaceChildren();
      for (const entry of matches) {
        const row = el("div", "", "registry-row");
        row.dataset.registryId = entry.id;
        const title = el("div", el("strong", entry.name));
        const metadata = el("div", el("span", entry.model ? "Изделие" : labels[entry.kind], "registry-kind"), "registry-row-meta");
        if (searching) metadata.append(el("small", pathOf(find(entry.parent))));
        title.append(metadata);
        const identity = el("div", registryIcon(entry.kind), "registry-entry");
        identity.dataset.kind = entry.kind;
        identity.append(title);
        const tools = el("div", "", "registry-row-actions");
        if (entry.kind === "folder") tools.append(button("Открыть", () => browseFolder(entry.id, true), { icon: "open", iconOnly: true }));
        if (entry.kind === "part") tools.append(partViewButton(entry));
        if (["part", "drawing"].includes(entry.kind)) {
          tools.append(button("Редактировать", execute(async () => openCad(await get("/api/registry", { id: entry.id }))), { icon: "edit", iconOnly: true }));
        }
        if (entry.kind === "kit") {
          tools.append(button("Раскладка комплекта", execute(() => kitLayoutDialog(entry)), { icon: "layout", iconOnly: true, primary: true }));
          tools.append(button("Карта пошива", execute(() => sewingMap(entry)), { icon: "sewing", iconOnly: true }));
        }
        tools.append(button("Свойства", execute(async () => recordDialog(entry.kind, await get("/api/registry", { id: entry.id }))), { icon: "properties", iconOnly: true }));
        const remove = button("Удалить", execute(() => deleteRegistryEntry(entry)), { icon: "trash", iconOnly: true });
        remove.classList.add("registry-delete");
        tools.append(button("Переместить", () => moveRegistryEntry(entry), { icon: "move", iconOnly: true }), remove);
        row.append(identity, tools);
        table.append(row);
      }
      if (!matches.length) table.append(el("p", searching ? "Совпадений нет" : "В этой папке пока нет записей", "workplace-empty"));
    }
    search.oninput = () => { registrySearch = search.value; drawMatches(); };
    panel.append(workHeading("Реестр лекал", "", [
      button("Скачать бэкап", execute(() => downloadAttachment("/api/registry-backup", "Реестр Plotter.rar", "Не удалось скачать бэкап реестра."))),
      button("Восстановить бэкап", restoreRegistryBackup),
      button("Папка", () => recordDialog("folder", null)), button("Комплект", () => recordDialog("kit", null)),
      button("Новый чертёж", () => openCad(null), { primary: true }),
    ]), columns);
    updateTreeVisibility();
    drawTree();
    drawBreadcrumbs();
    drawMatches();
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

  function openCad(document) {
    let record = document;
    const host = el("div", "", "workplace-cad");
    cad?.destroy();
    page.classList.add("is-cad");
    panel.replaceChildren(host);
    const back = execute(async () => {
      if (!await confirmLeaveCad()) return;
      cad?.destroy(); cad = null;
      await render();
      focusWorkplaceHeading();
    });
    const savePart = ({ entities, pattern, partLegend }) => new Promise((resolve, reject) => {
      const parent = folderSelect(record?.parent ?? selectedFolder);
      const previous = () => pattern && entries.find(item => item.kind === "part" &&
        item.patternId === pattern.id && item.parent === (parent.value || null));
      const title = input("text", previous()?.name || (pattern ? pattern.number + " · " + pattern.name : ""),
        { required: true, maxLength: 255 });
      const content = el("div", field("Название детали", title), "workplace-form-grid");
      content.append(field("Папка", parent));
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
      const showUpdate = () => { update.textContent = previous() ? "Обновится сохранённая деталь в этой папке." : ""; };
      if (pattern) {
        content.append(update);
        if (pattern.checkSewing) content.append(el("p", "Линия стачивания менялась в 2D. Сверьте парные кромки и насечки."));
        parent.onchange = showUpdate;
        showUpdate();
      }
      modal.addEventListener("close", () => { if (modal.returnValue !== "saved") reject(new Error("Сохранение детали отменено.")); }, { once: true });
      formDialog(record?.kind === "part" ? "Сохранить как новую деталь" : "Сохранить деталь", content, "Сохранить", async () => {
        const old = previous();
        const part = await post("/api/registry", { action: "save", kind: "part", parent: parent.value || null,
          name: title.value, entities, ...(pattern ? { pattern } : {}),
          ...(legendEnabled.checked ? { partLegend: { developer: developer.value, comments: comments.value } } : {}),
          ...(old ? { id: old.id, revision: old.revision } : {}) });
        await refreshRegistry();
        resolve(part);
      });
    });
    cad = mountCadEditor(host, { document, request, sketchupDesign: sketchupDesign && document?.kind !== "part",
      onBack: back, onSavePart: savePart, onSaveDocument: async (value) => {
      record = await post("/api/registry", { action: "save", kind: record?.kind ?? "drawing", parent: record ? record.parent : selectedFolder,
        ...(record ? { id: record.id, revision: record.revision } : {}), ...value });
      await refreshRegistry();
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
      if (!await confirmLeaveCad()) return;
      cad?.destroy();
      cad = null;
      page.hidden = true;
      page.classList.remove("is-cad");
      panel.replaceChildren();
      onBack();
    });
    cad?.destroy();
    page.classList.add("is-cad");
    panel.replaceChildren(host);
    page.hidden = false;
    cad = mountCadEditor(host, { document, request, layoutPart: true, onBack: back, onSaveDocument });
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
    dock.append(button("Скачать карту пошива", execute(async () => {
      if (mode !== "kit" && flushLayout && !await flushLayout()) {
        throw new Error("Сохраните раскладку перед скачиванием карты пошива.");
      }
      const params = mode === "kit" ? { kitId: next.kitId, widthMm: result.widthMm, quantity: result.quantity }
        : next.id ? { cutId: next.id, mode: next.previewSource ?? mode } : { workspace: 1 };
      await downloadSewingPdf(params);
    })));
    dock.append(xiaoActions);
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
    selectedOrder = id;
    await render();
    page.scrollTop = 0;
    focusWorkplaceHeading();
  }

  function renderOrder(order) {
    const operator = tab === "operator";
    const draft = !order.published;
    const done = order.cuts.filter(cut => cut.status === "done").length;
    const actions = [button(designerView === "analysis" && !operator ? "К анализу" : "Все наряды",
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
    panel.append(workHeading(`Наряд № ${order.number}`, orderContext(order), actions));
    const overview = el("section", orderDates(order), "workplace-order-overview");
    overview.append(progressRow(done, order.cuts.length));
    if (draft) overview.append(el("span", "Черновик", "workplace-chip"));
    if (order.notes) overview.append(el("p", order.notes, "workplace-order-notes"));
    panel.append(overview);
    const finished = done > 0 && done === order.cuts.length;
    const totals = workStats(order.stats, { order: true, finished, draft });
    totals.classList.add("workplace-order-totals");
    panel.append(totals);
    panel.append(el("h2", `Ткани · ${order.cuts.length}`, "workplace-section-title"));
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
    panel.append(list);
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

  function renderAnalysis({ summary, days, orders }) {
    const back = button("Все наряды", execute(async () => {
      designerView = "orders";
      await openOrder(null);
    }), { icon: "back" });
    panel.append(workHeading("Анализ раскроя", "", [back]));
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
        await render();
        focusWorkplaceHeading();
      })();
    };
    panel.append(filter);
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
    panel.append(summaryGrid, analysisChart(days), analysisOrders(orders));
  }

  async function render() {
    const epoch = ++renderEpoch;
    clearInterval(workClock);
    page.classList.remove("is-cad");
    const showReport = tab === "designer" && designerView === "analysis" && !selectedOrder;
    let report;
    if (showReport) {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      report = await get("/api/order-analysis", { ...analysisPeriod, tz });
    } else if (tab === "designer") await refreshRegistry();
    if (epoch !== renderEpoch) return;
    const operator = tab === "operator";
    panel.replaceChildren();
    if (!operator) {
      const nav = el("nav", "", "workplace-subnav");
      for (const [view, title] of [["registry", "Реестр"], ["orders", "Наряды"]]) {
        const control = button(title, execute(async () => { designerView = view; await openOrder(null); }));
        control.setAttribute("aria-pressed", String(designerView === view || (view === "orders" && designerView === "analysis")));
        nav.append(control);
      }
      panel.append(nav);
      if (designerView === "registry") { renderRegistry(); return; }
      if (showReport) { renderAnalysis(report); return; }
    }
    const params = operator ? { published: 1 } : {};
    const result = await get("/api/orders", selectedOrder ? { ...params, id: selectedOrder } : params);
    if (epoch !== renderEpoch) return;
    if (selectedOrder) { renderOrder(result); updateWorkClocks(); return; }
    const actions = operator ? [button("Новый раскрой", execute(() => showLayout({ mode: "standalone" })), { primary: true })]
      : [button("Анализ", execute(async () => { designerView = "analysis"; await openOrder(null); }), { icon: "analysis" }),
        button("Новый наряд", () => orderDialog(null), { primary: true, icon: "plus" })];
    if (operator) actions.unshift(button("Продолжить раскрой", execute(async () => {
      const id = await getActiveCut();
      const order = id && result.orders.find(order => order.materials?.some(material => material.id === id));
      const material = order?.materials.find(material => material.id === id);
      await showLayout(id ? { mode: "operator", id, order, name: material?.name } : { mode: "standalone" });
    })));
    panel.append(workHeading(operator ? "Наряды на раскрой" : "Наряды", "", actions));
    const cards = el("div", "", "workplace-cards workplace-order-list");
    for (const order of result.orders) {
      const card = el("article", "", "workplace-card workplace-order-card");
      enableCardOpen(card, `Открыть наряд № ${order.number}`, execute(() => openOrder(order.id)));
      const identity = el("div", el("h2", `Наряд № ${order.number}`), "workplace-card-identity");
      const finished = order.total > 0 && order.done === order.total;
      const state = !order.published ? "draft" : finished ? "done" : order.active || order.done ? "active" : "pending";
      identity.prepend(el("span", state === "draft" ? "Черновик" : states[state], `workplace-chip ${state}`));
      const detail = orderContext(order);
      if (detail) identity.append(el("p", detail));
      const summary = el("div", "", "workplace-card-materials");
      summary.append(el("span", "Ткани", "workplace-order-label"));
      const fabrics = order.materials?.map(material => material.widthMm == null ? material.name : `${material.name} · ${material.widthMm} мм`);
      summary.append(el("span", fabrics?.join(", ") || "Ткани не добавлены"));
      const progress = progressRow(order.done, order.total);
      const header = el("header", identity, "workplace-card-heading");
      header.append(orderDates(order));
      card.append(header, summary, progress, workStats(order.stats, {
        order: true, finished, draft: !order.published,
      }));
      if (order.notes) card.append(el("p", order.notes, "workplace-card-notes"));
      cards.append(card);
    }
    if (!cards.children.length) cards.append(el("p", operator ? "Отправленные наряды появятся здесь." : "Создайте наряд и добавьте ткани.", "workplace-empty"));
    panel.append(cards);
    updateWorkClocks();
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

  function focusPicker() {
    $("role-picker-title").focus({ preventScroll: true });
  }

  function focusWorkplaceHeading() {
    const heading = panel.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }

  function confirmLeaveCad() {
    return !cad?.hasUnsavedChanges() || confirmAction({
      title: "Закрыть чертёж?",
      message: "Несохранённые изменения будут потеряны.",
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
    topbar.hidden = page.hidden = false;
    focusWorkplaceHeading();
  }

  const leaveRole = execute(async () => {
    if (cad?.isBusy()) return;
    if (!await confirmLeaveCad()) return;
    if (!await leaveLayout()) return;
    await onExit?.();
    cad?.destroy();
    cad = null;
    tab = selectedOrder = selectedFolder = null;
    registrySearch = registryKind = "";
    designerView = "registry";
    analysisPeriod = defaultAnalysisPeriod();
    clearInterval(workClock);
    saveRole(null);
    page.classList.remove("is-cad");
    panel.replaceChildren();
    topbar.hidden = page.hidden = true;
    picker.hidden = false;
    focusPicker();
  });
  exit.onclick = leaveRole;
  for (const name of Object.keys(roleNames)) {
    $(`${name}-tab`).onclick = execute(() => enterRole(name));
  }
  window.addEventListener("beforeunload", event => {
    if (!cad?.hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  const initialRole = storedRole();
  if (initialRole) void execute(() => enterRole(initialRole))();
  else focusPicker();
  return { openLayoutPart, hasPendingCadEdits: () => Boolean(cad?.hasUnsavedChanges() || cad?.isBusy()) };
}
