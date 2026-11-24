const api = "/api/cad/3d/";
const mm = new Intl.NumberFormat("ru", { maximumFractionDigits: 1 });
const pct = new Intl.NumberFormat("ru", { maximumFractionDigits: 4 });
const distance = (a, b) => Math.hypot(...a.map((value, i) => value - b[i]));

export function notchPositionOnStitch([from, to], fraction, side) {
  const { vertices, fractions, range: [start, end] } = side;
  for (let i = 1; i < vertices.length; i++) {
    const a = vertices[i - 1], b = vertices[i];
    const forward = a === from && b === to;
    if (!forward && !(a === to && b === from)) continue;
    const offset = forward ? fraction : 1 - fraction;
    const along = fractions[i - 1] + offset * (fractions[i] - fractions[i - 1]);
    const position = (along - start) / (end - start);
    if (position > .0001 && position < .9999) return position;
  }
  return null;
}

function drawPattern(canvas, panel, color, cut = []) {
  if (!panel?.vertices?.length || !panel.loops?.length) return "";
  const bounds = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of [...panel.vertices, ...cut.flatMap(item => item.points || [])]) {
    bounds[0] = Math.min(bounds[0], x);
    bounds[1] = Math.min(bounds[1], y);
    bounds[2] = Math.max(bounds[2], x);
    bounds[3] = Math.max(bounds[3], y);
  }
  const width = Math.max(120, Math.round(canvas.getBoundingClientRect().width) || 300);
  const height = Math.max(130, Math.min(230, Math.round(width * .6)));
  const ratio = Math.min(canvas.ownerDocument.defaultView?.devicePixelRatio || 1, 2);
  canvas.style.height = height + "px";
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  const w = bounds[2] - bounds[0], h = bounds[3] - bounds[1];
  if (ctx) {
    const scale = Math.min((width - 20) / (w || 1), (height - 20) / (h || 1));
    const project = ([x, y]) => [(width - w*scale)/2 + (x - bounds[0])*scale, (height + h*scale)/2 - (y - bounds[1])*scale];
    const path = (points, closed) => {
      if (!points?.length) return;
      ctx.moveTo(...project(points[0]));
      for (let i = 1; i < points.length; i++) ctx.lineTo(...project(points[i]));
      if (closed) ctx.closePath();
    };
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.beginPath();
    for (const loop of panel.loops) path(loop.map(i => panel.vertices[i]), true);
    ctx.fillStyle = /^#[0-9a-f]{6}$/i.test(color) ? color + "50" : "#d7e7eb";
    ctx.fill("evenodd");
    ctx.strokeStyle = "#2c484f";
    ctx.lineWidth = 1.6;
    ctx.stroke();
    ctx.beginPath();
    for (const item of cut) if (!item.reference) path(item.points, item.closed);
    ctx.strokeStyle = "#12639a";
    ctx.stroke();
    ctx.beginPath();
    ctx.setLineDash([4, 3]);
    for (const guide of panel.guides || []) path(guide.points, false);
    if (panel.grain) path(panel.grain, false);
    ctx.strokeStyle = "#5d646e";
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#9d3e26";
    for (const mark of panel.marks || []) {
      ctx.beginPath();
      ctx.arc(...project(mark.point), 3, 0, 2*Math.PI);
      ctx.fill();
    }
  }
  return mm.format(w) + " × " + mm.format(h) + " мм";
}

export function mountPatternTools(host, {
  getAttachment, getScope = () => null, request, onState, onTransfer,
  onGeometry = () => {}, onJoinSelection = () => {}, onPanelSelection = () => {},
  onPickEdge = () => {}, onCancelPick = () => {}, onPairHighlight = () => {}, onToggleFix = () => {},
  onBusyChange = () => {},
}) {
  const doc = host.ownerDocument;
  const lifecycle = new AbortController();
  host.classList.add("pattern-tools");
  host.hidden = true;
  host.innerHTML = `
    <section class="pattern-tool-section" data-out="parts-section">
      <div class="pattern-tool-actions"><h3>Детали</h3><button type="button" class="button" data-do="calculate">Пересчитать</button></div>
      <div class="pattern-part-search"><input type="search" data-in="part-search" aria-label="Поиск деталей по номеру, названию или ткани" placeholder="Номер, название или ткань"><output data-out="part-count" aria-label="Найдено деталей"></output></div>
      <div class="pattern-parts" data-out="parts"></div>
      <div data-out="details"></div>
      <details data-out="part-details" class="pattern-part-details">
        <summary>Развёртка и свойства детали</summary>
        <canvas class="pattern-preview" aria-label="Контур плоской детали" hidden></canvas>
        <div class="pattern-tool-actions"><output data-out="dimensions"></output><button type="button" class="button" data-do="remove-panel" hidden>Удалить деталь</button></div>
        <div class="pattern-settings" data-out="part-properties"></div>
      </details>
    </section>
    <details class="pattern-tool-section" data-out="seams-tool">
      <summary>Шов</summary>
      <p data-out="no-seams">Для выбора кромок нужна развёртка деталей.</p>
      <div data-out="pairs-section" hidden>
        <label class="pattern-seam-select">Шов <select data-in="pair"></select></label>
        <p class="pattern-seam-prompt" data-out="pair-prompt" role="status"></p>
        <div class="pattern-seam-sides" data-out="pair-sides" role="group" aria-label="Стороны шва">
          ${[["a", "А"], ["b", "Б"]].map(([side, label]) => `
            <div class="pattern-seam-side" data-side="${side}" data-out="pair-side-${side}" data-state="empty">
              <span class="pattern-seam-badge">${label}</span>
              <div class="pattern-seam-side-copy">
                <strong class="pattern-seam-side-title" data-out="pair-${side}-panel"></strong>
                <span class="pattern-seam-side-detail" data-out="pair-${side}-edge"></span>
                <span class="pattern-seam-side-range" data-out="pair-${side}-range" hidden></span>
              </div>
              <button type="button" class="button" data-do="pair-${side}" aria-pressed="false">Выбрать ${label} на модели</button>
            </div>`).join("")}
        </div>
        <div class="pattern-settings" data-out="pair-edit" hidden>
          <details class="pattern-seam-advanced">
            <summary>Участки и тип шва</summary>
            <div class="pattern-settings">
              <label>Начало А, % <input data-in="a-start" type="number" min="0" max="100" step="any" value="0"></label>
              <label>Конец А, % <input data-in="a-end" type="number" min="0" max="100" step="any" value="100"></label>
              <label>Начало Б, % <input data-in="b-start" type="number" min="0" max="100" step="any" value="0"></label>
              <label>Конец Б, % <input data-in="b-end" type="number" min="0" max="100" step="any" value="100"></label>
              <label><input data-in="reverse" type="checkbox"> Развернуть Б</label>
              <label>Соединение <select data-in="method"><option value="stitch">Стачной шов</option>
                <option value="closure">Застёжка</option></select></label>
            </div>
          </details>
        </div>
        <div class="pattern-seam-actions">
          <button type="button" class="button primary" data-do="pair-save">Сохранить шов</button>
          <button type="button" class="button" data-do="pair-delete" hidden>Удалить шов</button>
        </div>
      </div>
      <details class="pattern-joins" hidden>
        <summary>Общая кромка</summary>
        <label>Общая кромка <select data-in="join"></select></label>
        <div class="pattern-tool-actions">
          <button type="button" class="button" data-do="join">Соединить детали</button>
          <button type="button" class="button" data-do="unjoin" hidden>Убрать соединение</button>
        </div>
      </details>
    </details>
    <details class="pattern-tool-section" data-out="notches-tool">
      <summary>Насечки</summary>
      <div class="pattern-notch-progress" data-out="notch-progress">
        <p data-out="notch-status" role="status"></p>
        <output data-out="notch-location" hidden></output>
      </div>
      <label class="pattern-notch-target" data-out="mark-target-label">Парная насечка <select data-in="mark-target"></select></label>
      <p data-out="no-notches">Для парной насечки выберите сохранённый шов или соединённую общую кромку.</p>
      <div class="pattern-settings" data-out="pair-marks" hidden>
        <label>Насечка, % <input data-in="pair-position" type="number" min="0" max="100" step="any" value="50"></label>
        <button type="button" class="button" data-do="pair-mark">Добавить парную насечку</button>
        <button type="button" class="button" data-do="pair-pick-mark">Указать положение на модели</button>
        <div data-out="pair-intervals"></div>
      </div>
      <div class="pattern-settings" data-out="notches" hidden>
        <label>Позиция насечки, % <input data-in="position" type="number" min="0.0001" max="99.9999" step="any" value="50"></label>
        <button type="button" class="button" data-do="mark">Добавить насечку</button>
        <div data-out="marks"></div>
      </div>
    </details>
    <details class="pattern-tool-section" data-out="transfer-section" hidden>
      <summary>Припуски</summary>
      <div class="pattern-settings">
        <label>Припуск, мм <input data-in="allowance" type="number" min="0" step="any" value="10" required></label>
        <label>Глубина насечки, мм <input data-in="depth" type="number" min="0" step="any" value="2" required></label>
      </div>
      <details>
        <summary>Припуски выбранной детали и крепления</summary>
        <div class="pattern-settings" data-out="allowance-overrides"></div>
      </details>
      <div class="pattern-tool-actions">
        <button type="button" class="button" data-do="check">Проверить выбранные</button>
        <button type="button" class="button primary" data-do="transfer" disabled>Перенести готовые</button>
      </div>
    </details>
    <p class="pattern-status" role="status" aria-live="polite"></p>`;

  function indexed(attribute) {
    return Object.fromEntries([...host.querySelectorAll("[" + attribute + "]")].map(el => [el.getAttribute(attribute), el]));
  }
  const out = indexed("data-out"), fields = indexed("data-in"), actions = indexed("data-do");
  const canvas = host.querySelector("canvas");
  canvas.style.width = "100%";
  const joinSection = host.querySelector(".pattern-joins");
  const status = host.querySelector(".pattern-status");
  const checked = new Map();
  const reports = new Map();
  let modelId, selectedPanel = null, selectedJoin = null, result = null;
  let selectedPair = "", pairSides = {}, notchKind = "";
  let pickTarget = null, notchFeedback = null;
  let epoch = 0, loadTask = null, queue = Promise.resolve(), pending = 0;
  const state = () => getAttachment()?.state || {};
  const transferSettings = () => state().transferSettings || {};
  const scoped = records => (records || []).filter(record => !getScope() || record.nodeId === getScope());
  const scopePanels = () => scoped(state().panels);
  const reportReady = id => reports.get(id)?.ready === true;
  const selectedIds = () => scopePanels().filter(p => checked.get(p.id) !== false).map(p => p.id);
  const activeJoin = () => scoped(result?.joins).find(join => join.id === selectedJoin) || null;
  const existingJoin = () => (state().joins || []).find(join => join.id === selectedJoin);
  const busy = () => pending > 0;
  const alive = () => !lifecycle.signal.aborted;

  function element(tag, text, className) {
    const el = doc.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function button(text, handler) {
    const el = element("button", text, "button");
    el.type = "button";
    el.onclick = handler;
    return el;
  }
  function notify(message = "", error = false) {
    if (!alive()) return;
    status.textContent = message;
    status.classList.toggle("is-error", error);
  }
  function commit(next) {
    const previous = epoch;
    onState(next);
    if (epoch === previous) refresh();
  }
  function serial(operation) {
    pending++;
    onBusyChange();
    render();
    const task = queue.then(() => {
      if (!alive()) throw new Error("Редактор закрыт.");
      return operation();
    });
    queue = task.catch(() => {});
    return task.finally(() => {
      pending--;
      if (alive()) { render(); onBusyChange(); }
    });
  }
  function post(command, attachment, extra = {}) {
    return request(api + command, {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: lifecycle.signal,
      body: JSON.stringify({ id: attachment.id, state: attachment.state || {}, ...extra }),
    });
  }
  function current(revision, attachment) {
    return alive() && revision === epoch && getAttachment()?.id === attachment.id;
  }

  function searchText(value) {
    return String(value ?? "").toLocaleLowerCase("ru").replaceAll("ё", "е").replaceAll("№", " ")
      .replace(/\d+/g, digits => digits.replace(/^0+(?=\d)/, "")).trim();
  }
  function matchesPart(part, words) {
    const text = searchText([part.number, part.name || part.id, part.fabricName].filter(Boolean).join(" "));
    return words.every(word => text.includes(word));
  }
  const searchWords = () => searchText(fields["part-search"].value).split(/\s+/).filter(Boolean);

  function selectPanel(id) {
    const part = scopePanels().find(panel => panel.id === id);
    const next = part?.id || null;
    if (part && !matchesPart(part, searchWords())) fields["part-search"].value = "";
    if (next !== selectedPanel) { selectedPanel = next; onPanelSelection(next); }
    render();
    const selected = !host.hidden && out.parts.querySelector('[aria-pressed="true"]');
    if (selected) {
      const list = out.parts.getBoundingClientRect(), row = selected.parentElement.getBoundingClientRect();
      if (row.bottom > list.bottom) out.parts.scrollTop += row.bottom - list.bottom;
      else if (row.top < list.top) out.parts.scrollTop -= list.top - row.top;
    }
  }
  function selectJoin(id, fraction) {
    if ((id || null) !== selectedJoin) notchFeedback = null;
    selectedJoin = id || null;
    if (id) notchKind = "join";
    if (Number.isFinite(fraction) && fraction > 0 && fraction < 1) fields.position.value = String(+(fraction * 100).toFixed(4));
    onJoinSelection(activeJoin());
    render();
  }

  function panelTitle(panel) {
    if (!panel) return "Деталь";
    const name = panel.name || panel.fabricName || "Деталь";
    const number = panel.number && !name.includes(panel.number) ? panel.number : "";
    const fabric = panel.fabricName && !name.includes(panel.fabricName) ? panel.fabricName : "";
    return [number, name, fabric].filter(Boolean).join(" · ");
  }

  function panelLabel(id) {
    return panelTitle(state().panels?.find(item => item.id === id));
  }

  function edgeChoices() {
    const panels = result?.panels || [];
    const names = new Map((state().panels || []).map(panel => [panel.id, panelTitle(panel)]));
    return [
      ...(result?.seams || []).flatMap((seam, index) => (seam.banks || []).map((bank, bankIndex) => ({
        panelId: bank.panelId, seamId: seam.id, bank: bank.bank, bankId: bank.id,
        label: names.get(bank.panelId), edgeLabel: `Кромка шва ${index + 1}.${bankIndex + 1}`, points: bank.sourcePoints || seam.points,
      }))),
      ...panels.flatMap(panel => (panel.edges || []).map((edge, index) => ({
        panelId: panel.id, edgeId: edge.id, label: names.get(panel.id), edgeLabel: `Кромка ${index + 1}`, points: edge.sourcePoints,
      }))),
    ].filter(edge => edge.points?.length >= 2);
  }

  let pairPanels = null;

  function currentPickPanel(side) {
    return pairPanels?.[side] || pairSides[side]?.panelId || null;
  }

  function setStitchPanels([a, b] = []) {
    if (a === pairPanels?.a && b === pairPanels?.b) return;
    if (a && !b) selectPair("");
    pairPanels = a ? { a, b } : null;
    if (!host.hidden) renderPairs();
  }

  function setPickTarget(side) {
    if (side === pickTarget) return;
    pickTarget = side;
    if (side === "mark") notchFeedback = null;
    if (!host.hidden) {
      renderPairs();
      updateNotchButtons();
      renderNotchFeedback();
    }
  }

  function beginPick(side) {
    setPickTarget(side);
    onPickEdge(side);
  }

  function beginPairPanels(a, b) {
    selectPair("");
    pairPanels = { a, b };
    openSection("seams");
    beginPick("a");
  }

  function candidatesFor(purpose) {
    const edges = pairPanels?.[purpose] ? edgeChoices().filter(edge => edge.panelId === pairPanels[purpose]) : edgeChoices();
    if (purpose === "allowance" || purpose === "notch") return edges.filter(edge => edge.edgeId);
    if (purpose === "b" && pairSides.a) return edges.filter(edge => !sameEdge(edge, pairSides.a));
    if (purpose === "mark") {
      const pair = pairRecord();
      return pair ? edges.filter(edge => sameEdge(edge, pair.a) || sameEdge(edge, pair.b)) : [];
    }
    return edges;
  }

  function sameEdge(left, right) {
    return left && right && left.panelId === right.panelId && (right.edgeId ? left.edgeId === right.edgeId :
      left.seamId === right.seamId && (right.bankId ? left.bankId === right.bankId : (left.bank || 0) === (right.bank || 0)));
  }

  function edgeLength(points) {
    return points?.slice(1).reduce((sum, point, index) => sum + distance(point, points[index]), 0) || 0;
  }

  function pairRecord() { return (state().pairs || []).find(item => item.id === selectedPair); }

  function pairEndpoint(edge) {
    const { panelId, edgeId, seamId, bank, bankId } = edge;
    return edgeId ? { panelId, edgeId } : { panelId, seamId, ...(bankId ? { bankId } : { bank }) };
  }

  function pickPairSide(side, edge, fraction) {
    if (!edge) return;
    if (pickTarget === side) pickTarget = null;
    if (side === "allowance") {
      if (edge.edgeId) {
        selectPanel(edge.panelId);
        allowanceEdge = edge.edgeId;
        renderAllowances();
      }
      return;
    }
    if (side === "mark") {
      const record = pairRecord();
      if (!record) return;
      const endpoint = sameEdge(edge, record.a) ? record.a : sameEdge(edge, record.b) ? record.b : null;
      if (!endpoint) return;
      const [first, last] = endpoint.range || [0, 1];
      const relative = (fraction - first) / (last - first);
      if (relative > 0 && relative < 1) {
        fields["pair-position"].value = String(+((endpoint.reverse ? 1 - relative : relative) * 100).toFixed(4));
        setNotchStatus({ edge, fraction, message: "Позиция выбрана. Добавьте парную насечку." });
      } else setNotchStatus({ message: "Укажите точку внутри выбранного участка шва.", error: true });
      updateNotchButtons();
      return;
    }
    pairSides[side] = pairEndpoint(edge);
    fields[side + "-start"].value = "0";
    fields[side + "-end"].value = "100";
    renderPairs();
  }

  function selectPair(id) {
    if (pickTarget) {
      pickTarget = null;
      onCancelPick();
    }
    pairPanels = null;
    if (id !== selectedPair) notchFeedback = null;
    selectedPair = id;
    if (id) notchKind = "pair";
    else if (notchKind === "pair") notchKind = "";
    const pair = pairRecord();
    pairSides = pair ? { a: pair.a, b: pair.b } : {};
    for (const side of ["a", "b"]) {
      const [first, last] = pairSides[side]?.range || [0, 1];
      fields[side + "-start"].value = String(100 * first);
      fields[side + "-end"].value = String(100 * last);
    }
    fields.reverse.checked = Boolean(pair?.b?.reverse);
    fields.method.value = pair?.method || "stitch";
    renderPairs();
    renderNotchTargets();
  }

  function savePair(pair) {
    const latest = state();
    const pairs = [...(latest.pairs || [])];
    const index = pairs.findIndex(item => item.id === selectedPair);
    if (pair && index < 0) pairs.push(pair);
    else if (pair) pairs[index] = pair;
    else if (index >= 0) pairs.splice(index, 1);
    else return;
    selectedPair = pair?.id || "";
    if (pair) notchKind = "pair";
    if (!pair) pairSides = {};
    commit({ ...latest, pairs });
  }

  function markPair(position) {
    const pair = pairRecord();
    const marks = pair?.marks || [];
    let message = "";
    if (!pair || !(position > 0 && position < 1)) message = "Выберите позицию насечки внутри шва.";
    else if (marks.length >= 100) message = "У шва уже 100 насечек.";
    else if (marks.some(mark => Math.abs(mark - position) < 1e-8)) message = "В этой позиции уже есть насечка.";
    if (message) {
      setNotchStatus({ message, error: true });
      return message;
    }
    const index = marks.filter(mark => mark < position).length;
    const identifiers = pair.markIds || marks.map(mark => pair.id + ":" + mark);
    const ease = Array.from({ length: marks.length + 1 }, (_, i) => pair.ease?.[i] || 0);
    const start = marks[index - 1] || 0, end = marks[index] || 1;
    const fraction = (position - start) / (end - start);
    const target = ease[index];
    ease.splice(index, 1, target * fraction, target * (1 - fraction));
    savePair({ ...pair, marks: [...marks.slice(0, index), position, ...marks.slice(index)],
      markIds: [...identifiers.slice(0, index), crypto.randomUUID(), ...identifiers.slice(index)], ease });
    fields["pair-position"].value = String(+(position * 100).toFixed(4));
    setNotchStatus({ message: `Парная насечка добавлена · ${pct.format(position * 100)} %.`, fraction: position });
    return "";
  }

  function addEdgeNotch(edge, fraction, shared = null) {
    if (busy()) return "Дождитесь расчёта лекал.";
    const latest = state();
    const boundary = result?.panels?.find(panel => panel.id === edge.panelId)?.edges?.find(item => item.id === edge.edgeId);
    if (boundary?.vertices && latest.pairs?.length) {
      const pairIds = new Set(latest.pairs.map(pair => pair.id));
      for (const stitch of result.fitStitches || []) {
        if (!pairIds.has(stitch.id)) continue;
        for (const side of [stitch.a, stitch.b]) {
          if (side.panelId !== edge.panelId) continue;
          const position = notchPositionOnStitch(boundary.vertices, fraction, side);
          if (position === null) continue;
          selectPair(stitch.id);
          return markPair(+position.toFixed(6));
        }
      }
    }
    if (shared) return addJoinNotch(shared.join.id, shared.fraction);
    if (!edge.edgeId || !(fraction > .0001 && fraction < .9999)) return "Укажите точку внутри кромки.";
    const panel = latest.panels?.find(item => item.id === edge.panelId);
    if (!panel) return "У кромки нет лекала.";
    const marks = panel.notches || [];
    const position = +fraction.toFixed(6);
    if (marks.length >= 100) return "У детали уже 100 насечек.";
    const tolerance = .5 / edgeLength(edge.points);
    if (marks.some(mark => mark.edgeId === edge.edgeId && Math.abs(mark.position - position) < tolerance)) {
      return "В этой позиции уже есть насечка.";
    }
    const notch = { id: crypto.randomUUID(), edgeId: edge.edgeId, position };
    commit({ ...latest, panels: latest.panels.map(item => item === panel ? { ...panel, notches: [...marks, notch] } : item) });
    return "";
  }

  function addJoinNotch(id, fraction) {
    if (busy() || !(fraction > .0001 && fraction < .9999)) return "Укажите точку внутри шва.";
    selectJoin(id, fraction);
    if (!activeJoin()) return "Шов на этой кромке не найден.";
    if (!existingJoin()) {
      updateJoin({ id, marks: [fraction] });
      return "";
    }
    return addNotch(fraction) ? "" : "В этой позиции уже есть насечка.";
  }

  function sideRange(side) {
    return [fields[side + "-start"], fields[side + "-end"]].map(input => input.valueAsNumber / 100);
  }

  function validRange(range) {
    return range.every(value => value >= 0 && value <= 1) && range[0] !== range[1];
  }

  function renderPairSide(side, edge, range, edges) {
    const letter = side === "a" ? "А" : "Б";
    const target = currentPickPanel(side);
    const picking = pickTarget === side;
    const card = out["pair-side-" + side];
    const action = actions["pair-" + side];
    card.dataset.state = picking ? "picking" : edge ? "ready" : pairSides[side] ? "missing" : "empty";
    out[`pair-${side}-panel`].textContent = target ? panelLabel(target) : "Выберите ткань";
    let detail = "Кромка не выбрана";
    if (edge) detail = `${edge.edgeLabel} · ${mm.format(edgeLength(edge.points))} мм`;
    else if (pairSides[side]) detail = busy() ? "Кромка рассчитывается" : "Кромка недоступна";
    out[`pair-${side}-edge`].textContent = detail;
    const rangeOut = out[`pair-${side}-range`];
    rangeOut.hidden = !edge;
    rangeOut.textContent = edge && validRange(range)
      ? `${pct.format(range[0] * 100)}–${pct.format(range[1] * 100)} % · ${mm.format(Math.abs(range[1] - range[0]) * edgeLength(edge.points))} мм`
      : "Проверьте границы участка";
    const candidates = pairPanels?.[side] ? edges.filter(item => item.panelId === pairPanels[side]) : edges;
    action.disabled = picking || busy() || !candidates.some(item => side !== "b" || !pairSides.a || !sameEdge(item, pairSides.a));
    action.setAttribute("aria-pressed", String(picking));
    action.textContent = picking ? `Выбираете ${letter}` : edge ? `Заменить ${letter}` : `Выбрать ${letter} на модели`;
  }

  function renderPairs() {
    const edges = edgeChoices();
    const pairs = state().pairs || [];
    const saved = pairRecord();
    const sides = ["a", "b"];
    out["pairs-section"].hidden = !edges.length && !pairs.length && !pairPanels;
    const options = [["", "Новый шов"], ...pairs.map(pair => [pair.id,
      `${pair.name || "Шов"} · ${panelLabel(pair.a?.panelId)} / ${panelLabel(pair.b?.panelId)}`])];
    fields.pair.replaceChildren(...options.map(([id, name]) => {
      const option = element("option", name);
      option.value = id;
      return option;
    }));
    fields.pair.value = selectedPair;
    fields.pair.disabled = busy();
    const chosen = sides.map(side => pairSides[side] && edges.find(edge => sameEdge(edge, pairSides[side])));
    const ranges = sides.map(sideRange);
    onPairHighlight(chosen.map((edge, index) => {
      if (!edge) return edge;
      const reversed = index && fields.reverse.checked;
      return { ...edge, direction: reversed ? -1 : 1, side: index ? "Б" : "А",
        range: validRange(ranges[index]) ? ranges[index] : [0, 1] };
    }));
    for (const [index, side] of sides.entries()) renderPairSide(side, chosen[index], ranges[index], edges);
    const same = saved && sides.every((side, index) => chosen[index] && sameEdge(chosen[index], saved[side]) &&
      (saved[side].range || [0, 1]).every((value, bound) => Math.abs(value - ranges[index][bound]) < 1e-9)) &&
      Boolean(saved.b?.reverse) === fields.reverse.checked && (saved.method || "stitch") === fields.method.value;
    const invalid = ranges.some(range => !validRange(range));
    let prompt = "Выберите первую кромку на модели.";
    if (busy()) prompt = "Обновляем шов и лекала…";
    else if (pickTarget === "a" || pickTarget === "b") {
      prompt = `Щёлкните по кромке ${pickTarget === "a" ? "А" : "Б"} на модели.`;
    } else if (chosen.every(Boolean)) {
      if (invalid) prompt = "Укажите разные границы участка от 0 до 100 %.";
      else if (same) prompt = `${saved.name || "Шов"} сохранён.`;
      else prompt = "Обе кромки выбраны. Сохраните шов.";
    } else if (chosen.some(Boolean)) prompt = `Выберите кромку ${chosen[0] ? "Б" : "А"}.`;
    else if (!edges.length) prompt = "Кромки появятся после расчёта деталей.";
    if (out["pair-prompt"].textContent !== prompt) out["pair-prompt"].textContent = prompt;
    out["pair-edit"].hidden = !chosen.some(Boolean) && !saved;
    actions["pair-save"].disabled = busy() || Boolean(same) || invalid || chosen.some(edge => !edge);
    actions["pair-save"].textContent = saved ? "Сохранить изменения" : "Сохранить шов";
    actions["pair-delete"].hidden = !saved;
    actions["pair-delete"].disabled = busy();
    const intervals = result?.pairs?.find(pair => pair.id === selectedPair)?.intervals || [];
    out["pair-intervals"].replaceChildren(...intervals.map((interval, i) => {
      const row = element("label");
      const input = element("input");
      input.type = "number";
      input.step = "any";
      input.value = saved?.ease?.[i] || 0;
      input.disabled = busy();
      input.onchange = () => {
        if (!Number.isFinite(input.valueAsNumber)) return;
        const pair = pairRecord();
        const ease = Array.from({ length: intervals.length }, (_, index) => pair.ease?.[index] || 0);
        ease[i] = input.valueAsNumber;
        savePair({ ...pair, ease });
      };
      row.append(`Участок ${i + 1} · А ${mm.format(interval.a)} / Б ${mm.format(interval.b)} мм · посадка А − Б`, input);
      if (i < intervals.length - 1) row.append(button("Убрать насечку", () => {
        const pair = pairRecord(), ease = [...(pair.ease || [])];
        ease.splice(i, 2, (ease[i] || 0) + (ease[i + 1] || 0));
        savePair({ ...pair, ease, marks: pair.marks.filter((_, index) => index !== i),
          ...(pair.markIds ? { markIds: pair.markIds.filter((_, index) => index !== i) } : {}) });
      }));
      return row;
    }));
  }
  function updateJoin(nextJoin) {
    const latest = state();
    const joins = [...(latest.joins || [])];
    const index = joins.findIndex(item => item.id === selectedJoin);
    if (nextJoin && index < 0) joins.push(nextJoin);
    else if (nextJoin) joins[index] = nextJoin;
    else if (index >= 0) joins.splice(index, 1);
    else return;
    if (nextJoin) notchKind = "join";
    commit({ ...latest, joins });
  }
  function addNotch(fraction) {
    if (busy() || !activeJoin() || !existingJoin()) {
      setNotchStatus({ message: "Выберите соединённую кромку для насечки.", error: true });
      return false;
    }
    if (!Number.isFinite(fraction) || fraction <= 0 || fraction >= 1) {
      setNotchStatus({ message: "Укажите позицию между 0 и 100 %.", error: true });
      return false;
    }
    const join = existingJoin(), marks = join.marks || [];
    if (marks.length >= 100 || marks.some(mark => Math.abs(mark - fraction) < 1e-8)) {
      setNotchStatus({ message: marks.length >= 100 ? "У соединения уже 100 насечек." : "В этой позиции уже есть насечка.", error: true });
      return false;
    }
    updateJoin({ ...join, marks: [...marks, fraction].sort((a, b) => a - b) });
    setNotchStatus({ message: `Насечка добавлена · ${pct.format(fraction * 100)} %.`, fraction });
    return true;
  }
  function removePanel() {
    const id = selectedPanel;
    if (!id || busy()) return;
    const previous = state();
    const next = { ...previous, panels: previous.panels.filter(panel => panel.id !== id) };
    const removedSeams = new Set((previous.seams || []).filter(seam => seam.placementFor === id).map(seam => seam.id));
    if (previous.seams) next.seams = previous.seams.filter(seam => !removedSeams.has(seam.id));
    if (previous.pairs) next.pairs = previous.pairs.filter(pair => ![pair.a, pair.b].some(
      side => side?.panelId === id || removedSeams.has(side?.seamId)));
    if (previous.joins) {
      const affected = new Set((result?.joins || []).filter(join => join.panelIds.includes(id)).map(join => join.id));
      for (const issue of result?.diagnostics || []) if (issue.panelId === id && issue.joinId) affected.add(issue.joinId);
      next.joins = previous.joins.filter(join => !affected.has(join.id));
    }
    checked.delete(id);
    commit(next);
  }

  function preview() {
    const pattern = result?.panels.find(part => part.id === selectedPanel);
    const saved = scopePanels().find(part => part.id === selectedPanel);
    canvas.hidden = !pattern;
    if (out["part-details"].open || !pattern) {
      out.dimensions.textContent = pattern ? drawPattern(canvas, pattern, saved?.color, reports.get(selectedPanel)?.preview) : "";
    }
  }
  function renderParts(parts) {
    const currentIds = new Set(parts.map(part => part.id));
    if (!currentIds.has(selectedPanel)) {
      const next = parts[0]?.id || null;
      if (selectedPanel !== next) { selectedPanel = next; onPanelSelection(next); }
    }
    const calculated = new Set((result?.panels || []).map(item => item.id));
    out.parts.replaceChildren();
    const words = searchWords();
    const matching = words.length ? parts.filter(part => matchesPart(part, words)) : parts;
    out["part-count"].textContent = words.length ? matching.length + " / " + parts.length : String(parts.length);
    for (const part of matching) {
      const row = element("div", undefined, "pattern-part");
      const name = [part.number, part.name || part.id].filter(Boolean).join(" · ");
      const check = element("input");
      check.type = "checkbox";
      check.checked = checked.get(part.id) !== false;
      check.disabled = busy();
      check.setAttribute("aria-label", "Выбрать для переноса — " + name);
      check.onchange = () => { checked.set(part.id, check.checked); updateTransfer(); };
      const pick = button("", () => selectPanel(part.id));
      pick.classList.add("pattern-part-name");
      pick.append(element("span", name), element("small", part.fabricName || "", "pattern-part-fabric"));
      pick.title = [name, part.fabricName].filter(Boolean).join(" · ");
      pick.setAttribute("aria-pressed", String(selectedPanel === part.id));
      const report = reports.get(part.id);
      const error = result?.diagnostics.some(issue => issue.panelId === part.id && issue.severity === "error");
      const note = report ? (report.ready ? "Готова к переносу" : "Требует исправления") :
        error ? "Требует исправления" : calculated.has(part.id) ? "Развёртка готова" : result ? "Без развёртки" : "";
      row.append(check, pick, element("small", note, "pattern-part-status"));
      out.parts.append(row);
    }
    if (!parts.length) out.parts.textContent = getAttachment() ? "В выбранном объекте пока нет деталей." : "Импортируйте модель SketchUp.";
    else if (!matching.length) out.parts.textContent = "Детали не найдены.";
    out["part-details"].hidden = !selectedPanel;
    actions["remove-panel"].hidden = !selectedPanel;
    actions["remove-panel"].disabled = busy();
    out.details.replaceChildren();
    const diagnostics = (result?.diagnostics || []).filter(issue => issue.panelId === selectedPanel ||
      !issue.panelId && (!getScope() || (state().seams || []).some(seam => seam.id === issue.seamId && seam.nodeId === getScope())));
    const messages = new Set([...diagnostics, ...(reports.get(selectedPanel)?.issues || [])]
      .filter(issue => issue.code !== "ready").map(issue => [issue.message, issue.detail].filter(Boolean).join(" ")));
    for (const message of messages) out.details.append(element("p", message));
    renderPartProperties(parts.find(part => part.id === selectedPanel));
    preview();
  }

  function renderPartProperties(part) {
    const target = out["part-properties"];
    target.replaceChildren();
    if (!part) return;
    function field(label, input) {
      const row = element("label", label);
      input.disabled = busy();
      row.append(input);
      target.append(row);
    }
    function select(values, current) {
      const input = element("select");
      for (const [value, name] of values) {
        const option = element("option", name);
        option.value = value;
        input.append(option);
      }
      input.value = current || values[0][0];
      return input;
    }
    const quantity = element("input");
    quantity.type = "number";
    quantity.min = 1;
    quantity.max = 999;
    quantity.value = part.quantity || 1;
    const side = select([["", "Общая"], ["left", "Левая"], ["right", "Правая"]], part.side);
    const direction = select([["free", "Свободное"], ["grain", "По долевой"], ["nap", "По ворсу"]], part.direction);
    const update = () => {
      if (!quantity.reportValidity() || !Number.isInteger(quantity.valueAsNumber) || busy()) return;
      const latest = state();
      commit({ ...latest, panels: latest.panels.map(item => item.id === part.id ? { ...item,
        side: side.value, quantity: quantity.valueAsNumber, direction: direction.value } : item) });
    };
    quantity.onchange = side.onchange = direction.onchange = update;
    field("Количество", quantity);
    field("Сторона", side);
    field("Направление ткани", direction);
  }

  let allowanceEdge = "";
  function renderAllowances() {
    const target = out["allowance-overrides"];
    target.replaceChildren();
    const pattern = result?.panels.find(panel => panel.id === selectedPanel);
    if (!pattern) return;
    const settings = transferSettings().panels?.[pattern.id] || {};
    const edges = pattern.edges || [];
    function addField(label, key, id) {
      const row = element("label", label);
      const input = element("input");
      input.type = "number";
      input.min = 0;
      input.max = 1000;
      input.step = "any";
      input.placeholder = "По умолчанию";
      input.value = (id ? settings[key]?.[id] : settings[key]) ?? "";
      input.disabled = busy();
      input.onchange = () => {
        if (!input.reportValidity()) return;
        const value = input.value === "" ? undefined : input.valueAsNumber;
        const all = transferSettings();
        const values = { ...settings };
        if (id) {
          values[key] = { ...values[key] };
          if (value === undefined) delete values[key][id];
          else values[key][id] = value;
        } else if (value === undefined) delete values[key];
        else values[key] = value;
        commit({ ...state(), transferSettings: { ...all, panels: { ...all.panels, [pattern.id]: values } } });
        reports.clear();
        render();
      };
      row.append(input);
      target.append(row);
    }
    addField("Припуск детали, мм", "allowanceMm");
    addField("Припуск всех отверстий, мм", "holeAllowanceMm");
    for (const [i, hole] of (pattern.holes || []).entries()) addField("Отверстие " + (i + 1) + ", мм", "holeAllowances", hole.id);
    const list = element("select");
    list.setAttribute("aria-label", "Кромка для припуска или крепления");
    for (const [i, edge] of edges.entries()) {
      const option = element("option", "Кромка " + (i + 1) + " · " + mm.format(edgeLength(edge.points)) + " мм");
      option.value = edge.id;
      list.append(option);
    }
    if (edges.some(edge => edge.id === allowanceEdge)) list.value = allowanceEdge;
    list.disabled = busy() || !edges.length;
    list.onchange = () => { allowanceEdge = list.value; renderAllowances(); };
    const pick = button("Выбрать кромку на модели", () => onPickEdge("allowance"));
    pick.disabled = list.disabled;
    const edge = edges.find(item => item.id === list.value);
    target.append(list, pick);
    if (!edge) return;
    addField("Припуск выбранной кромки, мм", "edgeAllowances", edge.id);
    const index = pattern.loops[edge.loop]?.findIndex((vertex, i, loop) =>
      [vertex, loop[(i + 1) % loop.length]].every(id => edge.vertices.includes(id))) ?? -1;
    if (index >= 0) {
      const fixed = (state().fit?.fixedEdges || []).some(item => item.panelId === pattern.id &&
        (item.edgeId ? item.edgeId === edge.id : item.loop === edge.loop && item.index === index));
      const fix = button(fixed ? "Убрать крепление" : "Закрепить кромку на форме", () => {
        onToggleFix({ panelId: pattern.id, loop: edge.loop, index, edgeId: edge.id });
        renderAllowances();
      });
      fix.disabled = busy();
      target.append(fix);
    }
  }
  function renderJoins(parts) {
    const saved = state().joins || [];
    const available = scoped(result?.joins);
    const allIds = new Set((result?.joins || []).map(join => join.id));
    const ids = new Set(parts.map(part => part.id));
    const stale = result ? saved.filter(join => !allIds.has(join.id) && (!getScope() ||
      result.diagnostics.some(issue => issue.joinId === join.id && (ids.has(issue.panelId) || !issue.panelId)))) : [];
    const names = new Map(parts.map(part => [part.id, part.name || part.id]));
    const committed = new Set(saved.map(join => join.id));
    const select = fields.join;
    select.replaceChildren();
    const option = (value, text) => {
      const el = element("option", text);
      el.value = value;
      select.append(el);
    };
    option("", "Выберите общую кромку");
    available.forEach((join, index) => {
      let length = 0;
      for (let i = 1; i < join.points.length; i++) length += distance(join.points[i - 1], join.points[i]);
      const pair = join.panelIds.map(id => names.get(id) || id).join(" / ");
      option(join.id, (index + 1) + ". " + pair + " · " + mm.format(length) + " мм" + (committed.has(join.id) ? " · соединено" : ""));
    });
    for (const join of stale) option(join.id, "Соединение " + (saved.indexOf(join) + 1) + " · кромка изменилась");
    if (result && selectedJoin && ![...available, ...stale].some(join => join.id === selectedJoin)) {
      selectedJoin = null;
      onJoinSelection(null);
    }
    select.value = selectedJoin || "";
    const join = existingJoin();
    const candidate = activeJoin();
    joinSection.hidden = !available.length && !stale.length;
    select.disabled = busy();
    actions.join.hidden = Boolean(join);
    actions.join.disabled = busy() || !candidate;
    actions.unjoin.hidden = !join;
    actions.unjoin.disabled = busy() || !result;
    out.marks.replaceChildren();
    for (const mark of join?.marks || []) {
      const remove = button(pct.format(mark * 100) + " % ×", () => {
        const latest = existingJoin();
        if (latest && !busy()) updateJoin({ ...latest, marks: (latest.marks || []).filter(value => value !== mark) });
      });
      remove.setAttribute("aria-label", "Удалить насечку на " + pct.format(mark * 100) + " %");
      remove.disabled = busy();
      out.marks.append(remove);
    }
  }

  function renderNotchFeedback() {
    const { edge, fraction, message, error } = notchFeedback || {};
    let step = message || "Щёлкните по кромке ткани, чтобы поставить насечку.";
    if (busy()) step = "Обновляем насечки и лекала…";
    else if (pickTarget === "mark") step = "Укажите положение на выбранном участке шва.";
    else if (edge && !message) step = "Щёлкните, чтобы поставить насечку в этой точке.";
    const status = out["notch-status"];
    if (status.textContent !== step) status.textContent = step;
    out["notch-progress"].dataset.state = busy() ? "working" : error ? "error" : edge ? "edge" : "idle";
    const location = out["notch-location"];
    location.hidden = !edge;
    if (edge) {
      const title = [edge.label || panelLabel(edge.panelId), edge.edgeLabel || "Кромка"].join(" · ");
      const position = Number.isFinite(fraction)
        ? ` · ${mm.format(fraction * edgeLength(edge.points))} мм (${pct.format(fraction * 100)} %)` : "";
      if (location.textContent !== title + position) location.textContent = title + position;
    }
  }

  function setNotchStatus(feedback) {
    notchFeedback = feedback;
    if (!host.hidden) renderNotchFeedback();
  }

  function updateNotchButtons() {
    const pair = pairRecord();
    const pairPosition = fields["pair-position"].valueAsNumber;
    const joinPosition = fields.position.valueAsNumber;
    actions["pair-mark"].disabled = busy() || !pair || !(pairPosition > 0 && pairPosition < 100);
    actions["pair-pick-mark"].disabled = busy() || pickTarget === "mark" || !pair || !candidatesFor("mark").length;
    actions.mark.disabled = busy() || !activeJoin() || !existingJoin() || !(joinPosition > 0 && joinPosition < 100);
  }

  function renderNotchTargets() {
    const select = fields["mark-target"];
    const options = [new Option("Выберите шов или общую кромку", "")];
    for (const pair of state().pairs || []) options.push(new Option(
      `${pair.name || "Шов"} · ${panelLabel(pair.a?.panelId)} / ${panelLabel(pair.b?.panelId)}`, "p:" + pair.id));
    const savedIds = new Set((state().joins || []).map(join => join.id));
    const validIds = new Set(scoped(result?.joins).map(join => join.id));
    for (const item of fields.join.options) {
      if (validIds.has(item.value) && savedIds.has(item.value)) {
        options.push(new Option("Кромка · " + item.textContent, "j:" + item.value));
      }
    }
    select.replaceChildren(...options);
    select.value = notchKind === "pair" ? "p:" + selectedPair : notchKind === "join" ? "j:" + selectedJoin : "";
    if (!select.value) select.value = "";
    select.disabled = busy();
    out["pair-marks"].hidden = !select.value.startsWith("p:");
    out.notches.hidden = !select.value.startsWith("j:");
    out["mark-target-label"].hidden = options.length === 1;
    out["no-notches"].hidden = options.length > 1;
    updateNotchButtons();
    renderNotchFeedback();
  }
  function chooseNotchTarget() {
    const value = fields["mark-target"].value;
    if (value.startsWith("p:")) selectPair(value.slice(2));
    else if (value.startsWith("j:")) selectJoin(value.slice(2));
    else { notchKind = ""; renderNotchTargets(); }
  }
  function openSection(name) {
    const section = { parts: out["parts-section"], seams: out["seams-tool"], notches: out["notches-tool"],
      transfer: out["transfer-section"] }[name];
    if (!section || section.hidden) return;
    if (section.tagName === "DETAILS") section.open = true;
    section.scrollIntoView({ block: "nearest" });
  }
  function updateTransfer() {
    const ids = selectedIds();
    const ready = ids.filter(reportReady).length;
    out["transfer-section"].hidden = !scopePanels().length;
    actions.check.disabled = busy() || !result || !ids.length;
    actions.check.textContent = ids.length ? "Проверить выбранные (" + ids.length + ")" : "Проверить выбранные";
    actions.transfer.disabled = busy() || !result || !ready;
    actions.transfer.textContent = ready ? "Перенести готовые (" + ready + ")" : "Перенести готовые";
    fields.allowance.disabled = fields.depth.disabled = busy();
  }
  function render() {
    if (!alive() || host.hidden) return;
    actions.calculate.disabled = busy() || !getAttachment();
    const parts = scopePanels();
    renderParts(parts);
    renderJoins(parts);
    renderPairs();
    out["seams-tool"].hidden = out["notches-tool"].hidden = !getAttachment();
    out["no-seams"].hidden = !out["pairs-section"].hidden || !joinSection.hidden;
    renderNotchTargets();
    renderAllowances();
    updateTransfer();
  }

  function calculate() {
    if (loadTask) return loadTask;
    const attachment = getAttachment();
    if (!attachment) { render(); return Promise.resolve(null); }
    const revision = epoch;
    const hasMarkup = ["panels", "seams", "pairs", "joins"].some(field => attachment.state?.[field]?.length);
    if (!hasMarkup) {
      result = { panels: [], placements: [], joins: [], seams: [], pairs: [], diagnostics: [], complete: true,
        cadSync: { entities: [], patterns: {}, issues: [] } };
      onGeometry(result);
      render();
      return Promise.resolve(result);
    }
    notify("Рассчитываем детали…");
    const task = serial(async () => {
      if (!current(revision, attachment)) return null;
      const data = await post("calculate", attachment, { sync2d: true });
      if (!current(revision, attachment)) return null;
      result = data;
      onGeometry(data);
      onJoinSelection(activeJoin());
      notify(data.complete ? "" : "Часть разметки требует исправления.", !data.complete);
      return data;
    }).catch(error => {
      if (current(revision, attachment)) notify(error.message, true);
      return null;
    }).finally(() => { if (loadTask === task) loadTask = null; });
    loadTask = task;
    return task;
  }
  function refresh(calculated = null) {
    // The button passes a click event. An accepted edit can pass its calculation.
    if (!calculated?.diagnostics) calculated = null;
    epoch++;
    loadTask = null;
    result = calculated;
    reports.clear();
    const nextId = getAttachment()?.id || null;
    if (nextId !== modelId) {
      fields["part-search"].value = "";
      notchKind = "";
      fields.allowance.value = transferSettings().allowanceMm ?? 10;
      fields.depth.value = transferSettings().notchDepthMm ?? 2;
      pairPanels = null;
      pickTarget = null;
      notchFeedback = null;
      selectedJoin = selectedPanel = null;
      selectedPair = "";
      pairSides = {};
      checked.clear();
      modelId = nextId;
      onPanelSelection(null);
    }
    onGeometry(result);
    onJoinSelection(activeJoin());
    if (result && !result.complete) notify("Часть разметки требует исправления.", true);
    else notify();
    render();
    return result || host.hidden ? Promise.resolve(result) : calculate();
  }
  async function transferCheck(exportReady) {
    const attachment = getAttachment();
    if (!attachment || !result || busy()) return;
    const ids = selectedIds().filter(id => !exportReady || reportReady(id));
    if (!ids.length) return;
    const allowance = fields.allowance.valueAsNumber, depth = fields.depth.valueAsNumber;
    if (![allowance, depth].every(n => Number.isFinite(n) && n >= 0 && n <= 1000)) {
      notify("Укажите припуск и глубину насечки от 0 до 1000 мм.", true);
      return;
    }
    const revision = epoch;
    const panelSettings = Object.fromEntries(Object.entries(transferSettings().panels || {}).filter(([id]) => ids.includes(id)));
    const extra = { allowanceMm: allowance, notchDepthMm: depth, panelIds: ids, panelSettings };
    notify(exportReady ? "Готовим перенос в 2D…" : "Проверяем выбранные детали…");
    try {
      await serial(async () => {
        if (!current(revision, attachment)) return;
        const response = await post(exportReady ? "transfer" : "check-transfer", attachment, extra);
        if (!current(revision, attachment)) return;
        if (exportReady) {
          if (!response.entities?.length) throw new Error("Для переноса не получено контуров.");
          const count = await onTransfer(response.entities, { attachment, ...response });
          notify(count ? "Перенесено в 2D — " + count + " дет." : "Существующие лекала сохранены.");
        } else {
          for (const id of ids) reports.delete(id);
          for (const report of response.panels) reports.set(report.id, report);
          const ready = ids.filter(reportReady).length;
          notify("Готово к переносу " + ready + " из " + ids.length + ".", ready < ids.length);
        }
      });
    } catch (error) { if (current(revision, attachment)) notify(error.message, true); }
  }

  actions.calculate.onclick = refresh;
  fields["part-search"].oninput = () => renderParts(scopePanels());
  fields["mark-target"].onchange = chooseNotchTarget;
  out["notches-tool"].ontoggle = () => {
    const select = fields["mark-target"];
    if (out["notches-tool"].open && !select.value && select.options.length === 2) {
      select.selectedIndex = 1;
      chooseNotchTarget();
    }
  };
  out["part-details"].ontoggle = () => { if (out["part-details"].open) preview(); };
  fields.pair.onchange = () => selectPair(fields.pair.value);
  actions["pair-a"].onclick = () => beginPick("a");
  actions["pair-b"].onclick = () => beginPick("b");
  actions["pair-delete"].onclick = () => savePair(null);
  actions["pair-mark"].onclick = () => markPair(fields["pair-position"].valueAsNumber / 100);
  actions["pair-pick-mark"].onclick = () => beginPick("mark");
  fields["pair-position"].oninput = fields.position.oninput = updateNotchButtons;
  for (const key of ["a-start", "a-end", "b-start", "b-end"]) fields[key].oninput = renderPairs;
  fields.reverse.onchange = renderPairs;
  fields.method.onchange = renderPairs;
  actions["pair-save"].onclick = () => {
    const pair = pairRecord();
    const sides = {};
    for (const side of ["a", "b"]) {
      const range = sideRange(side);
      if (!validRange(range)) {
        notify("Концы участка должны различаться и быть в пределах 0–100 %.", true);
        return;
      }
      sides[side] = { ...pairSides[side], range, reverse: side === "b" && fields.reverse.checked };
    }
    const names = new Set((state().pairs || []).map(item => item.name));
    let number = 1;
    while (names.has("Ш" + String(number).padStart(2, "0"))) number++;
    savePair({ id: crypto.randomUUID(), name: "Ш" + String(number).padStart(2, "0"), ...pair, ...sides, method: fields.method.value });
  };
  actions["remove-panel"].onclick = removePanel;
  fields.join.onchange = () => selectJoin(fields.join.value);
  actions.join.onclick = () => { if (!busy() && activeJoin() && !existingJoin()) updateJoin({ id: selectedJoin, marks: [] }); };
  actions.unjoin.onclick = () => { if (!busy() && existingJoin()) updateJoin(null); };
  actions.mark.onclick = () => addNotch(fields.position.valueAsNumber / 100);
  for (const field of [fields.allowance, fields.depth]) {
    field.max = "1000";
    field.oninput = () => { reports.clear(); notify(); render(); };
    field.onchange = () => {
      if (field.reportValidity()) commit({ ...state(), transferSettings: { ...transferSettings(),
        [field === fields.allowance ? "allowanceMm" : "notchDepthMm"]: field.valueAsNumber } });
    };
  }
  actions.check.onclick = () => transferCheck(false);
  actions.transfer.onclick = () => transferCheck(true);
  const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
    if (alive() && !host.hidden && !canvas.hidden) preview();
  });
  observer?.observe(host);

  return {
    show() {
      host.hidden = false;
      if (modelId !== (getAttachment()?.id || null)) return refresh();
      render();
      return result ? Promise.resolve(result) : calculate();
    },
    hide() { host.hidden = true; },
    refresh,
    scopeChanged() { render(); onJoinSelection(activeJoin()); },
    openSection, selectJoin, selectPanel, addNotch, addEdgeNotch, addJoinNotch, activeJoin, busy, candidatesFor, pickPairSide, beginPairPanels,
    setStitchPanels, setPickTarget, setNotchStatus, currentPickPanel,
    removePart(id) {
      selectPanel(id);
      removePanel();
    },
    checkPanel(proposedState, nodeId, panelId) {
      const attachment = getAttachment();
      if (!attachment) return Promise.reject(new Error("Импортируйте модель SketchUp."));
      const revision = epoch;
      return serial(async () => {
        if (!current(revision, attachment)) throw new Error("Модель изменилась. Выберите деталь заново.");
        const response = await post("check-panel", { ...attachment, state: proposedState }, { nodeId, panelId });
        if (!current(revision, attachment)) throw new Error("Модель изменилась. Выберите деталь заново.");
        return response;
      });
    },
    destroy() {
      lifecycle.abort();
      observer?.disconnect();
      host.replaceChildren();
      host.hidden = true;
    },
  };
}
