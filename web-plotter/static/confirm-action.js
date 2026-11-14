let confirmationPending = false;

export async function confirmAction({ title, message = "", confirmLabel, cancelLabel = "Отмена", signal }) {
  const dialog = document.getElementById("confirm-action-dialog");
  if (confirmationPending || signal?.aborted || document.querySelector("dialog[open]")) return false;

  const cancel = document.getElementById("confirm-action-cancel");
  const messageNode = document.getElementById("confirm-action-message");
  const invoker = document.activeElement;
  document.getElementById("confirm-action-title").textContent = title;
  document.getElementById("confirm-action-submit").textContent = confirmLabel;
  cancel.textContent = cancelLabel;
  messageNode.textContent = message;
  messageNode.hidden = !message;
  if (message) dialog.setAttribute("aria-describedby", messageNode.id);
  else dialog.removeAttribute("aria-describedby");
  dialog.returnValue = "";

  const abort = () => dialog.close();
  confirmationPending = true;
  try {
    dialog.showModal();
    await new Promise(resolve => {
      dialog.addEventListener("close", resolve, { once: true });
      signal?.addEventListener("abort", abort, { once: true });
      cancel.focus({ preventScroll: true });
    });
    return dialog.returnValue === "confirm" && !signal?.aborted;
  } finally {
    signal?.removeEventListener("abort", abort);
    confirmationPending = false;
    if (invoker?.isConnected) invoker.focus({ preventScroll: true });
  }
}
