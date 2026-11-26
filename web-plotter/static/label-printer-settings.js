export async function chooseLabelPrinter({ getPrinters, signal }) {
  if (signal?.aborted || document.querySelector("dialog[open]")) return null;

  const invoker = document.activeElement;
  const dialog = document.getElementById("label-printer-dialog");
  const select = dialog.querySelector("select");
  const status = dialog.querySelector("#label-printer-status");
  const refresh = dialog.querySelector('[data-action="refresh"]');
  const cancel = dialog.querySelector('[data-action="cancel"]');
  const save = dialog.querySelector('[data-action="save"]');
  let closed = false;

  async function load() {
    if (refresh.disabled) return;
    refresh.disabled = select.disabled = save.disabled = true;
    select.replaceChildren(new Option("Выберите принтер", ""));
    status.textContent = "Загружаем список принтеров…";
    try {
      const { printers, preferred } = await getPrinters();
      if (closed || !dialog.open || signal?.aborted) return;
      const names = [...new Set(printers.filter(name => typeof name === "string" && name.trim()))];
      for (const name of names) select.add(new Option(name, name));
      if (names.includes(preferred)) select.value = preferred;
      else if (names.length === 1) select.value = names[0];
      select.disabled = !names.length;
      save.disabled = !select.value;
      status.textContent = !names.length ? "В Windows не найдено установленных принтеров."
        : preferred && !names.includes(preferred) ? `Сохранённый принтер «${preferred}» недоступен.` : "";
    } catch (error) {
      if (!closed && dialog.open && !signal?.aborted) status.textContent = error?.message || "Не удалось загрузить список принтеров.";
    } finally {
      if (!closed) refresh.disabled = false;
    }
  }

  const dismiss = () => { if (dialog.open) dialog.close(); };
  cancel.onclick = dismiss;
  refresh.onclick = load;
  select.onchange = () => { save.disabled = select.disabled || !select.value; };
  save.onclick = () => { if (!save.disabled && dialog.open) dialog.close("save"); };

  dialog.returnValue = "";
  refresh.disabled = false;
  try {
    const whenClosed = new Promise(resolve => dialog.addEventListener("close", resolve, { once: true }));
    dialog.showModal();
    signal?.addEventListener("abort", dismiss, { once: true });
    void load();
    await whenClosed;
    return dialog.returnValue === "save" && !signal?.aborted ? select.value : null;
  } finally {
    closed = true;
    signal?.removeEventListener("abort", dismiss);
    if (invoker?.isConnected && !document.querySelector("dialog[open]")) invoker.focus({ preventScroll: true });
  }
}
