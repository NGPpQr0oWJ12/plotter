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
    chevron: "m9 6 6 6-6 6",
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

export function mountWorkplaces({ request, navigateLayout, addRegisteredParts, finishRegisteredCut, releaseRegisteredCut, openFile,
  getActiveCut, onExit, layoutWidths }) {
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
  modal.setAttribute("aria-labelledby", "workplace-dialog-title");
  document.body.append(modal);
  let entries = [];
  let entriesById = new Map();
  const expandedFolders = storedFolders();
  let tab = null;
  let designerView = "registry";
  let selectedFolder = null;
  let registrySearch = "";
  let cad = null;
  let selectedOrder = null;
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
    entries = (await get("/api/registry")).entries;
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

  function formDialog(title, content, submit, save) {
    const heading = el("h2", title);
    heading.id = "workplace-dialog-title";
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

  function itemPicker(kinds, previous = [], { kit = false } = {}) {
    const chosen = new Map(previous.map(item => [item.id, item.quantity]));
    const section = el("div", "", `workplace-picker${kit ? " registry-kit-picker" : ""}`);
    const browser = el("div", "", "workplace-picker-browser");
    const search = input("search", "", { placeholder: "Название или путь к папке" });
    const results = el("div", "", "workplace-picker-results");
    const count = el("small", "");
    const selected = el("div", "", "registry-picked-list");
    const choices = entries.filter(entry => kinds.includes(entry.kind));
    const directory = folderSelect("");
    directory.options[0].textContent = "Все папки";
    const inFolder = (entry) => !directory.value || trailOf(find(entry.parent)).some(parent => parent.id === directory.value);
    function itemText(entry) {
      const text = el("span", el("strong", entry.name));
      text.append(el("small", `${labels[entry.kind]} · ${pathOf(find(entry.parent))}`));
      return text;
    }
    function amountInput(entry, value, disabled = false) {
      const amount = input("number", value, { min: 1, max: 50000, required: true, disabled,
        ariaLabel: `Количество: ${entry.name}` });
      amount.oninput = () => chosen.set(entry.id, amount.valueAsNumber);
      return amount;
    }
    function drawChosen() {
      count.textContent = `Выбрано: ${chosen.size}`;
      if (!kit) return;
      selected.replaceChildren();
      for (const [id, quantity] of chosen) {
        const entry = find(id) ?? { id, name: "Недоступная деталь", kind: "part" };
        const row = el("div", itemText(entry), "workplace-pick-row registry-picked-row");
        const remove = button("Убрать", () => {
          chosen.delete(id);
          drawChosen();
          drawMatches().get(id)?.focus({ preventScroll: true });
          if (!browser.contains(document.activeElement)) search.focus({ preventScroll: true });
        });
        remove.setAttribute("aria-label", `Убрать ${entry.name} из комплекта`);
        row.append(amountInput(entry, quantity), remove);
        selected.append(row);
      }
      if (!selected.children.length) selected.append(el("p", "Детали ещё не выбраны.", "workplace-empty"));
    }
    function drawMatches() {
      const needle = search.value.trim().toLocaleLowerCase();
      const toggles = new Map();
      results.replaceChildren();
      for (const entry of choices) {
        if (!inFolder(entry) || !pathOf(entry).toLocaleLowerCase().includes(needle)) continue;
        const row = el("label", "", `workplace-pick-row${kit ? " registry-kit-choice" : ""}`);
        const toggle = input("checkbox", "", { checked: chosen.has(entry.id) });
        const amount = kit ? null : amountInput(entry, chosen.get(entry.id) ?? 1, !toggle.checked);
        toggle.onchange = () => {
          if (amount) amount.disabled = !toggle.checked;
          if (toggle.checked) chosen.set(entry.id, amount?.valueAsNumber ?? 1);
          else chosen.delete(entry.id);
          drawChosen();
        };
        row.append(toggle, itemText(entry));
        if (amount) row.append(amount);
        results.append(row);
        toggles.set(entry.id, toggle);
      }
      if (!results.children.length) results.append(el("p", "Подходящих записей нет.", "workplace-empty"));
      return toggles;
    }
    search.oninput = drawMatches;
    directory.onchange = drawMatches;
    if (kit) browser.append(el("h3", "Добавить детали"));
    browser.append(field("Фильтр по папке", directory), field("Поиск по реестру", search), results);
    section.append(browser);
    if (kit) {
      const composition = el("section", el("h3", "Детали в комплекте"), "registry-picked");
      composition.append(count, selected);
      section.append(composition);
    } else section.append(count);
    drawMatches();
    drawChosen();
    return { section, values: () => Array.from(chosen, ([id, quantity]) => ({ id, quantity })) };
  }

  function recordDialog(kind, old, onSaved) {
    let record = old;
    const name = input("text", old?.name ?? "", { required: true, maxLength: 255 });
    const parent = folderSelect(old ? old.parent : selectedFolder, old);
    const model = input("checkbox", "", { checked: old?.model === true });
    const picker = kind === "kit" ? itemPicker(["part"], old?.members, { kit: true }) : null;
    const fields = el("div", "", "workplace-form-grid");
    fields.append(field("Название", name), field(kind === "folder" ? "Родительская папка" : "Папка записи", parent));
    if (kind === "folder") fields.append(field("Обозначить как изделие", model));
    if (picker) fields.append(picker.section);
    if (old && kind !== "drawing") {
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
      }));
      remove.classList.add("registry-delete");
      const move = button("Перенести в другую папку", () => {
        if (modal.querySelector('[type="submit"]').disabled) return;
        const restore = suspendDialog(move);
        moveRegistryEntry(record, {
          returnTo: restore,
          onMoved: (moved) => {
            record = { ...record, parent: moved.parent, revision: moved.revision };
            parent.value = record.parent ?? "";
          },
        });
      });
      actions.append(move, remove);
      if (picker) {
        const open = button("Раскладка комплекта", execute(async () => {
          if (modal.querySelector('[type="submit"]').disabled) return;
          if (name.value !== record.name || (parent.value || null) !== record.parent
            || JSON.stringify(picker.values()) !== JSON.stringify(record.members)) {
            throw new Error("Сохраните свойства комплекта перед открытием раскладки.");
          }
          await kitLayoutDialog(record, suspendDialog(open));
        }));
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
      registrySearch = "";
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

  function renderRegistry() {
    revealFolder(selectedFolder);
    const search = input("search", registrySearch, { placeholder: "Название или путь к папке" });
    const table = el("div", "", "registry-list");
    const tree = el("nav", "", "registry-tree");
    tree.setAttribute("aria-label", "Папки реестра");
    const location = el("p", "", "registry-location");
    const content = el("section", location, "registry-body");
    content.append(field("Поиск по всему реестру", search), table);
    const columns = el("div", tree, "registry-columns");
    columns.append(content);
    const childrenByParent = new Map();
    for (const folder of folders()) {
      if (!childrenByParent.has(folder.parent)) childrenByParent.set(folder.parent, []);
      childrenByParent.get(folder.parent).push(folder);
    }
    const folderButtons = new Map();
    function browseFolder(id, focusTree = false) {
      selectedFolder = id;
      revealFolder(id);
      registrySearch = search.value = "";
      drawTree();
      drawMatches();
      if (focusTree) folderButtons.get(id)?.focus({ preventScroll: true });
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
      location.textContent = `Папка: ${pathOf(find(selectedFolder))}`;
      table.replaceChildren();
      for (const entry of entries.filter(item => needle ? pathOf(item).toLocaleLowerCase().includes(needle) : item.parent === selectedFolder)) {
        const row = el("div", "", "registry-row");
        row.dataset.registryId = entry.id;
        const title = el("div", el("strong", entry.name));
        const metadata = el("div", el("span", entry.model ? "Изделие" : labels[entry.kind], "registry-kind"), "registry-row-meta");
        if (needle) metadata.append(el("small", pathOf(find(entry.parent))));
        title.append(metadata);
        const identity = el("div", registryIcon(entry.kind), "registry-entry");
        identity.dataset.kind = entry.kind;
        identity.append(title);
        const tools = el("div", "", "registry-row-actions");
        if (entry.kind === "folder") tools.append(button("Открыть", () => browseFolder(entry.id, true)));
        if (["part", "drawing"].includes(entry.kind)) tools.append(button("Редактировать", execute(async () => openCad(await get("/api/registry", { id: entry.id })))));
        if (entry.kind === "kit") tools.append(button("Раскладка комплекта", execute(() => kitLayoutDialog(entry)), { primary: true }));
        tools.append(button("Свойства", execute(async () => recordDialog(entry.kind, await get("/api/registry", { id: entry.id })))));
        if (entry.kind !== "drawing") {
          const remove = button("Удалить", execute(() => deleteRegistryEntry(entry)));
          remove.classList.add("registry-delete");
          tools.append(button("Переместить", () => moveRegistryEntry(entry)), remove);
        }
        row.append(identity, tools);
        table.append(row);
      }
      if (!table.children.length) table.append(el("p", needle ? "Совпадений нет" : "В этой папке пока нет записей", "workplace-empty"));
    }
    search.oninput = () => { registrySearch = search.value; drawMatches(); };
    panel.append(workHeading("Реестр лекал", "", [
      button("Папка", () => recordDialog("folder", null)), button("Комплект", () => recordDialog("kit", null)),
      button("Новый чертёж", () => openCad(null), { primary: true }),
    ]), columns);
    drawTree();
    drawMatches();
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
    if (!old) content.append(el("p", "Если выбран один комплект и ширина и количество совпадут, в наряд скопируется его сохранённая раскладка.", "workplace-form-note"));
    if (order.published) content.append(el("p", "Если изменить состав или ширину, подготовьте и отправьте эту раскладку оператору заново.", "workplace-form-note"));
    formDialog(old ? "Изменить пункт раскроя" : "Добавить ткань", content, "Сохранить", async () => {
      await post("/api/orders", { action: "save-cut", orderId: order.id, orderRevision: order.revision, id: old?.id,
        name: title.value, widthMm: width.valueAsNumber, members: picker.values() });
      await render();
    });
  }

  async function showLayout(next) {
    const result = await navigateLayout(next);
    if (result === false) return;
    currentLayout = next;
    const mode = next.kitId ? "kit" : result?.demo ? "demo" : next.mode;
    const heading = el("strong", mode === "kit" ? `Раскладка комплекта · ${result?.name || next.name}`
      : result?.name || "Самостоятельный раскрой");
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
    if (!currentLayout) return true;
    if (await navigateLayout(null) === false) return false;
    currentLayout = null;
    return true;
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
    if (!await leaveLayout()) return;
    await onExit?.();
    cad?.destroy();
    cad = null;
    tab = selectedOrder = selectedFolder = null;
    registrySearch = "";
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
