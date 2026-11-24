const endpoint = "/api/cad/3d/fit";
const clothFields = [
  ["warpNPerMm", "Жёсткость по основе, Н/мм"],
  ["weftNPerMm", "Жёсткость по утку, Н/мм"],
  ["shearNPerMm", "Жёсткость на сдвиг, Н/мм"],
  ["bendingNmm", "Жёсткость на изгиб, Н·мм"],
  ["massGsm", "Плотность ткани, г/м²"],
];

export function mountFitTools(host, { request, getAttachment, getFoamNodes, onChange, onResult }) {
  host.innerHTML = `
    <fieldset class="pattern-settings fit-settings">
      <legend>Ткань</legend>
      <label>Материал <select data-fit-fabric disabled></select></label>
      ${clothFields.map(([name, label]) =>
        `<label>${label}<input type="number" min="0" step="any" name="${name}"></label>`).join("")}
      <button class="button" type="button" data-fit="cloth">Сохранить свойства ткани</button>
    </fieldset>
    <fieldset class="pattern-settings fit-settings">
      <legend>Поролон</legend>
      <label>Зона <select data-fit-zone aria-label="Зона поролона"></select></label>
      <label>Толщина до опоры, мм <input type="number" min="0.1" step="any" name="foamThicknessMm"></label>
      <label>Коэффициент трения ткани о поролон <input type="number" min="0" step="any" name="friction"></label>
      <label>Модуль для чернового расчёта, кПа <input type="number" min="0.01" step="any" name="foamModulusKPa"></label>
      <label>Замеры «сжатие %, давление кПа», по строке на точку
        <textarea name="foamCurve" rows="3" placeholder="0 0"></textarea>
      </label>
      <button class="button" type="button" data-fit="foam">Назначить выбранному объекту</button>
      <button class="button" type="button" data-fit="remove">Убрать зону</button>
    </fieldset>
    <label class="fit-calibration"><input type="checkbox" data-fit-measured> Введены измеренные свойства</label>
    <div class="pattern-tool-actions">
      <button class="button primary" type="button" data-fit="run">Рассчитать посадку</button>
      <button class="button" type="button" data-fit="cancel" hidden>Отменить расчёт</button>
      <button class="button" type="button" data-fit="hide" hidden>Скрыть результат</button>
    </div>
    <label data-fit-map-label hidden>Карта
      <select data-fit-map><option value="tensionNPerMm">Натяжение ткани</option>
        <option value="warpPct">Удлинение по основе</option><option value="weftPct">Удлинение по утку</option>
        <option value="compressionMm">Сжатие поролона</option><option value="gapMm">Зазор контакта</option>
      </select>
    </label>
    <output data-fit-scale hidden></output>
    <p role="status"></p>`;
  const get = selector => host.querySelector(selector);
  const fabric = get("[data-fit-fabric]"), zone = get("[data-fit-zone]"), measured = get("[data-fit-measured]");
  const thickness = get('[name="foamThicknessMm"]'), modulus = get('[name="foamModulusKPa"]'), curve = get('[name="foamCurve"]');
  const friction = get('[name="friction"]'), scale = get("[data-fit-scale]");
  const map = get("[data-fit-map]"), mapLabel = get("[data-fit-map-label]"), status = get('[role="status"]');
  const fields = Object.fromEntries(clothFields.map(([name]) => [name, get(`[name="${name}"]`)]));
  const buttons = Object.fromEntries([...host.querySelectorAll("[data-fit]")].map(el => [el.dataset.fit, el]));
  const settings = () => getAttachment()?.state?.fit || {};
  const materials = () => getAttachment()?.state?.panels || [];
  const life = new AbortController();
  let job, operation, timer, calculated;
  const format = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });
  const post = (url, data, signal) => request(url, { method: "POST", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

  function choices(select, values) {
    const previous = select.value;
    const options = values.map(([value, text]) => {
      const option = host.ownerDocument.createElement("option");
      option.value = value;
      option.textContent = text;
      return option;
    });
    select.replaceChildren(...options);
    if (values.some(([value]) => value === previous)) select.value = previous;
  }

  function showCloth() {
    const panel = materials().find(item => (item.fabricName || "") === fabric.value);
    const values = settings().panelMaterials?.[panel?.id] || settings();
    for (const [name, input] of Object.entries(fields)) input.value = values[name] ?? "";
  }

  function showFoam() {
    const values = settings().foamZones?.[zone.value] || {};
    thickness.value = values.foamThicknessMm ?? "";
    modulus.value = values.foamModulusKPa ?? "";
    friction.value = values.friction ?? "";
    curve.value = (values.foamCurve || []).map(point => point.compressionPct + " " + point.pressureKPa).join("\n");
  }

  function controls() {
    buttons.cancel.hidden = !operation;
    buttons.run.disabled = Boolean(operation) || !materials().length;
    buttons.hide.hidden = !calculated;
    mapLabel.hidden = !calculated;
    scale.hidden = !calculated;
    fabric.disabled = !materials().length;
  }

  function refresh() {
    choices(fabric, [...new Set(materials().map(item => item.fabricName || ""))]
      .map(value => [value, value || "Без названия"]));
    const foam = settings().foamNodeIds || [];
    choices(zone, [["", "Выбранный объект"], ...foam.map(id => [id, getFoamNodes(id)?.name || id])]);
    measured.checked = settings().calibrated === true;
    showCloth();
    showFoam();
    controls();
  }

  function invalidate() {
    const stale = calculated || operation;
    clearTimeout(timer);
    operation = null;
    if (job) {
      void post(endpoint + "-cancel", { id: job }).catch(() => {});
      job = null;
    }
    calculated = null;
    onResult(null);
    if (stale) status.textContent = "Настройки или лекала изменились. Рассчитайте посадку заново.";
    controls();
  }

  fabric.onchange = showCloth;
  zone.onchange = showFoam;
  measured.onchange = () => onChange({ ...settings(), calibrated: measured.checked });
  buttons.cloth.onclick = () => {
    if (Object.values(fields).some(input => !input.reportValidity())) return;
    const values = Object.fromEntries(Object.entries(fields).filter(([, input]) => input.value !== "")
      .map(([name, input]) => [name, input.valueAsNumber]));
    const panelMaterials = { ...settings().panelMaterials };
    for (const item of materials().filter(item => (item.fabricName || "") === fabric.value)) {
      panelMaterials[item.id] = values;
    }
    onChange({ ...settings(), panelMaterials, calibrated: false });
  };
  buttons.foam.onclick = () => {
    const selected = getFoamNodes(zone.value || null);
    if (!selected?.ids.length) {
      status.textContent = "Выберите объект с поверхностью поролона в дереве модели.";
      return;
    }
    if (!thickness.reportValidity() || !modulus.reportValidity() || !friction.reportValidity() || !thickness.value) {
      status.textContent = "Укажите толщину поролона до опоры.";
      return;
    }
    const rows = curve.value.trim().split(/\n/).filter(row => row.trim()).map(row => row.trim().split(/\s+/));
    if (rows.some(row => row.length !== 2 || row.some(value => !Number.isFinite(Number(value.replace(",", ".")))))) {
      status.textContent = "В каждой строке замеров укажите процент сжатия и давление в кПа.";
      return;
    }
    if (!rows.length && !modulus.value) {
      status.textContent = "Укажите модуль поролона или введите таблицу замеров.";
      return;
    }
    const foamCurve = rows.map(([compressionPct, pressureKPa]) =>
      ({ compressionPct: Number(compressionPct.replace(",", ".")), pressureKPa: Number(pressureKPa.replace(",", ".")) }));
    const foamZones = { ...settings().foamZones };
    for (const id of selected.ids) foamZones[id] = { foamThicknessMm: thickness.valueAsNumber,
      ...(friction.value ? { friction: friction.valueAsNumber } : {}),
      ...(modulus.value ? { foamModulusKPa: modulus.valueAsNumber } : {}), ...(rows.length ? { foamCurve } : {}) };
    onChange({ ...settings(), foamZones, foamNodeIds: [...new Set([...(settings().foamNodeIds || []), ...selected.ids])], calibrated: false });
  };
  buttons.remove.onclick = () => {
    if (!zone.value) return;
    const foamZones = { ...settings().foamZones };
    delete foamZones[zone.value];
    onChange({ ...settings(), foamZones, foamNodeIds: (settings().foamNodeIds || []).filter(id => id !== zone.value), calibrated: false });
  };
  buttons.cancel.onclick = () => { invalidate(); status.textContent = "Расчёт отменён."; };
  buttons.hide.onclick = () => {
    calculated = null;
    onResult(null);
    controls();
  };
  function showResult() {
    if (!calculated) return;
    const metric = map.value;
    const peak = calculated.panels.reduce((maximum, panel) => panel.strain.reduce(
      (value, sample) => Math.max(value, Math.abs(sample[metric] || 0)), maximum), 0);
    const unit = metric.endsWith("Pct") ? "%" : metric.endsWith("Mm") ? "мм" : "Н/мм";
    scale.textContent = "Синий: 0 · красный: " + format.format(peak) + " " + unit;
    onResult(calculated, metric);
  }
  map.onchange = showResult;
  buttons.run.onclick = async () => {
    if (operation || !getAttachment()) return;
    const draft = getAttachment();
    const token = {};
    operation = token;
    calculated = null;
    onResult(null);
    controls();
    status.textContent = "Рассчитываем посадку…";
    const fail = error => {
      if (operation !== token) return;
      operation = null;
      job = null;
      status.textContent = error.message;
      controls();
    };
    try {
      const { id } = await post(endpoint, draft, life.signal);
      if (operation !== token || life.signal.aborted) {
        void post(endpoint + "-cancel", { id }).catch(() => {});
        return;
      }
      job = id;
      const poll = async () => {
        try {
          const response = await request(endpoint + "?id=" + encodeURIComponent(id), { signal: life.signal });
          if (operation !== token) return;
          if (response.status === "running") { timer = setTimeout(poll, 500); return; }
          if (response.status !== "done") throw new Error(response.message || "Расчёт прерван.");
          calculated = response.result;
          operation = null;
          job = null;
          showResult();
          const gap = calculated.metrics?.seamGapMm;
          status.textContent = [calculated.converged ? "Расчёт сошёлся." : "Равновесие не подтверждено.",
            calculated.calibrated ? "" : "Предварительная оценка.",
            Number.isFinite(gap) ? "Зазор швов " + format.format(gap) + " мм." : "",
            ...(calculated.warnings || [])].filter(Boolean).join(" ");
          controls();
        } catch (error) { fail(error); }
      };
      void poll();
    } catch (error) { fail(error); }
  };

  return { refresh, invalidate, destroy() { invalidate(); life.abort(); } };
}
