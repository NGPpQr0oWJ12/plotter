import { confirmAction } from "/confirm-action.js";

const fieldNames = {
  number: "Номер детали", name: "Название детали", patternNumber: "Номер лекала", fabric: "Ткань",
  side: "Сторона", seam: "Швы и соединяемые детали", developer: "Разработчик", date: "Дата", comment: "Комментарий",
  nap: "Направление ворса", contour: "Миниатюра контура", image: "Изображение PNG", text: "Свободный текст",
  order: "Номер заказа", ticket: "Номер наряда", product: "Изделие", dimensions: "Габариты детали",
  material: "Материал", count: "Количество деталей",
};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function action(text, handler, primary = false) {
  const result = node("button", primary ? "button primary" : "button", text);
  result.type = "button";
  result.onclick = handler;
  return result;
}

function field(title, control) {
  const label = node("label", "label-input");
  control.setAttribute("aria-label", title);
  label.append(node("span", "", title), control);
  return label;
}

function options(select, entries, previous = "") {
  select.replaceChildren(...entries.map(([value, title]) => new Option(title, String(value))));
  if (entries.some(([value]) => String(value) === String(previous))) select.value = String(previous);
}

function numbered(item) {
  const number = String(item.number ?? "").replace(/^[№#]\s*/, "");
  return [number && "№" + number, item.name].filter(Boolean).join(" · ");
}

function makeId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
}

function numberInput(value, change, minimum = 0, maximum = 200) {
  const input = node("input");
  input.type = "number";
  input.step = "0.1";
  input.min = minimum;
  input.max = maximum;
  input.value = value;
  input.onchange = () => {
    if (input.value && input.checkValidity()) change(input.valueAsNumber);
    else input.reportValidity();
  };
  return input;
}

export function createLabelDesigner({ get, post }) {
  const element = node("section", "labels-editor");
  const heading = node("header", "labels-heading");
  const buttons = node("div", "labels-row");
  const message = node("p", "label-message");
  message.setAttribute("role", "status");
  const reload = action("Загрузить заново", () => {
    if (!dirty.size || window.confirm("Загрузить сохранённые шаблоны и отбросить несохранённые изменения?")) void open(true);
  });
  const save = action("Сохранить шаблон", saveTemplate, true);
  buttons.append(reload, save);
  heading.append(node("h1", "", "Конструктор бирок"), buttons);
  const kindSelect = node("select");
  options(kindSelect, [["part", "Бирка детали"], ["master", "Мастер-бирка"]]);
  const order = node("select"), material = node("select"), instance = node("select");
  const instanceField = field("Экземпляр детали", instance);
  const context = node("div", "label-context");
  context.append(field("Шаблон", kindSelect), field("Наряд", order), field("Материал", material), instanceField);
  const grid = node("div", "label-designer-grid");
  const composition = node("section", "label-panel"), work = node("section", "label-paper-area"), properties = node("section", "label-panel");
  const size = node("div", "label-two-col");
  const list = node("div", "label-element-list");
  const addField = node("select");
  const add = action("Добавить поле", addElement);
  composition.append(node("h2", "", "Формат, мм"), size, node("h2", "", "Элементы"), list, field("Новое поле", addField), add);
  const paper = node("div", "label-paper"), image = node("img", "label-render"), handles = node("div", "label-handles");
  image.alt = "Предпросмотр бирки";
  image.hidden = true;
  image.draggable = false;
  paper.append(image, handles);
  const caption = node("p", "label-caption");
  const warnings = node("div", "label-warnings");
  warnings.setAttribute("role", "status");
  work.append(paper, caption, warnings);
  grid.append(composition, work, properties);
  element.append(heading, message, context, grid);
  const dirty = new Set();
  let templates, revision, kind = "part", selected = null, orders = [], timer = 0, previewId = 0, loadId = 0;
  let fieldsByKind = {};
  let saving = false, opened = false;
  const template = () => templates[kind];
  const availableFields = () => fieldsByKind[kind] ?? [];
  const selectedItem = () => template().elements.find(item => item.id === selected);
  const status = (text, error = false) => { message.textContent = text; message.classList.toggle("error", error); };
  function mark() {
    dirty.add(kind);
    save.disabled = saving;
    status("Есть несохранённые изменения.");
    schedulePreview();
  }
  function schedulePreview(delay = 220) {
    clearTimeout(timer);
    const id = ++previewId;
    timer = setTimeout(() => void preview(id), delay);
  }
  async function preview(id) {
    if (!templates || id !== previewId) return;
    const cutId = material.value;
    if (!cutId) {
      image.hidden = true;
      warnings.textContent = "Для предпросмотра выберите наряд с материалом.";
      return;
    }
    try {
      const data = await post("/api/label-preview", { cutId, kind, template: template(),
        ...(kind === "part" && instance.value ? { partKey: instance.value } : {}) });
      if (id !== previewId) return;
      options(instance, (data.parts ?? []).map(p => [p.key, numbered(p)]), instance.value);
      instance.disabled = !instance.options.length;
      image.src = data.image;
      image.hidden = false;
      warnings.replaceChildren(...(data.warnings ?? []).map(w => node("p", "", typeof w === "string" ? w : w.message)));
    } catch (error) {
      if (id !== previewId) return;
      image.hidden = true;
      warnings.textContent = error.message;
    }
  }
  function updateMaterials() {
    const current = orders.find(item => String(item.id) === order.value);
    options(material, (current?.materials || []).map(m => [m.id, m.name]));
    material.disabled = !material.options.length;
    instance.replaceChildren();
    image.hidden = true;
    schedulePreview(0);
  }
  function updatePaper() {
    const t = template();
    paper.style.aspectRatio = t.widthMm + " / " + t.heightMm;
    paper.style.setProperty("--paper-ratio", String(t.widthMm / t.heightMm));
    caption.textContent = t.widthMm + " × " + t.heightMm + " мм";
    handles.replaceChildren();
    for (const item of t.elements) {
      const box = action(fieldNames[item.field] || item.field, () => selectItem(item.id));
      box.className = "label-box";
      box.classList.toggle("selected", item.id === selected);
      box.setAttribute("aria-label", fieldNames[item.field] || item.field);
      box.title = fieldNames[item.field] || item.field;
      box.style.left = item.x / t.widthMm * 100 + "%";
      box.style.top = item.y / t.heightMm * 100 + "%";
      box.style.width = item.w / t.widthMm * 100 + "%";
      box.style.height = item.h / t.heightMm * 100 + "%";
      if (item.id === selected) box.append(node("span", "label-resize"));
      box.onpointerdown = e => beginDrag(e, box, item);
      handles.append(box);
    }
  }
  function beginDrag(e, box, item) {
    if (e.button !== 0 || saving) return;
    e.preventDefault();
    const resize = e.target.classList.contains("label-resize");
    selected = item.id;
    for (const sibling of handles.children) sibling.classList.toggle("selected", sibling === box);
    showProperties();
    updateList();
    const t = template(), rect = paper.getBoundingClientRect();
    const original = { x: item.x, y: item.y, w: item.w, h: item.h }, startX = e.clientX, startY = e.clientY;
    const clamp = (v, min, max) => Math.round(Math.min(max, Math.max(min, v)) * 10) / 10;
    let moved = false;
    box.setPointerCapture(e.pointerId);
    box.onpointermove = event => {
      const dx = (event.clientX - startX) * t.widthMm / rect.width, dy = (event.clientY - startY) * t.heightMm / rect.height;
      if (!dx && !dy && !moved) return;
      moved = true;
      if (resize) {
        item.w = clamp(original.w + dx, 1, t.widthMm - item.x);
        item.h = clamp(original.h + dy, 1, t.heightMm - item.y);
      } else {
        item.x = clamp(original.x + dx, 0, t.widthMm - item.w);
        item.y = clamp(original.y + dy, 0, t.heightMm - item.h);
      }
      Object.assign(box.style, { left: item.x / t.widthMm * 100 + "%", top: item.y / t.heightMm * 100 + "%",
        width: item.w / t.widthMm * 100 + "%", height: item.h / t.heightMm * 100 + "%" });
    };
    const finish = () => {
      box.onpointermove = box.onpointerup = box.onpointercancel = box.onlostpointercapture = null;
      if (moved) mark();
      updatePaper();
      showProperties();
    };
    box.onpointerup = box.onpointercancel = box.onlostpointercapture = finish;
  }
  function updateList() {
    list.replaceChildren(...template().elements.map(item => {
      const b = action(item.field === "text" && item.text ? item.text : fieldNames[item.field] || item.field, () => selectItem(item.id));
      b.setAttribute("aria-pressed", String(item.id === selected));
      return b;
    }));
  }
  function selectItem(id) { selected = id; updateList(); updatePaper(); showProperties(); }
  function showProperties() {
    const item = selectedItem();
    properties.replaceChildren(node("h2", "", "Свойства элемента"));
    if (!item) { properties.append(node("p", "label-caption", "Выберите поле на макете.")); return; }
    const coords = node("div", "label-two-col");
    for (const [key, name] of [["x", "X, мм"], ["y", "Y, мм"], ["w", "Ширина, мм"], ["h", "Высота, мм"]]) {
      coords.append(field(name, numberInput(item[key], v => { item[key] = v; updatePaper(); mark(); }, ["w", "h"].includes(key) ? .1 : 0)));
    }
    properties.append(node("strong", "", fieldNames[item.field]), coords);
    if (!["image", "contour", "nap"].includes(item.field)) {
      properties.append(field("Шрифт, мм", numberInput(item.fontMm ?? 3, v => { item.fontMm = v; mark(); }, .5, 50)));
    }
    if (item.field === "text") {
      const text = node("textarea");
      text.value = item.text || "";
      text.maxLength = 500;
      text.oninput = () => { item.text = text.value; updateList(); mark(); };
      properties.append(field("Текст", text));
    }
    if (item.field === "image") {
      const upload = node("input");
      upload.type = "file"; upload.accept = "image/png,.png";
      upload.onchange = async () => {
        const file = upload.files[0], currentKind = kind;
        if (!file) return;
        try {
          if (file.size > 1000000) throw new Error("Выберите PNG размером до 1 МБ.");
          const bytes = new Uint8Array(await file.slice(0, 8).arrayBuffer());
          if ([137, 80, 78, 71, 13, 10, 26, 10].some((v, i) => v !== bytes[i])) throw new Error("Выберите файл PNG.");
          const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(new Error("Не удалось прочитать PNG."));
            reader.readAsDataURL(new Blob([file], { type: "image/png" }));
          });
          if (currentKind !== kind || !template().elements.includes(item)) return;
          item.image = data;
          mark();
        } catch (error) { status(error.message, true); }
      };
      properties.append(field("Файл PNG", upload));
    }
    properties.append(action("Удалить элемент", () => {
      template().elements = template().elements.filter(e => e !== item);
      selectItem(template().elements[0]?.id);
      mark();
    }));
  }
  function addElement() {
    const t = template(), f = addField.value;
    if (!availableFields().includes(f)) return;
    if (t.elements.length >= 40) { status("На бирке можно разместить до 40 элементов.", true); return; }
    const item = { id: makeId(), field: f, x: 0, y: 0,
      w: Math.min(t.widthMm, f === "image" || f === "contour" ? 20 : 35), h: Math.min(t.heightMm, f === "image" || f === "contour" ? 15 : 7) };
    if (f === "text") item.text = "Текст";
    t.elements.push(item);
    selectItem(item.id);
    mark();
  }
  function paint() {
    const t = template();
    const fields = availableFields();
    options(addField, fields.length ? fields.map(key => [key, fieldNames[key] ?? key]) : [["", "Нет доступных полей"]], addField.value);
    addField.disabled = add.disabled = !fields.length;
    size.replaceChildren(...[["widthMm", "Ширина"], ["heightMm", "Высота"]].map(([key, title]) =>
      field(title, numberInput(t[key], v => { t[key] = v; updatePaper(); mark(); }, 5))));
    instanceField.hidden = kind !== "part";
    if (!t.elements.some(e => e.id === selected)) selected = t.elements[0]?.id;
    save.disabled = saving || !dirty.has(kind);
    updateList(); updatePaper(); showProperties(); schedulePreview(0);
  }
  async function saveTemplate() {
    if (saving || !dirty.has(kind)) return;
    const t = template();
    const outside = t.elements.find(e => e.x + e.w > t.widthMm + .001 || e.y + e.h > t.heightMm + .001);
    if (outside) { selectItem(outside.id); status("Элемент выходит за границу бирки. Проверьте размеры и положение.", true); return; }
    saving = true; element.inert = true;
    try {
      const result = await post("/api/label-templates", { kind, template: t, revision });
      revision = result.revision;
      fieldsByKind = result.fieldsByKind ?? {};
      dirty.delete(kind);
      for (const [key, value] of Object.entries(result.templates)) if (!dirty.has(key)) templates[key] = value;
      status("Шаблон сохранён.");
    } catch (error) { status(error.message, true); }
    finally { saving = false; element.inert = false; paint(); }
  }
  async function open(force = false) {
    if (opened && !force) { if (templates) schedulePreview(0); return; }
    opened = true;
    const id = ++loadId;
    element.inert = true;
    try {
      const [settings, catalog] = await Promise.all([get("/api/label-templates"), get("/api/orders")]);
      if (id !== loadId) return;
      templates = settings.templates; revision = settings.revision; dirty.clear();
      fieldsByKind = settings.fieldsByKind ?? {};
      orders = catalog.orders ?? [];
      const first = orders.find(o => o.materials?.length);
      options(order, orders.map(o => [o.id, ["№" + o.number, o.designation || o.modelName].filter(Boolean).join(" · ")]), order.value || first?.id);
      updateMaterials(); paint(); status("");
    } catch (error) { if (id === loadId) { opened = false; save.disabled = true; status(error.message, true); } }
    finally {
      if (id === loadId) {
        element.inert = false;
        grid.inert = context.inert = !templates;
      }
    }
  }
  kindSelect.onchange = () => { if (templates) { kind = kindSelect.value; image.hidden = true; paint(); status(dirty.has(kind) ? "Есть несохранённые изменения." : ""); } };
  order.onchange = updateMaterials;
  material.onchange = () => { instance.replaceChildren(); image.hidden = true; schedulePreview(0); };
  instance.onchange = () => { image.hidden = true; schedulePreview(0); };
  return { element, open, dirty: () => dirty.size > 0 };
}

function downloadLabelPDF(blob, name) {
  if (!blob.size || blob.type.split(";")[0] !== "application/pdf") throw new Error("Сервер не вернул PDF бирок.");
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = name.replace(/[<>:"/\\|?*]/g, "_") + ".pdf";
  document.body.append(link);
  try { link.click(); }
  finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
}

export function mountLabelStation(request) {
  const actions = document.getElementById("label-actions"), auto = document.getElementById("label-auto");
  const print = document.getElementById("label-open");
  const printerButton = document.getElementById("label-printer");
  const dialog = document.getElementById("label-printer-dialog");
  const dialogNotice = document.getElementById("label-print-status");
  const notice = node("p", "label-message");
  notice.setAttribute("role", "status");
  actions.append(notice);
  print.title = "Скачать PDF всех деталей материала и мастер-бирки";
  let context = null, timer = 0, noticeTimer = 0, noticeSource = "";
  let printerAction = false, view = new AbortController();
  const pendingPrints = new Map();

  function report(error, source = "") {
    clearTimeout(noticeTimer);
    notice.classList.remove("success");
    const text = error ? error.message || "Не удалось выполнить действие с бирками." : "";
    const dockText = source === "poll" || dialog.open ? "" : text;
    if (dockText !== notice.textContent) notice.textContent = dockText;
    if (text !== dialogNotice.textContent) dialogNotice.textContent = text;
    noticeSource = source;
  }

  function success(message) {
    report(null);
    notice.textContent = message;
    notice.classList.add("success");
    noticeTimer = setTimeout(() => report(null), 6000);
  }

  function toolbar() {
    const enabled = context?.enabled ?? true;
    auto.setAttribute("aria-pressed", String(enabled));
    auto.disabled = !context || context.offline || context.enabled === null || context.saving;
    auto.title = context?.offline ? "Автопечать доступна при подключении помощника"
      : enabled ? "Выключить автоматическую печать на выбранном принтере" : "Включить автоматическую печать на выбранном принтере";
    print.disabled = !context || context.exporting;
  }

  function setContext(cut, demo, offline, controlToken) {
    const active = cut && ["operator", "demo"].includes(cut.mode) ? cut : null;
    if (context && active && context.cut.id === active.id && context.demo === demo && context.offline === offline) {
      context.cut = active;
      if (controlToken && controlToken !== context.controlToken) schedule(context, 0);
      context.controlToken = controlToken;
      return;
    }
    if (!active && !context) return;
    clearTimeout(timer);
    view.abort();
    view = new AbortController();
    context = active ? { cut: active, demo, offline, controlToken, enabled: null, saving: false, polling: false, settingVersion: 0,
      exporting: false, registeredKey: null } : null;
    auto.hidden = print.hidden = !context;
    report(null);
    toolbar();
    if (context && !offline) void refresh(context);
  }

  async function exportPDF() {
    const ctx = context;
    if (!ctx || ctx.exporting) return;
    ctx.exporting = true;
    toolbar();
    report(null);
    try {
      const cut = ctx.cut;
      const payload = { cutId: cut.id, cutRevision: cut.cutRevision };
      const response = await request("/api/labels-pdf", payload);
      const blob = await response.blob();
      if (ctx !== context) return;
      downloadLabelPDF(blob, ["Бирки", cut.order?.number, cut.name].filter(Boolean).join(" · "));
    } catch (error) {
      if (ctx === context) report(error);
    } finally {
      ctx.exporting = false;
      if (ctx === context) toolbar();
    }
  }

  async function printers() {
    return (await request("/api/label-printers")).json();
  }

  async function selectPrinter(signal) {
    const { chooseLabelPrinter } = await import("/label-printer-settings.js");
    const printer = await chooseLabelPrinter({ getPrinters: printers, signal });
    if (!printer || signal.aborted) return null;
    const result = await (await request("/api/label-printers", { printer })).json();
    return result.preferred;
  }

  // Retain the request ID if the response is lost after Windows has received the label.
  function rememberPrint(key, attempt) {
    if (attempt) pendingPrints.set(key, attempt); else pendingPrints.delete(key);
    try {
      if (attempt) sessionStorage.setItem(key, JSON.stringify(attempt)); else sessionStorage.removeItem(key);
    } catch { /* The current tab can still retry from memory. */ }
  }

  async function printPart(partKey, display) {
    const ctx = context, signal = view.signal;
    if (!ctx || partKey == null || printerAction) return;
    printerAction = true;
    report(null);
    try {
      const available = await printers();
      if (signal.aborted) return;
      const printer = available.printers.includes(available.preferred) ? available.preferred : await selectPrinter(signal);
      if (!printer || signal.aborted) return;
      const storageKey = "plotter.partPrint." + JSON.stringify([ctx.cut.id, ctx.demo, partKey]);
      let previous = pendingPrints.get(storageKey);
      try { previous ??= JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* Storage is optional. */ }
      const newCopy = previous && (previous.unknown || previous.printer !== printer || previous.revision !== ctx.cut.cutRevision);
      const warning = newCopy ? " Предыдущее задание могло попасть в очередь Windows. Подтвердите печать ещё одной копии." : "";
      if (!await confirmAction({ title: "Отправить бирку на печать?", message: display + " · " + printer + "." + warning,
        confirmLabel: "Отправить на печать", signal }) || signal.aborted) return;
      const attempt = previous && !newCopy ? previous : { requestId: makeId(), printer, revision: ctx.cut.cutRevision };
      rememberPrint(storageKey, attempt);
      const result = await (await request("/api/label-print-part", { cutId: ctx.cut.id, cutRevision: ctx.cut.cutRevision,
        partKey: String(partKey), printer, requestId: attempt.requestId, confirmed: true })).json();
      if (result.status === "submitted" || result.status === "failed") rememberPrint(storageKey, null);
      else if (result.status === "unknown") rememberPrint(storageKey, { ...attempt, unknown: true });
      if (signal.aborted) return;
      if (result.status === "submitted") success("Бирка " + display + " передана в очередь «" + printer + "».");
      else report(new Error(result.message || "Проверьте состояние задания в очереди Windows."));
    } catch (error) {
      if (!signal.aborted) report(error);
    } finally { printerAction = false; }
  }


  function schedule(ctx, delay = 3000) {
    if (ctx !== context) return;
    clearTimeout(timer);
    if (!document.hidden && !ctx.offline) timer = setTimeout(() => void refresh(ctx), delay);
  }

  async function registerRelay(ctx, state) {
    if (!state.enabled || !state.autoRelay || !state.relayKey || ctx.registeredKey === state.relayKey) return;
    await request("/api/labels-auto-relay", { cutId: ctx.cut.id, relayKey: state.relayKey });
    ctx.registeredKey = state.relayKey;
  }

  async function registerUploadedRun(state) {
    const ctx = context;
    if (!ctx?.demo) return;
    try { await registerRelay(ctx, state); }
    catch (error) { if (ctx === context) report(error, "poll"); }
  }

  async function refresh(ctx) {
    if (ctx !== context || document.hidden || ctx.polling || ctx.offline) return;
    ctx.polling = true;
    const version = ctx.settingVersion;
    try {
      const statusRequest = { cutId: ctx.cut.id };
      if (ctx.demo && ctx.controlToken) statusRequest.controlToken = ctx.controlToken;
      const state = await (await request("/api/labels-status", statusRequest)).json();
      if (ctx !== context) return;
      if (!ctx.saving && version === ctx.settingVersion) ctx.enabled = state.enabled;
      toolbar();
      if (ctx.controlToken && version === ctx.settingVersion) await registerRelay(ctx, state);
      if (ctx !== context) return;
      if (state.enabled && state.autoMessage) report(new Error(state.autoMessage), "poll");
      else if (noticeSource === "poll") report(null);
    } catch (error) {
      if (ctx === context) report(error, "poll");
    } finally {
      ctx.polling = false;
      schedule(ctx);
    }
  }

  auto.onclick = async () => {
    const ctx = context;
    if (!ctx || auto.disabled) return;
    ctx.saving = true;
    ++ctx.settingVersion;
    toolbar();
    report(null);
    try {
      const enabled = !ctx.enabled;
      const state = await (await request("/api/labels-settings", { cutId: ctx.cut.id, enabled, relay: enabled && ctx.demo })).json();
      ctx.enabled = state.enabled;
      if (ctx === context) await registerRelay(ctx, state);
    } catch (error) {
      if (ctx === context) report(error);
    } finally {
      ctx.saving = false;
      ++ctx.settingVersion;
      if (ctx === context) toolbar();
    }
  };
  printerButton.onclick = async () => {
    if (printerAction) return;
    printerAction = true;
    const signal = view.signal;
    try {
      const printer = await selectPrinter(signal);
      if (printer && !signal.aborted) success("Выбран принтер «" + printer + "».");
    } catch (error) {
      if (!signal.aborted) report(error);
    } finally { printerAction = false; }
  };
  print.onclick = exportPDF;
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden && context) void refresh(context);
  });
  return { setContext, printPart, registerUploadedRun };
}
