import { mountCadEditor } from "/designer-cad.js";
import { confirmAction } from "/confirm-action.js";

const $ = (id) => document.getElementById(id);
const labels = { folder: "Папка", drawing: "Чертёж", part: "Деталь", kit: "Комплект" };
const states = { pending: "Ожидает", active: "В работе", done: "Выполнен" };

function registryIcon(kind) {
  const paths = {
    folder: "M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
    drawing: "M5 3h9l5 5v13H5ZM14 3v6h5m-11 8 3-5 4 5Z",
    part: "M4 7l4-3h9l3 4v12H4ZM4 14h3m10 0h3",
    kit: "m12 3 9 5-9 5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
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

function button(title, action, { primary = false, disabled = false } = {}) {
  const node = el("button", title, `button${primary ? " primary" : ""}`);
  node.type = "button";
  node.disabled = disabled;
  node.addEventListener("click", action);
  return node;
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

function progressPercent(done, total) {
  return total ? Math.floor(done * 100 / total) : 0;
}

const dateParts = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit" });
const today = () => dateParts.format(new Date());
const showDate = (value) => value.split("-").reverse().join(".");

function orderDates(order) {
  const dates = el("span", "", "workplace-order-dates");
  for (const [label, value] of [["Поставлен", order.placedOn], ["Выполнить до", order.dueOn]]) {
    const item = el("span", el("span", label, "workplace-order-label"));
    const date = value ? el("time", showDate(value)) : el("strong", label === "Поставлен" ? "Дата не указана" : "Срок не указан");
    if (value) date.dateTime = value;
    item.append(date);
    dates.append(item);
  }
  return dates;
}

const ROLE_STORAGE_KEY = "plotter.workplace";
const roleNames = { operator: "Оператор", designer: "Конструктор" };

export function mountWorkplaces({ request, navigateLayout, addRegisteredParts, finishRegisteredCut, releaseRegisteredCut, openFile, getActiveCut, onExit }) {
  const page = $("workplace-page");
  const dock = $("workplace-dock");
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
  document.body.append(modal);
  let entries = [];
  let tab = null;
  let designerView = "registry";
  let selectedFolder = null;
  let cad = null;
  let selectedOrder = null;
  let currentLayout = null;
  let renderEpoch = 0;
  let pending = false;

  const post = (url, payload) => request(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const get = (url, params = {}) => request(`${url}?${new URLSearchParams(params)}`);
  const find = (id) => entries.find(entry => entry.id === id);
  const folders = () => entries.filter(entry => entry.kind === "folder");

  function pathOf(entry) {
    const segments = [entry.name];
    for (let parent = find(entry.parent); parent; parent = find(parent.parent)) segments.unshift(parent.name);
    return segments.join(" / ");
  }

  function folderSelect(value) {
    return select([["", "Корень реестра"], ...folders().map(folder => [folder.id, pathOf(folder)])], value);
  }

  async function refreshRegistry() {
    entries = (await get("/api/registry")).entries;
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

  function formDialog(title, content, submit, save) {
    const heading = el("h2", title);
    const header = el("header", heading, "drawing-dialog-heading");
    const form = el("form");
    const body = el("div", content, "drawing-dialog-body workplace-dialog-body");
    const error = el("p", "", "workplace-dialog-error");
    error.setAttribute("role", "alert");
    body.append(error);
    const footer = el("footer", button("Отмена", () => modal.close()), "dialog-actions");
    const action = el("button", submit, "button primary");
    action.type = "submit";
    footer.append(action);
    form.append(body, footer);
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (action.disabled) return;
      error.textContent = "";
      action.disabled = true;
      try {
        await save();
        if (modal.contains(form)) modal.close("saved");
      } catch (failure) { error.textContent = failure.message; }
      finally { action.disabled = false; }
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

  function itemPicker(kinds, previous = []) {
    const chosen = new Map(previous.map(item => [item.id, item.quantity]));
    const section = el("div", "", "workplace-picker");
    const search = input("search", "", { placeholder: "Поиск по названию и папкам", ariaLabel: "Поиск по реестру" });
    const results = el("div", "", "workplace-picker-results");
    const count = el("small", "");
    const choices = entries.filter(entry => kinds.includes(entry.kind));
    const directory = folderSelect("");
    directory.options[0].textContent = "Все папки";
    const inFolder = (entry) => {
      if (!directory.value) return true;
      for (let parent = find(entry.parent); parent; parent = find(parent.parent)) {
        if (parent.id === directory.value) return true;
      }
      return false;
    };
    function redraw() {
      const needle = search.value.trim().toLocaleLowerCase();
      results.replaceChildren();
      for (const entry of choices) {
        if (!inFolder(entry) || !pathOf(entry).toLocaleLowerCase().includes(needle)) continue;
        const row = el("label", "", "workplace-pick-row");
        const toggle = input("checkbox", "", { checked: chosen.has(entry.id) });
        const amount = input("number", chosen.get(entry.id) ?? 1, { min: 1, max: 50000, required: true,
          disabled: !toggle.checked, ariaLabel: `Количество: ${entry.name}` });
        const text = el("span", el("strong", entry.name));
        text.append(el("small", `${labels[entry.kind]} · ${entry.parent ? pathOf(find(entry.parent)) : "Корень"}`));
        toggle.onchange = () => {
          amount.disabled = !toggle.checked;
          if (toggle.checked) chosen.set(entry.id, amount.valueAsNumber);
          else chosen.delete(entry.id);
          count.textContent = `Выбрано: ${chosen.size}`;
        };
        amount.oninput = () => chosen.set(entry.id, amount.valueAsNumber);
        row.append(toggle, text, amount);
        results.append(row);
      }
      if (!results.children.length) results.append(el("p", "Подходящих записей нет.", "workplace-empty"));
      count.textContent = `Выбрано: ${chosen.size}`;
    }
    search.oninput = redraw;
    directory.onchange = redraw;
    section.append(field("Папка", directory), search, results, count);
    redraw();
    return { section, values: () => Array.from(chosen, ([id, quantity]) => ({ id, quantity })) };
  }

  function recordDialog(kind, old, onSaved) {
    const name = input("text", old?.name ?? "", { required: true, maxLength: 255 });
    const parent = folderSelect(old ? old.parent : selectedFolder);
    const model = input("checkbox", "", { checked: old?.model === true });
    const picker = kind === "kit" ? itemPicker(["part"], old?.members) : null;
    const fields = el("div", "", "workplace-form-grid");
    fields.append(field("Название", name), field("Папка", parent));
    if (kind === "folder") fields.append(field("Обозначить как изделие", model));
    if (picker) fields.append(picker.section);
    formDialog(old ? `Свойства · ${old.name}` : labels[kind], fields, "Сохранить", async () => {
      const result = await post("/api/registry", { action: "save", kind, name: name.value, parent: parent.value || null,
        ...(old ? { id: old.id, revision: old.revision } : {}), ...(kind === "folder" ? { model: model.checked } : {}),
        ...(picker ? { members: picker.values() } : {}) });
      await refreshRegistry();
      onSaved?.(result);
      await render();
    });
  }

  function renderRegistry() {
    const search = input("search", "", { placeholder: "Найти папку, деталь или комплект", ariaLabel: "Поиск по реестру" });
    const table = el("div", "", "registry-list");
    const tree = el("nav", "", "registry-tree");
    tree.setAttribute("aria-label", "Папки реестра");
    const content = el("section", search, "registry-body");
    content.append(table);
    const columns = el("div", tree, "registry-columns");
    columns.append(content);
    function draw() {
      tree.replaceChildren();
      const folderButton = (entry, level) => {
        const branch = button(entry ? entry.name : "Все папки", () => { selectedFolder = entry?.id ?? null; search.value = ""; draw(); });
        branch.prepend(registryIcon("folder"));
        branch.style.paddingInlineStart = `${14 + level * 16}px`;
        branch.setAttribute("aria-pressed", String((entry?.id ?? null) === selectedFolder));
        tree.append(branch);
      };
      folderButton(null, 0);
      const walk = (parent, level) => {
        for (const folder of folders().filter(item => item.parent === parent)) { folderButton(folder, level); walk(folder.id, level + 1); }
      };
      walk(null, 1);
      const needle = search.value.trim().toLocaleLowerCase();
      table.replaceChildren();
      for (const entry of entries.filter(item => needle ? pathOf(item).toLocaleLowerCase().includes(needle) : item.parent === selectedFolder)) {
        const row = el("div", "", "registry-row");
        const title = el("span", el("strong", entry.name));
        title.append(el("small", `${entry.model ? "Изделие" : labels[entry.kind]}${needle && entry.parent ? ` · ${pathOf(find(entry.parent))}` : ""}`));
        const identity = el("div", registryIcon(entry.kind), "registry-entry");
        identity.append(title);
        row.append(identity);
        if (entry.kind === "folder") row.append(button("Открыть", () => { selectedFolder = entry.id; search.value = ""; draw(); }));
        if (["part", "drawing"].includes(entry.kind)) row.append(button("Редактировать", execute(async () => openCad(await get("/api/registry", { id: entry.id })))));
        row.append(button("Свойства", execute(async () => recordDialog(entry.kind, await get("/api/registry", { id: entry.id })))));
        table.append(row);
      }
      if (!table.children.length) table.append(el("p", needle ? "Совпадений нет" : "В этой папке пока нет записей", "workplace-empty"));
    }
    search.oninput = draw;
    panel.append(workHeading("Реестр лекал", selectedFolder ? pathOf(find(selectedFolder)) : "", [
      button("Папка", () => recordDialog("folder", null)), button("Комплект", () => recordDialog("kit", null)),
      button("Новый чертёж", () => openCad(null), { primary: true }),
    ]), columns);
    draw();
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
    const savePart = ({ entities }) => new Promise((resolve, reject) => {
      const title = input("text", "", { required: true, maxLength: 255 });
      const parent = folderSelect(record?.parent ?? selectedFolder);
      const content = el("div", field("Название детали", title), "workplace-form-grid");
      content.append(field("Папка", parent));
      modal.addEventListener("close", () => { if (modal.returnValue !== "saved") reject(new Error("Сохранение детали отменено.")); }, { once: true });
      formDialog("Сохранить деталь", content, "Сохранить", async () => {
        const part = await post("/api/registry", { action: "save", kind: "part", parent: parent.value || null, name: title.value, entities });
        await refreshRegistry();
        resolve(part);
      });
    });
    cad = mountCadEditor(host, { document, request, onBack: back, onSavePart: savePart, onSaveDocument: async (value) => {
      record = await post("/api/registry", { action: "save", kind: record?.kind ?? "drawing", parent: record ? record.parent : selectedFolder,
        ...(record ? { id: record.id, revision: record.revision } : {}), ...value });
      await refreshRegistry();
      return record;
    } });
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
    const published = Boolean(old?.published);
    const number = input("text", old?.number ?? "", { required: true, maxLength: 255 });
    const model = select([["", "Выберите папку изделия"], ...folders().filter(folder => folder.model).map(folder => [folder.id, pathOf(folder)])], old?.modelId);
    model.required = true;
    const amount = input("number", old?.quantity ?? 1, { min: 1, max: 50000, required: true });
    const fields = el("div", field("Номер заказа", number), "workplace-form-grid");
    if (!published) fields.append(field("Изделие", model), field("Количество изделий", amount));
    if (!published && old?.cuts.length) {
      model.disabled = true;
      fields.append(el("p", "Чтобы сменить изделие, сначала удалите пункты раскроя.", "workplace-form-note"));
      const notice = el("p", "После изменения количества раскладки по всем тканям нужно подготовить заново.", "workplace-form-note");
      notice.hidden = true;
      amount.oninput = () => { notice.hidden = amount.valueAsNumber === old.quantity; };
      fields.append(notice);
    }
    const placedOn = input("date", old ? old.placedOn ?? "" : today(), { required: !old || Boolean(old.placedOn) });
    const dueOn = input("date", old?.dueOn ?? "", { required: !old || Boolean(old.dueOn), min: placedOn.value });
    placedOn.oninput = () => { dueOn.min = placedOn.value; };
    const notes = Object.assign(el("textarea"), { value: old?.notes ?? "", maxLength: 2000, rows: 3,
      placeholder: "Например, сначала раскроить подкладку" });
    const dates = el("div", field("Дата постановки", placedOn), "workplace-date-fields");
    dates.append(field("Срок выполнения", dueOn));
    fields.append(dates, field("Комментарий для оператора", notes));
    formDialog(old ? "Изменить наряд" : "Новый наряд", fields, "Сохранить", async () => {
      const result = await post("/api/orders", { action: published ? "update-meta" : "save", number: number.value,
        ...(!published ? { modelId: model.value, quantity: amount.valueAsNumber } : {}),
        ...(placedOn.value ? { placedOn: placedOn.value } : {}), ...(dueOn.value ? { dueOn: dueOn.value } : {}), notes: notes.value,
        ...(old ? { id: old.id, revision: old.revision } : {}) });
      selectedOrder = result.id;
      await render();
    });
  }

  function cutDialog(order, old) {
    const title = input("text", old?.name ?? "", { required: true, maxLength: 255, placeholder: "Например, Подкладка" });
    const width = input("number", old?.widthMm ?? "", { min: 1, step: "any", required: true });
    const picker = itemPicker(["part", "kit"], old?.members);
    const content = el("div", field("Название ткани", title), "workplace-form-grid");
    content.append(field("Рабочая ширина полотна, мм", width), el("strong", "Состав на одно изделие"), picker.section);
    if (order.published) content.append(el("p", "Если изменить состав или ширину, подготовьте и отправьте эту раскладку оператору заново.", "workplace-form-note"));
    formDialog(old ? "Изменить пункт раскроя" : "Добавить ткань", content, "Сохранить", async () => {
      await post("/api/orders", { action: "save-cut", orderId: order.id, orderRevision: order.revision, id: old?.id,
        name: title.value, widthMm: width.valueAsNumber, members: picker.values() });
      await render();
    });
  }

  async function showLayout(next) {
    const result = await navigateLayout(next);
    currentLayout = next;
    const mode = result?.demo ? "demo" : next.mode;
    const heading = el("strong", result?.name || "Самостоятельный раскрой");
    if (mode === "design" || mode === "preview") heading.append(` · ${mode === "design" ? "Подготовка" : "Предпросмотр"}`);
    if (mode === "demo") heading.append(" · Демо");
    const error = el("span", "", "workplace-dock-error");
    error.setAttribute("role", "alert");
    dock.replaceChildren(button(next.id ? "К наряду" : "К нарядам", execute(async () => { await leaveLayout(); await render(); })), heading, error);
    if (mode === "design") {
      dock.append(button("Предпросмотр", execute(() => showLayout({ ...next, mode: "preview", editable: true }))));
    } else if (mode === "preview") {
      if (next.editable) dock.append(button("Редактировать раскладку", execute(() => showLayout({ ...next, mode: "design" }))));
    } else if (result?.status !== "done" || mode === "demo") {
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
          message: `Подтвердите, что раскрой «${result.name}» фактически выполнен.`,
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
    if (!currentLayout) return;
    await navigateLayout(null);
    currentLayout = null;
  }

  function renderOrder(order) {
    const operator = tab === "operator";
    const draft = !order.published;
    const done = order.cuts.filter(cut => cut.status === "done").length;
    const actions = [button("Все наряды", execute(async () => { selectedOrder = null; await render(); }))];
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
    panel.append(workHeading(`Наряд № ${order.number}`, `${order.modelName} · ${order.quantity} шт.`, actions));
    const overview = el("section", orderDates(order), "workplace-order-overview");
    if (order.notes) overview.append(el("p", order.notes, "workplace-order-notes"));
    panel.append(overview);
    if (operator || order.published) panel.append(progressRow(done, order.cuts.length));
    const list = el("div", "", "workplace-cut-list");
    for (const cut of order.cuts) {
      const row = el("article", "", "workplace-cut-row");
      const main = el("div", el("h2", cut.name));
      main.append(el("p", `Полотно ${cut.widthMm} мм`));
      const tools = el("div", "", "workplace-actions");
      const released = cut.released !== false;
      const state = draft ? cut.prepared ? "Готов к отправке" : "Требуется раскладка"
        : released ? states[cut.status] : "Конструктор обновляет раскладку";
      tools.append(el("span", state, `workplace-chip ${cut.status}`));
      if (operator && released) {
        tools.append(button(cut.status === "pending" ? "Приступить" : cut.status === "active" ? "Продолжить" : "Просмотреть",
          execute(() => showLayout({ id: cut.id, mode: "operator" })), { primary: cut.status !== "done" }));
        tools.append(button("Демо", execute(() => showLayout({ id: cut.id, mode: "demo" }))));
      } else if (!operator && (draft || cut.status === "pending")) {
        tools.append(button("Изменить ткань и состав", () => cutDialog(order, cut)));
        if (draft || !released) {
          tools.append(button("Раскладка", execute(() => showLayout({ id: cut.id, mode: "design" })), { primary: true }));
          if (!draft) tools.append(button("Отправить раскладку", execute(async () => {
            await post("/api/orders", { action: "publish-cut", id: cut.id, orderId: order.id, orderRevision: order.revision });
            await render();
          }), { primary: true }));
        } else {
          tools.append(button("Предпросмотр", execute(() => showLayout({ id: cut.id, mode: "preview" }))));
        }
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
      } else if (!operator) tools.append(button("Предпросмотр", execute(() => showLayout({ id: cut.id, mode: "preview" }))));
      row.append(main, tools);
      list.append(row);
    }
    if (!list.children.length) list.append(el("p", "Добавьте ткань и выберите детали для раскладки.", "workplace-empty"));
    panel.append(list);
  }

  function progressRow(done, total) {
    const percent = progressPercent(done, total);
    const row = el("div", "", "workplace-progress");
    const bar = el("progress");
    Object.assign(bar, { max: 100, value: percent, ariaLabel: "Выполнение наряда" });
    row.append(bar, el("span", `${done} из ${total} · ${percent}%`));
    return row;
  }

  async function render() {
    const epoch = ++renderEpoch;
    page.classList.remove("is-cad");
    if (tab === "designer") await refreshRegistry();
    if (epoch !== renderEpoch) return;
    const operator = tab === "operator";
    panel.replaceChildren();
    if (!operator) {
      const nav = el("nav", "", "workplace-subnav");
      for (const [view, title] of [["registry", "Реестр"], ["orders", "Наряды"]]) {
        const control = button(title, execute(async () => { designerView = view; selectedOrder = null; await render(); }));
        control.setAttribute("aria-pressed", String(designerView === view));
        nav.append(control);
      }
      panel.append(nav);
      if (designerView === "registry") { renderRegistry(); return; }
    }
    const params = operator ? { published: 1 } : {};
    const result = await get("/api/orders", selectedOrder ? { ...params, id: selectedOrder } : params);
    if (epoch !== renderEpoch) return;
    if (selectedOrder) { renderOrder(result); return; }
    const actions = operator ? [button("Новый раскрой", execute(() => showLayout({ mode: "standalone" })), { primary: true })]
      : [button("Новый наряд", () => orderDialog(null), { primary: true })];
    if (operator) actions.unshift(button("Продолжить раскрой", execute(async () => {
      const id = await getActiveCut();
      await showLayout(id ? { mode: "operator", id } : { mode: "standalone" });
    })));
    panel.append(workHeading(operator ? "Наряды на раскрой" : "Наряды", "", actions));
    const cards = el("div", "", "workplace-cards");
    for (const order of result.orders) {
      const card = el("article", "", "workplace-card");
      const identity = el("div", el("h2", `Наряд № ${order.number}`), "workplace-card-identity");
      identity.append(el("p", `${order.modelName} · ${order.quantity} шт.`));
      const summary = el("div", "", "workplace-card-materials");
      summary.append(el("span", "Ткани", "workplace-order-label"));
      summary.append(el("span", order.materials?.map(material => `${material.name} · ${material.widthMm} мм`).join(", ") || "Ткани не добавлены"));
      const progress = el("div", order.published ? progressRow(order.done, order.total)
        : el("small", "Черновик", "workplace-chip"), "workplace-card-progress");
      const open = button("Открыть", execute(async () => { selectedOrder = order.id; await render(); }));
      open.setAttribute("aria-label", `Открыть наряд № ${order.number}`);
      card.append(identity, orderDates(order), summary, progress, open);
      if (order.notes) card.append(el("p", order.notes, "workplace-card-notes"));
      cards.append(card);
    }
    if (!cards.children.length) cards.append(el("p", operator ? "Отправленные наряды появятся здесь." : "Создайте наряд для изделия из реестра.", "workplace-empty"));
    panel.append(cards);
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
    await leaveLayout();
    await onExit?.();
    cad?.destroy();
    cad = null;
    tab = selectedOrder = selectedFolder = null;
    designerView = "registry";
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
  return { openLayoutPart };
}
