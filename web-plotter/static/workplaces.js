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
  dates.append(item);
  return dates;
}

function orderProduct(order) {
  return order?.designation || order?.modelName || "";
}

function orderContext(order) {
  const product = orderProduct(order);
  return product ? `${product} · ${order.quantity} шт.` : "";
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
      registrySearch = "";
      await render();
      status.textContent = "Реестр восстановлен.";
    }, { lockWhileSaving: true });
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
      if (!table.children.length) table.append(el("p", needle ? "Совпадений нет" : "В этой папке пока нет записей", "workplace-empty"));
    }
    search.oninput = () => { registrySearch = search.value; drawMatches(); };
    panel.append(workHeading("Реестр лекал", "", [
      button("Скачать бэкап", execute(() => downloadAttachment("/api/registry-backup", "Реестр Plotter.rar", "Не удалось скачать бэкап реестра."))),
      button("Восстановить бэкап", restoreRegistryBackup),
      button("Папка", () => recordDialog("folder", null)), button("Комплект", () => recordDialog("kit", null)),
      button("Новый чертёж", () => openCad(null), { primary: true }),
    ]), columns);
    drawTree();
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
        : next.id ? { cutId: next.id, mode } : { workspace: 1 };
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
    panel.append(workHeading(`Наряд № ${order.number}`, orderContext(order), actions));
    const overview = el("section", orderDates(order), "workplace-order-overview");
    if (order.notes) overview.append(el("p", order.notes, "workplace-order-notes"));
    panel.append(overview);
    if (operator || order.published) panel.append(progressRow(done, order.cuts.length));
    const list = el("div", "", "workplace-cut-list");
    for (const cut of order.cuts) {
      const layout = { id: cut.id, order, name: cut.name };
      const row = el("article", "", "workplace-cut-row");
      const main = el("div", el("h2", cut.name));
      main.append(el("p", cut.widthMm == null ? "Ширина задаётся в автораскладке" : `Полотно ${cut.widthMm} мм`));
      const tools = el("div", "", "workplace-actions");
      const released = cut.released !== false;
      const draftState = cut.prepared ? "Готов к отправке" : cut.widthMm == null ? "Раскладка у оператора" : "Требуется раскладка";
      const state = draft ? draftState
        : released ? states[cut.status] : "Конструктор обновляет раскладку";
      tools.append(el("span", state, `workplace-chip ${cut.status}`));
      if (operator && released) {
        tools.append(button(cut.status === "pending" ? "Приступить" : cut.status === "active" ? "Продолжить" : "Просмотреть",
          execute(() => showLayout({ ...layout, mode: "operator" })), { primary: cut.status !== "done" }));
        tools.append(button("Демо", execute(() => showLayout({ ...layout, mode: "demo" }))));
      } else if (!operator && (draft || cut.status === "pending")) {
        tools.append(button("Изменить ткань и состав", () => cutDialog(order, cut)));
        if (draft || !released) {
          tools.append(button("Раскладка", execute(() => showLayout({ ...layout, mode: "design" })), { primary: true }));
          if (!draft) tools.append(button("Отправить раскладку", execute(async () => {
            await post("/api/orders", { action: "publish-cut", id: cut.id, orderId: order.id, orderRevision: order.revision });
            await render();
          }), { primary: true }));
        } else {
          tools.append(button("Предпросмотр", execute(() => showLayout({ ...layout, mode: "preview" }))));
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
      } else if (!operator) tools.append(button("Предпросмотр", execute(() => showLayout({ ...layout, mode: "preview" }))));
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
      const order = id && result.orders.find(order => order.materials?.some(material => material.id === id));
      const material = order?.materials.find(material => material.id === id);
      await showLayout(id ? { mode: "operator", id, order, name: material?.name } : { mode: "standalone" });
    })));
    panel.append(workHeading(operator ? "Наряды на раскрой" : "Наряды", "", actions));
    const cards = el("div", "", "workplace-cards");
    for (const order of result.orders) {
      const card = el("article", "", "workplace-card");
      const identity = el("div", el("h2", `Наряд № ${order.number}`), "workplace-card-identity");
      const detail = orderContext(order);
      if (detail) identity.append(el("p", detail));
      const summary = el("div", "", "workplace-card-materials");
      summary.append(el("span", "Ткани", "workplace-order-label"));
      const fabrics = order.materials?.map(material => material.widthMm == null ? material.name : `${material.name} · ${material.widthMm} мм`);
      summary.append(el("span", fabrics?.join(", ") || "Ткани не добавлены"));
      const progress = el("div", order.published ? progressRow(order.done, order.total)
        : el("small", "Черновик", "workplace-chip"), "workplace-card-progress");
      const open = button("Открыть", execute(async () => { selectedOrder = order.id; await render(); }));
      open.setAttribute("aria-label", `Открыть наряд № ${order.number}`);
      card.append(identity, orderDates(order), summary, progress, open);
      if (order.notes) card.append(el("p", order.notes, "workplace-card-notes"));
      cards.append(card);
    }
    if (!cards.children.length) cards.append(el("p", operator ? "Отправленные наряды появятся здесь." : "Создайте наряд и добавьте ткани.", "workplace-empty"));
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
  return { openLayoutPart, hasPendingCadEdits: () => Boolean(cad?.hasUnsavedChanges() || cad?.isBusy()) };
}
