const STORAGE_KEY = "plotter.cad.shortcuts";
const MODIFIERS = ["Ctrl", "Alt", "Shift"];
const MODIFIER_KEYS = new Set(["Alt", "AltGraph", "Control", "Meta", "Shift"]);
const CTRL_KEYS = new Set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789=-[]");
const CTRL_SHIFT_KEYS = new Set("ABCDGIJKLMNOPQRSTUVWYZ0123456789=[]");
const ALT_KEYS = new Set(["D", "E", "F"]);
const PRINTABLE_CODES = {
  Backquote: "`", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
  Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/",
};
const PRINTABLE_KEYS = new Set(Object.values(PRINTABLE_CODES));

function acceptedKey(key) {
  return /^[A-Z0-9]$/.test(key) || PRINTABLE_KEYS.has(key);
}

function shortcut(key, modifiers) {
  if (!acceptedKey(key)) return null;
  const has = modifier => modifiers.includes(modifier);
  if (has("Ctrl") && (has("Shift") ? CTRL_SHIFT_KEYS : CTRL_KEYS).has(key)) return null;
  if (has("Alt") && (has("Ctrl") || ALT_KEYS.has(key))) return null;
  return [...MODIFIERS.filter(has), key].join("+");
}

function parseShortcut(value) {
  if (typeof value !== "string" || !value) return null;
  const parts = value.split("+");
  const key = parts.pop().toUpperCase();
  if (parts.some(modifier => !MODIFIERS.includes(modifier)) || new Set(parts).size !== parts.length) return null;
  return shortcut(key, parts);
}

function fromEvent(event) {
  if (event.isComposing || event.metaKey || event.getModifierState("AltGraph")) return null;
  const { code, key } = event;
  const physicalKey = /^(?:Key([A-Z])|Digit(\d))$/.exec(code);
  const value = physicalKey ? physicalKey[1] || physicalKey[2] : PRINTABLE_CODES[code] || key.toUpperCase();
  const modifiers = [
    ...(event.ctrlKey ? ["Ctrl"] : []),
    ...(event.altKey ? ["Alt"] : []),
    ...(event.shiftKey ? ["Shift"] : []),
  ];
  return shortcut(value, modifiers);
}

function element(root, tag, className = "", text = "") {
  const node = root.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

export function mountCadShortcuts(container, { commands, activate, signal }) {
  const root = container.ownerDocument;
  const gear = container.querySelector('[data-action="shortcuts"]');
  if (!gear || signal?.aborted) return;
  const lifecycle = new AbortController();
  const listener = { signal: lifecycle.signal };
  const commandLabels = new Map(commands.map(({ id, label }) => [id, label]));
  const standardKeys = new Map(commands.filter(command => command.builtInKeys).map(({ id, builtInKeys }) => [id, builtInKeys]));
  const controls = new Map();
  for (const control of container.querySelectorAll("[data-tool], [data-action]")) {
    const id = control.dataset.tool || control.dataset.action;
    if (commandLabels.has(id)) controls.set(id, control);
  }
  const rows = new Map();
  const bindings = new Map();
  const keys = new Map();
  let overrides = {};
  let capturing = null;
  let storageError = false;

  try {
    const saved = JSON.parse(root.defaultView.localStorage.getItem(STORAGE_KEY));
    if (saved && typeof saved === "object" && !Array.isArray(saved)) overrides = saved;
  } catch {
    // Start from the default shortcuts if the stored values cannot be read.
  }

  function refreshBindings() {
    bindings.clear();
    keys.clear();
    function register(id, value) {
      const key = parseShortcut(value);
      if (!key || keys.has(key)) return;
      bindings.set(id, key);
      keys.set(key, id);
    }
    for (const { id } of commands) {
      if (Object.hasOwn(overrides, id)) register(id, overrides[id]);
    }
    for (const { id, defaultKey } of commands) {
      if (!Object.hasOwn(overrides, id)) register(id, defaultKey);
    }
    for (const [id, control] of controls) {
      const shortcuts = displayKeys(id);
      if (shortcuts.length) control.setAttribute("aria-keyshortcuts", shortcuts.map(key => key.replace(/^Ctrl\+/, "Control+")).join(" "));
      else control.removeAttribute("aria-keyshortcuts");
    }
  }

  function displayKeys(id) {
    return [...(standardKeys.get(id) || []), bindings.get(id)].filter(Boolean);
  }

  function save() {
    try {
      root.defaultView.localStorage.setItem(STORAGE_KEY, JSON.stringify(overrides));
      storageError = false;
    } catch {
      storageError = true;
    }
  }

  const dialog = element(root, "dialog", "plotter-dialog cad-shortcut-dialog");
  dialog.id = "cad-shortcut-dialog";
  dialog.setAttribute("aria-labelledby", "cad-shortcut-title");
  const title = element(root, "h2", "", "Горячие клавиши");
  title.id = "cad-shortcut-title";
  title.tabIndex = -1;
  const heading = element(root, "header", "drawing-dialog-heading cad-shortcut-heading");
  heading.append(title);
  const body = element(root, "div", "drawing-dialog-body cad-shortcut-body");
  const instructions = element(root, "p", "cad-shortcut-instructions",
    "Выберите «Назначить» и нажмите клавишу или сочетание. Буквы обозначены по латинской раскладке. Esc отменяет ввод.");
  const list = element(root, "ul", "cad-shortcut-list");
  const status = element(root, "p", "cad-shortcut-status");
  status.setAttribute("role", "status");
  status.hidden = true;

  function announce(message = "", failure = false) {
    status.textContent = message;
    status.classList.toggle("is-error", failure);
    status.hidden = !message;
  }

  function describeChange(message) {
    announce(storageError ? message + " Браузер не сохранил настройки для следующего сеанса." : message, storageError);
  }

  function render() {
    for (const [id, { row, key, assign, clear }] of rows) {
      const active = capturing === id;
      row.classList.toggle("is-capturing", active);
      key.textContent = active ? "Нажмите клавишу" : displayKeys(id).join(" / ") || "—";
      assign.textContent = active ? "Отменить ввод" : "Назначить";
      assign.setAttribute("aria-label", assign.textContent + " — " + commandLabels.get(id));
      clear.disabled = !bindings.has(id);
    }
  }

  function startCapture(id) {
    if (capturing === id) {
      capturing = null;
      announce("Ввод отменён.");
    } else {
      capturing = id;
      rows.get(id).assign.focus();
      announce("Нажмите сочетание для команды «" + commandLabels.get(id) + "».");
    }
    render();
  }

  function apply(id, value) {
    capturing = null;
    overrides = { ...overrides, [id]: value };
    refreshBindings();
    save();
    render();
  }

  function action(label, callback) {
    const button = element(root, "button", "button", label);
    button.type = "button";
    button.addEventListener("click", callback, listener);
    return button;
  }

  for (const { id, label } of commands) {
    const row = element(root, "li", "cad-shortcut-row");
    const name = element(root, "span", "cad-shortcut-name", label);
    const key = element(root, "kbd", "cad-shortcut-key");
    const assign = action("Назначить", () => startCapture(id));
    assign.classList.add("cad-shortcut-assign");
    const clear = action("Сбросить клавишу", () => {
      assign.focus();
      apply(id, null);
      describeChange("Клавиша для команды «" + label + "» удалена.");
    });
    clear.classList.add("cad-shortcut-clear");
    clear.setAttribute("aria-label", "Сбросить клавишу — " + label);
    row.append(name, key, assign, clear);
    list.append(row);
    rows.set(id, { row, key, assign, clear });
  }

  const footer = element(root, "footer", "dialog-actions cad-shortcut-actions");
  const reset = action("Вернуть значения по умолчанию", () => {
    capturing = null;
    overrides = {};
    refreshBindings();
    save();
    render();
    describeChange("Восстановлены сочетания по умолчанию.");
  });
  const close = action("Готово", () => dialog.close());
  close.classList.add("primary");
  footer.append(reset, close);
  body.append(instructions, status, list);
  dialog.append(heading, body, footer);
  container.append(dialog);
  gear.setAttribute("aria-haspopup", "dialog");
  gear.setAttribute("aria-controls", dialog.id);

  dialog.addEventListener("keydown", event => {
    event.stopPropagation();
    if (!capturing || event.isComposing || MODIFIER_KEYS.has(event.key)) return;
    if (event.key === "Tab") {
      capturing = null;
      render();
      announce("Ввод отменён.");
      return;
    }
    event.preventDefault();
    if (event.repeat) return;
    if (event.key === "Escape") {
      capturing = null;
      render();
      announce("Ввод отменён.");
      return;
    }
    const key = fromEvent(event);
    if (!key) {
      announce("Это сочетание используется системой, браузером или редактором. Выберите другое.", true);
      return;
    }
    const conflict = keys.get(key);
    if (conflict && conflict !== capturing) {
      announce(key + " уже назначено команде «" + commandLabels.get(conflict) + "». Сначала освободите это сочетание.", true);
      return;
    }
    const label = commandLabels.get(capturing);
    apply(capturing, key);
    describeChange("Команде «" + label + "» назначено " + key + ".");
  }, listener);
  dialog.addEventListener("close", () => {
    capturing = null;
    announce();
    render();
    gear.focus({ preventScroll: true });
  }, listener);
  gear.addEventListener("click", () => {
    if (dialog.open) return;
    refreshBindings();
    render();
    dialog.showModal();
    title.focus({ preventScroll: true });
  }, listener);

  container.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.repeat || event.isComposing || !container.contains(event.target)
        || root.querySelector("dialog[open]")
        || event.target.closest?.("input, textarea, select, [role=textbox]")
        || event.target.isContentEditable) return;
    const id = keys.get(fromEvent(event));
    if (!id || activate(id) === false) return;
    event.preventDefault();
    event.stopPropagation();
  }, listener);

  signal?.addEventListener("abort", () => {
    lifecycle.abort();
    dialog.remove();
  }, { once: true });
  refreshBindings();
  render();
}
