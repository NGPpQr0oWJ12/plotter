const EPSILON = 1e-8;
const MAX_COORDINATE = 1e9;
const DEG = Math.PI / 180;
const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const mul = (p, k) => [p[0] * k, p[1] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
const cross = (a, b) => a[0] * b[1] - a[1] * b[0];
const norm = p => Math.hypot(...p);
const distance = (a, b) => norm(sub(a, b));
const mix = (a, b, t) => add(a, mul(sub(b, a), t));
const angle = p => Math.atan2(p[1], p[0]) / DEG;
const positiveAngle = degrees => ((degrees % 360) + 360) % 360;
const radial = (degrees, radius) => [radius * Math.cos(degrees * DEG), radius * Math.sin(degrees * DEG)];
const format = value => String(Number(value.toFixed(3)));
const numberFrom = input => input.value.trim() ? Number(input.value.trim().replace(",", ".")) : NaN;

function arcThrough(a, b, c) {
  const u = sub(b, a);
  const v = sub(c, a);
  const determinant = 2 * cross(u, v);
  if (Math.abs(determinant) < EPSILON) return null;
  const center = add(a, [
    (dot(u, u) * v[1] - dot(v, v) * u[1]) / determinant,
    (u[0] * dot(v, v) - v[0] * dot(u, u)) / determinant,
  ]);
  const startAngle = angle(sub(a, center));
  const span = positiveAngle(angle(sub(c, center)) - startAngle);
  const sweep = positiveAngle(angle(sub(b, center)) - startAngle) <= span ? span : span - 360;
  return { center, radius: distance(a, center), startAngle, sweep };
}

function arcPoint(entity, fraction) {
  return add(entity.center, radial(entity.startAngle + fraction * entity.sweep, entity.radius));
}

function ellipsePoint(entity, fraction) {
  const parameter = entity.startAngle + fraction * entity.sweep;
  const axis = entity.axis;
  const coefficients = radial(parameter, 1);
  return add(entity.center, add(mul(axis, coefficients[0]), mul([-axis[1], axis[0]], coefficients[1] * entity.ratio)));
}

function splinePoints(entity) {
  if (entity.fitPoints?.length) {
    const points = entity.fitPoints;
    const count = points.length;
    const at = i => points[entity.closed ? (i + count) % count : Math.max(0, Math.min(count - 1, i))];
    const result = [];
    for (let i = 0; i < count - Number(!entity.closed); i++) {
      const a = at(i - 1);
      const b = at(i);
      const c = at(i + 1);
      const d = at(i + 2);
      for (let j = 0; j < 16; j++) {
        const t = j / 16;
        result.push(b.map((value, k) => .5 * (
          2 * value + (-a[k] + c[k]) * t
          + (2 * a[k] - 5 * value + 4 * c[k] - d[k]) * t ** 2
          + (-a[k] + 3 * value - 3 * c[k] + d[k]) * t ** 3
        )));
      }
    }
    result.push(entity.closed ? points[0] : points.at(-1));
    return result;
  }
  const { controlPoints: points = [], knots = [], weights = [], degree } = entity;
  const count = points.length;
  if (!degree || count <= degree || knots.length !== count + degree + 1) return points;
  const start = knots[degree];
  const end = knots[count];
  const steps = Math.min(4096, Math.max(32, count * 12));
  const result = [];
  let span = degree;
  for (let step = 0; step <= steps; step++) {
    const at = start + (end - start) * step / steps;
    while (span < count - 1 && at >= knots[span + 1]) span++;
    const homogeneous = Array.from({ length: degree + 1 }, (_, k) => {
      const i = span - degree + k;
      const weight = weights[i] ?? 1;
      return [...mul(points[i], weight), weight];
    });
    for (let r = 1; r <= degree; r++) {
      for (let j = degree; j >= r; j--) {
        const i = span - degree + j;
        const interval = knots[i + degree + 1 - r] - knots[i];
        const alpha = interval ? (at - knots[i]) / interval : 0;
        homogeneous[j] = homogeneous[j].map((value, k) => value * alpha + homogeneous[j - 1][k] * (1 - alpha));
      }
    }
    const [x, y, weight] = homogeneous[degree];
    if (weight) result.push([x / weight, y / weight]);
  }
  if (entity.closed && result.length && distance(result[0], result.at(-1)) > EPSILON) result.push(result[0]);
  return result;
}

function bulgeArc(start, end, bulge) {
  const chord = sub(end, start);
  if (norm(chord) < EPSILON || Math.abs(bulge) < EPSILON) return null;
  const center = add(mix(start, end, .5), mul([-chord[1], chord[0]], (1 - bulge ** 2) / (4 * bulge)));
  return { center, radius: distance(start, center), startAngle: angle(sub(start, center)), sweep: 4 * Math.atan(bulge) / DEG };
}

function sampleArc(entity, sample) {
  const steps = Math.max(8, Math.ceil(Math.abs(entity.sweep) / 5));
  return Array.from({ length: steps + 1 }, (_, i) => sample(entity, i / steps));
}

function samples(entity) {
  switch (entity.type) {
    case "line": return [entity.start, entity.end];
    case "circle": return sampleArc({ ...entity, startAngle: 0, sweep: 360 }, arcPoint);
    case "arc": return sampleArc(entity, arcPoint);
    case "ellipse": return sampleArc(entity, ellipsePoint);
    case "spline": return splinePoints(entity);
    case "polyline": {
      const { points, bulges = [], closed } = entity;
      const result = [];
      for (let i = 0; i < points.length - Number(!closed); i++) {
        const arc = bulges[i] && bulgeArc(points[i], points[(i + 1) % points.length], bulges[i]);
        if (arc) result.push(...sampleArc(arc, arcPoint).slice(0, -1));
        else result.push(points[i]);
      }
      if (points.length) result.push(closed ? points[0] : points.at(-1));
      return result;
    }
    default: return [];
  }
}

function curveBounds(entity, path) {
  if (entity.type === "circle") {
    const [x, y] = entity.center;
    const r = entity.radius;
    return [x - r, y - r, x + r, y + r];
  }
  if (entity.type === "arc") {
    const extremes = [0, 90, 180, 270]
      .filter(degrees => positiveAngle(Math.sign(entity.sweep) * (degrees - entity.startAngle)) <= Math.abs(entity.sweep))
      .map(degrees => add(entity.center, radial(degrees, entity.radius)));
    return geometryBounds([path[0], path.at(-1), ...extremes]);
  }
  if (entity.type === "ellipse") {
    const [a, b] = entity.axis;
    const xExtremum = angle([a, -b * entity.ratio]);
    const yExtremum = angle([b, a * entity.ratio]);
    const extremes = [xExtremum, xExtremum + 180, yExtremum, yExtremum + 180]
      .map(degrees => positiveAngle(Math.sign(entity.sweep) * (degrees - entity.startAngle)))
      .filter(progress => progress <= Math.abs(entity.sweep))
      .map(progress => ellipsePoint(entity, progress / Math.abs(entity.sweep)));
    return geometryBounds([path[0], path.at(-1), ...extremes]);
  }
  return geometryBounds(path);
}

function geometryBounds(points) {
  const bounds = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of points) {
    bounds[0] = Math.min(bounds[0], x);
    bounds[1] = Math.min(bounds[1], y);
    bounds[2] = Math.max(bounds[2], x);
    bounds[3] = Math.max(bounds[3], y);
  }
  return bounds;
}

function controls(entity) {
  switch (entity.type) {
    case "line": return [entity.start, entity.end];
    case "circle": return [entity.center, add(entity.center, [entity.radius, 0])];
    case "arc": return [arcPoint(entity, 0), arcPoint(entity, .5), arcPoint(entity, 1), entity.center];
    case "polyline": return entity.points;
    case "spline": return entity.fitPoints ?? entity.controlPoints ?? [];
    case "ellipse": {
      const [a, b] = entity.axis;
      return [entity.center, add(entity.center, entity.axis), add(entity.center, mul([-b, a], entity.ratio))];
    }
    default: return [];
  }
}

function snapPoints(entity, path) {
  const endpoints = controls(entity);
  switch (entity.type) {
    case "line": return [...endpoints, mix(entity.start, entity.end, .5)];
    case "circle": return [entity.center, ...[0, 90, 180, 270].map(a => add(entity.center, radial(a, entity.radius)))];
    case "arc": return endpoints;
    case "polyline": {
      const points = [...entity.points];
      for (let i = 0; i < entity.points.length - Number(!entity.closed); i++) {
        const a = entity.points[i];
        const b = entity.points[(i + 1) % entity.points.length];
        const arc = entity.bulges?.[i] && bulgeArc(a, b, entity.bulges[i]);
        points.push(arc ? arcPoint(arc, .5) : mix(a, b, .5));
      }
      return points;
    }
    case "spline": return [path[0], path.at(-1)].filter(Boolean);
    case "ellipse": return [entity.center, ...[0, .25, .5, .75, 1].map(t => ellipsePoint(entity, t))];
    default: return [];
  }
}

function setGrip(entity, index, point) {
  switch (entity.type) {
    case "line": {
      const fixed = index ? entity.start : entity.end;
      return distance(point, fixed) < EPSILON ? entity : { ...entity, [index ? "end" : "start"]: point };
    }
    case "circle": {
      const radius = distance(entity.center, point);
      if (index && radius < EPSILON) return entity;
      return index ? { ...entity, radius } : { ...entity, center: point };
    }
    case "arc": {
      if (index === 3) return { ...entity, center: point };
      const points = controls(entity).slice(0, 3);
      points[index] = point;
      const arc = arcThrough(...points);
      return arc ? { ...entity, ...arc } : entity;
    }
    case "polyline":
    case "spline": {
      const key = entity.type === "polyline" ? "points" : entity.fitPoints ? "fitPoints" : "controlPoints";
      if (key !== "controlPoints") {
        const vertices = entity[key];
        const previous = index > 0 ? vertices[index - 1] : entity.closed ? vertices.at(-1) : null;
        const next = index < vertices.length - 1 ? vertices[index + 1] : entity.closed ? vertices[0] : null;
        if ((previous && distance(previous, point) < EPSILON) || (next && distance(next, point) < EPSILON)) return entity;
      }
      return { ...entity, [key]: entity[key].map((p, i) => i === index ? point : p) };
    }
    case "ellipse": {
      const axis = sub(point, entity.center);
      if (!index) return { ...entity, center: point };
      if (norm(axis) < EPSILON) return entity;
      if (index === 1) return { ...entity, axis };
      return { ...entity, ratio: Math.min(1, Math.max(EPSILON, Math.abs(cross(entity.axis, axis)) / dot(entity.axis, entity.axis))) };
    }
    default: return entity;
  }
}

function transform(entity, point, reflected) {
  const next = { ...entity };
  for (const field of ["start", "end", "center"]) if (entity[field]) next[field] = point(entity[field]);
  for (const field of ["points", "fitPoints", "controlPoints"]) if (entity[field]) next[field] = entity[field].map(point);
  if (entity.type === "arc") next.startAngle = angle(sub(point(arcPoint(entity, 0)), next.center));
  if (entity.type === "ellipse") {
    next.axis = sub(point(add(entity.center, entity.axis)), next.center);
    if (reflected) next.startAngle = -entity.startAngle;
  }
  if (reflected && entity.sweep != null) next.sweep = -entity.sweep;
  if (reflected && entity.bulges) next.bulges = entity.bulges.map(bulge => -bulge);
  return next;
}

function projectToSegment(point, a, b) {
  const vector = sub(b, a);
  const squared = dot(vector, vector);
  return squared ? mix(a, b, Math.max(0, Math.min(1, dot(sub(point, a), vector) / squared))) : a;
}

function within(point, box) {
  return point[0] >= box[0] && point[1] >= box[1] && point[0] <= box[2] && point[1] <= box[3];
}

function intersection(a, b, c, d) {
  const along = sub(b, a);
  const across = sub(d, c);
  const delta = sub(c, a);
  const determinant = cross(along, across);
  if (Math.abs(determinant) < EPSILON) return null;
  const t = cross(delta, across) / determinant;
  const u = cross(delta, along) / determinant;
  return t >= 0 && t <= 1 && u >= 0 && u <= 1 ? mix(a, b, t) : null;
}

function enclosed(path, box, crossing) {
  if (!path.length) return false;
  if (!crossing) return path.every(point => within(point, box));
  if (path.some(point => within(point, box))) return true;
  const [x, y, right, top] = box;
  const corners = [[x, y], [right, y], [right, top], [x, top]];
  return path.some((point, i) => i && corners.some((corner, k) =>
    intersection(path[i - 1], point, corner, corners[(k + 1) % 4])));
}

const CREATE = {
  line: { label: "Отрезок", points: ["Начальная точка", "Конечная точка"] },
  polyline: { label: "Полилиния", points: ["Начальная точка", "Следующая вершина"] },
  circle: { label: "Окружность", points: ["Центр окружности", "Точка на окружности"] },
  arc: { label: "Дуга", points: ["Начало дуги", "Промежуточная точка", "Конец дуги"] },
  spline: { label: "Сплайн", points: ["Начальная точка", "Следующая точка сплайна"] },
};
const EDIT = {
  move: { label: "Перенести", points: ["Базовая точка", "Новая точка"] },
  copy: { label: "Копировать", points: ["Базовая точка", "Точка вставки"] },
  rotate: { label: "Повернуть", points: ["Центр поворота"] },
  mirror: { label: "Отразить", points: ["Начало оси", "Конец оси"] },
  trim: { label: "Обрезать", points: ["Участок для удаления"] },
  notch: { label: "Надсечка", points: ["Точка на контуре"] },
};
const ENTITY_NAMES = { ...Object.fromEntries(Object.entries(CREATE).map(([key, value]) => [key, value.label])), ellipse: "Эллипс" };

function toolButtons(tools) {
  return Object.entries(tools).map(([key, { label }]) =>
    `<button type="button" class="button" data-tool="${key}" aria-pressed="false">${label}</button>`).join("");
}

const markup = `
  <div class="cad-header">
    <label class="cad-name"><span>Название чертежа</span><input data-field="name" maxlength="120"></label>
    <div class="cad-actions" role="group" aria-label="Чертёж">
      <button type="button" class="button" data-action="import">Импорт DXF / DWG</button>
      <input type="file" data-field="file" accept=".dxf,.dwg" hidden>
      <button type="button" class="button" data-action="save">Сохранить чертёж</button>
      <button type="button" class="button primary" data-action="part">Сохранить деталь</button>
    </div>
  </div>
  <div class="cad-tools">
    <div class="cad-tool-group" role="group" aria-label="Построение">
      <button type="button" class="button" data-tool="select" aria-pressed="true">Выбор</button>
      ${toolButtons(CREATE)}
    </div>
    <div class="cad-tool-group" role="group" aria-label="Правка">
      ${toolButtons(EDIT)}
      <button type="button" class="button" data-action="join">Соединить</button>
      <button type="button" class="button" data-tool="offset" aria-pressed="false">Припуск</button>
      <button type="button" class="button" data-action="delete">Удалить</button>
    </div>
    <div class="cad-tool-group" role="group" aria-label="История и вид">
      <button type="button" class="button" data-action="undo" aria-label="Отменить" title="Отменить (Ctrl+Z)">↶</button>
      <button type="button" class="button" data-action="redo" aria-label="Повторить" title="Повторить (Ctrl+Shift+Z)">↷</button>
      <button type="button" class="button" data-action="fit" title="Показать чертёж целиком">Вписать</button>
    </div>
  </div>
  <div class="cad-work">
    <div class="cad-view">
      <canvas tabindex="0" aria-label="Графическое поле чертежа"></canvas>
      <div class="cad-canvas-status" aria-hidden="true"><span data-view="scale"></span><span data-view="coords"></span></div>
    </div>
    <aside class="cad-inspector">
      <div class="cad-inspector-section">
        <h3 data-view="command">Выбор</h3>
        <p data-view="hint"></p>
        <form data-form="point" hidden>
          <div class="cad-field-pair">
            <label>X, мм<input data-field="x" inputmode="decimal" required></label>
            <label>Y, мм<input data-field="y" inputmode="decimal" required></label>
          </div>
          <button class="button" type="submit">Ввести точку</button>
        </form>
        <form data-form="option" hidden>
          <label><span data-view="option-label"></span><input data-field="option" inputmode="decimal" required></label>
          <button class="button" type="submit" data-view="option-button"></button>
        </form>
        <div class="cad-finish" data-view="finish" hidden>
          <button class="button" type="button" data-action="finish">Завершить</button>
          <button class="button" type="button" data-action="close">Замкнуть</button>
        </div>
        <div data-view="offset-preview" hidden>
          <label>Режущий контур
            <select data-field="cut">
              <option value="new">Контур с припуском</option>
              <option value="original">Исходный контур</option>
            </select>
          </label>
          <p data-view="preview-warning" hidden></p>
          <button class="button primary" type="button" data-action="apply">Применить</button>
          <button class="button" type="button" data-action="cancel-preview">Отмена</button>
        </div>
      </div>
      <div class="cad-inspector-section">
        <h3>Привязки</h3>
        <label class="cad-checkbox"><input type="checkbox" data-field="objects" checked> К точкам объектов</label>
        <label class="cad-checkbox"><input type="checkbox" data-field="crossings"> К пересечениям</label>
        <label class="cad-checkbox"><input type="checkbox" data-field="grid"> К сетке</label>
        <label class="cad-checkbox"><input type="checkbox" data-field="ortho"> Только по осям</label>
      </div>
      <div class="cad-inspector-section">
        <h3>Геометрия</h3>
        <p data-view="selection"></p>
        <div class="cad-legend">
          <span>Рез</span><span>Надсечка</span><span>Вспомогательный</span>
        </div>
        <button class="button" type="button" data-action="reference" hidden></button>
      </div>
      <details class="cad-inspector-section cad-warnings" data-view="warnings" hidden>
        <summary></summary><ul></ul>
      </details>
    </aside>
  </div>
  <div class="cad-message" data-view="message" role="status" hidden></div>
`;

function errorMessage(error) {
  return error?.message || String(error);
}

function angleTransform(center, degrees) {
  const cos = Math.cos(degrees * DEG);
  const sin = Math.sin(degrees * DEG);
  return point => {
    const [x, y] = sub(point, center);
    return add(center, [x * cos - y * sin, x * sin + y * cos]);
  };
}

function nearestSegment(point, path) {
  let nearest = null;
  let traversed = 0;
  for (let i = 1; i < path.length; i++) {
    const start = path[i - 1];
    const pointOnLine = projectToSegment(point, start, path[i]);
    const miss = distance(point, pointOnLine);
    const direction = sub(path[i], start);
    if (!nearest || miss < nearest.miss) {
      nearest = { miss, point: pointOnLine, direction, progress: traversed + distance(start, pointOnLine) };
    }
    traversed += norm(direction);
  }
  if (nearest) nearest.total = traversed;
  return nearest;
}

function atProgress(path, fraction) {
  const lengths = path.slice(1).map((point, i) => distance(path[i], point));
  let progress = fraction * lengths.reduce((total, length) => total + length, 0);
  for (let i = 0; i < lengths.length; i++) {
    const length = lengths[i];
    if (length && (progress <= length || i === lengths.length - 1)) {
      return { point: mix(path[i], path[i + 1], Math.min(progress / length, 1)), direction: sub(path[i + 1], path[i]) };
    }
    progress -= length;
  }
  return null;
}

function followGrip(notch, previousEntity, nextEntity, previousPath, nextPath) {
  if (notch.type !== "line") return notch;
  if (previousEntity.center && nextEntity.center !== previousEntity.center && nextEntity.radius === previousEntity.radius
      && nextEntity.startAngle === previousEntity.startAngle && nextEntity.sweep === previousEntity.sweep) {
    const translation = sub(nextEntity.center, previousEntity.center);
    return { ...notch, start: add(notch.start, translation), end: add(notch.end, translation) };
  }
  if (previousEntity.type === "circle") {
    const direction = angle(sub(notch.start, previousEntity.center));
    const start = add(nextEntity.center, radial(direction, nextEntity.radius));
    return { ...notch, start, end: add(start, sub(notch.end, notch.start)) };
  }
  const previous = nearestSegment(notch.start, previousPath);
  const next = previous?.total && atProgress(nextPath, previous.progress / previous.total);
  if (!next) return notch;
  const turn = angle(next.direction) - angle(previous.direction);
  const turned = angleTransform(notch.start, turn)(notch.end);
  return { ...notch, start: next.point, end: add(next.point, sub(turned, notch.start)) };
}

export function mountCadEditor(container, { document: draft, onSaveDocument, onSavePart, request }) {
  container.classList.add("cad-editor");
  container.innerHTML = markup;
  const root = container.ownerDocument;
  const canvas = container.querySelector("canvas");
  const context = canvas.getContext("2d");
  const fields = Object.fromEntries(Array.from(container.querySelectorAll("[data-field]"), node => [node.dataset.field, node]));
  const actions = Object.fromEntries(Array.from(container.querySelectorAll("[data-action]"), node => [node.dataset.action, node]));
  const views = Object.fromEntries(Array.from(container.querySelectorAll("[data-view]"), node => [node.dataset.view, node]));
  const buttons = Object.fromEntries(Array.from(container.querySelectorAll("[data-tool]"), node => [node.dataset.tool, node]));
  const pointForm = container.querySelector('[data-form="point"]');
  const optionForm = container.querySelector('[data-form="option"]');
  const lifecycle = new AbortController();
  const listener = { signal: lifecycle.signal };
  const previous = [];
  const future = [];
  const geometryCache = new WeakMap();
  const initial = { entities: draft?.entities || [], warnings: draft?.warnings || [] };
  let current = initial;
  let saved = initial;
  let savedName = draft?.name || "Новый чертёж";
  let selected = new Set();
  let tool = "select";
  let pending = [];
  let activeGrip = null;
  let offsetPreview = null;
  let cursor = null;
  let snap = null;
  let gesture = null;
  let spacePressed = false;
  let running = false;
  let fitOnSize = true;
  let center = [0, 0];
  let scale = 1;
  let width = 1;
  let height = 1;
  let pixelRatio = 1;

  fields.name.value = savedName;

  function geometry(entity) {
    let cached = geometryCache.get(entity);
    if (!cached) {
      const path = samples(entity);
      cached = { path, points: snapPoints(entity, path), bounds: curveBounds(entity, path) };
      geometryCache.set(entity, cached);
    }
    return cached;
  }

  function newId() {
    return root.defaultView.crypto.randomUUID?.() || "cad-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  function selectedEntities() {
    return current.entities.filter(entity => selected.has(entity.id));
  }

  function familyIds(ids) {
    const bases = new Set(ids);
    return new Set(current.entities.filter(entity => bases.has(entity.id) || bases.has(entity.parentId)).map(entity => entity.id));
  }

  function log(message = "", error = false) {
    views.message.textContent = message;
    views.message.hidden = !message;
    views.message.classList.toggle("cad-error", error);
  }

  function updateWarnings(warnings) {
    views.warnings.hidden = !warnings.length;
    views.warnings.querySelector("summary").textContent = "Предупреждения · " + warnings.length;
    const list = views.warnings.querySelector("ul");
    list.replaceChildren(...warnings.map(warning => {
      const item = root.createElement("li");
      item.textContent = warning?.message || String(warning);
      return item;
    }));
  }

  function update() {
    const items = selectedEntities();
    const count = items.length;
    const settings = count > 0;
    const contours = items.filter(entity => !entity.notch).length;
    const drawing = CREATE[tool];
    const edit = EDIT[tool];
    const canPoint = Boolean(drawing || (edit && settings) || activeGrip);
    const option = {
      offset: ["Смещение, мм", "Предпросмотр"],
      rotate: ["Угол, °", "Задать угол"],
      notch: ["Глубина, мм", "Задать глубину"],
    }[tool];
    const first = items[0];
    const instruction = (drawing || edit)?.points;
    const active = Boolean(gesture?.type === "grip" || offsetPreview);
    for (const [key, button] of Object.entries(buttons)) {
      button.setAttribute("aria-pressed", String(key === tool));
      button.disabled = running || active || (key in EDIT && !settings)
        || (key === "offset" && (count !== 1 || !contours))
        || (key === "notch" && !contours)
        || (key === "trim" && contours < 2);
    }
    actions.delete.disabled = running || active || !settings;
    actions.join.disabled = running || active || contours < 2;
    actions.undo.disabled = running || active || !previous.length;
    actions.redo.disabled = running || active || !future.length;
    actions.import.disabled = running || active;
    actions.save.disabled = running || active;
    actions.part.disabled = running || active || !items.some(entity => !entity.reference && !entity.notch);
    views.command.textContent = drawing?.label || edit?.label || (tool === "offset" ? "Припуск" : "Выбор");
    if (offsetPreview) views.hint.textContent = "Выберите режущий контур. Второй останется вспомогательным.";
    else if (tool === "offset") views.hint.textContent = "Плюс — наружу, минус — внутрь замкнутого контура.";
    else if (tool === "rotate") views.hint.textContent = "Введите угол и укажите центр поворота.";
    else if (tool === "notch") views.hint.textContent = "Введите глубину и укажите место на контуре.";
    else if (tool === "trim") views.hint.textContent = "Выберите лишний участок. Выделенные соседние линии ограничат обрезку.";
    else if (activeGrip) views.hint.textContent = "Введите координаты узла или перетащите его на чертеже.";
    else if (instruction) views.hint.textContent = instruction[Math.min(pending.length, instruction.length - 1)];
    else views.hint.textContent = "Щелчок или рамка выбирают объекты. Shift добавляет к выбору. Колесо меняет масштаб, средняя кнопка двигает поле.";
    pointForm.hidden = active || !canPoint;
    optionForm.hidden = active || !option;
    if (option) {
      views["option-label"].textContent = option[0];
      views["option-button"].textContent = option[1];
    }
    pointForm.querySelector("button").disabled = running;
    optionForm.querySelector("button").disabled = running;
    const finishable = tool === "spline" || tool === "polyline";
    views.finish.hidden = !finishable;
    actions.finish.disabled = pending.length < 2;
    actions.close.disabled = pending.length < 3;
    views["offset-preview"].hidden = !offsetPreview;
    views["preview-warning"].hidden = !offsetPreview?.warnings.length;
    views["preview-warning"].textContent = offsetPreview?.warnings.map(warning => warning?.message || String(warning)).join(" ") || "";
    views.selection.textContent = count === 1
      ? [ENTITY_NAMES[first.type] || first.type, first.layer, first.reference ? "вспомогательный" : ""].filter(Boolean).join(" · ")
      : count ? "Выбрано объектов: " + count : "Нет выбранных объектов";
    actions.reference.hidden = !first || tool !== "select";
    actions.reference.disabled = running || active;
    actions.reference.textContent = items.every(entity => entity.reference) ? "Назначить для реза" : "Сделать вспомогательным";
    draw();
  }

  function commit(entities, { warnings = current.warnings, ids = selected } = {}) {
    previous.push(current);
    if (previous.length > 50) previous.shift();
    future.length = 0;
    current = { entities, warnings };
    selected = new Set(ids);
    updateWarnings(warnings);
    update();
  }

  function choose(next) {
    tool = next;
    pending = [];
    activeGrip = null;
    cursor = snap = null;
    log();
    if (tool === "offset") fields.option.value = "5";
    if (tool === "rotate") fields.option.value = "90";
    if (tool === "notch") fields.option.value = "3";
    update();
    canvas.focus({ preventScroll: true });
  }

  function undo(from, to) {
    if (!from.length || running || gesture?.type === "grip" || offsetPreview) return;
    to.push(current);
    current = from.pop();
    selected.clear();
    tool = "select";
    pending = [];
    activeGrip = null;
    updateWarnings(current.warnings);
    log();
    update();
  }

  function screen([x, y]) {
    return [width / 2 + (x - center[0]) * scale, height / 2 - (y - center[1]) * scale];
  }

  function world([x, y]) {
    return [center[0] + (x - width / 2) / scale, center[1] - (y - height / 2) / scale];
  }

  function screenEvent(event) {
    const box = canvas.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  function gridStep() {
    const target = 25 / scale;
    const unit = 10 ** Math.floor(Math.log10(target));
    return unit * ([1, 2, 5, 10].find(interval => interval * unit >= target) || 10);
  }

  function pathLine(points) {
    if (!points.length) return;
    context.beginPath();
    context.moveTo(...screen(points[0]));
    for (const point of points.slice(1)) context.lineTo(...screen(point));
    context.stroke();
  }

  function drawGrid() {
    const step = gridStep();
    const topLeft = world([0, 0]);
    const bottomRight = world([width, height]);
    context.strokeStyle = "#e6eaee";
    context.lineWidth = 1;
    context.beginPath();
    for (let x = Math.ceil(topLeft[0] / step); x <= bottomRight[0] / step; x++) {
      const pixel = Math.round(screen([x * step, 0])[0]) + .5;
      context.moveTo(pixel, 0);
      context.lineTo(pixel, height);
    }
    for (let y = Math.ceil(bottomRight[1] / step); y <= topLeft[1] / step; y++) {
      const pixel = Math.round(screen([0, y * step])[1]) + .5;
      context.moveTo(0, pixel);
      context.lineTo(width, pixel);
    }
    context.stroke();
    const [originX, originY] = screen([0, 0]).map(coordinate => Math.round(coordinate) + .5);
    context.strokeStyle = "#b3c0ce";
    context.beginPath();
    if (originX >= 0 && originX <= width) {
      context.moveTo(originX, 0);
      context.lineTo(originX, height);
    }
    if (originY >= 0 && originY <= height) {
      context.moveTo(0, originY);
      context.lineTo(width, originY);
    }
    context.stroke();
    context.fillStyle = "#60727e";
    context.font = "11px Inter, Segoe UI, sans-serif";
    if (originX >= 0 && originX <= width && originY >= 0 && originY <= height) {
      context.fillText("0", originX + 5, originY + 14);
    }
  }

  function drawEntity(entity, highlighted = false, preview = false) {
    const { path, bounds } = geometry(entity);
    const [minX, maxY] = world([-10, -10]);
    const [maxX, minY] = world([width + 10, height + 10]);
    if (bounds[2] < minX || bounds[0] > maxX || bounds[3] < minY || bounds[1] > maxY) return;
    context.strokeStyle = entity.reference
      ? highlighted ? "#1745ae" : "#7d8b97"
      : entity.notch ? "#bb531a" : highlighted ? "#1745ae" : preview ? "#167452" : "#292e34";
    context.lineWidth = highlighted || preview ? 2.25 : 1.5;
    context.setLineDash(entity.reference ? [5, 4] : []);
    if (entity.type === "circle" || entity.type === "arc") {
      context.beginPath();
      const start = entity.type === "arc" ? entity.startAngle : 0;
      const sweep = entity.type === "arc" ? entity.sweep : 360;
      context.arc(...screen(entity.center), entity.radius * scale, -start * DEG, -(start + sweep) * DEG, sweep > 0);
      context.stroke();
    } else if (entity.type === "ellipse") {
      context.beginPath();
      const start = entity.startAngle;
      const sweep = entity.sweep;
      const radius = norm(entity.axis) * scale;
      context.ellipse(...screen(entity.center), radius, radius * entity.ratio, -angle(entity.axis) * DEG,
        -start * DEG, -(start + sweep) * DEG, sweep > 0);
      context.stroke();
    } else {
      pathLine(path);
    }
    context.setLineDash([]);
  }

  function draw() {
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);
    drawGrid();
    const shown = offsetPreview ? previewEntities() : gesture?.entities || current.entities;
    for (const entity of shown) {
      if (!offsetPreview && selected.has(entity.id)) continue;
      const activePreview = offsetPreview && [offsetPreview.original, ...offsetPreview.ids].includes(entity.parentId || entity.id);
      drawEntity(entity, false, Boolean(activePreview));
    }
    for (const entity of shown) {
      if (offsetPreview || !selected.has(entity.id)) continue;
      drawEntity(entity, true);
      if (tool !== "select" || selected.size !== 1) continue;
      for (const [index, grip] of controls(entity).entries()) {
        const [x, y] = screen(grip);
        context.fillStyle = activeGrip?.id === entity.id && activeGrip.index === index ? "#1745ae" : "#fff";
        context.strokeStyle = "#1745ae";
        context.lineWidth = 1.5;
        context.fillRect(x - 3, y - 3, 6, 6);
        context.strokeRect(x - 3, y - 3, 6, 6);
      }
    }
    if (pending.length && cursor && CREATE[tool]) {
      const points = [...pending, cursor];
      const provisional = creation(points, false, true);
      context.strokeStyle = "#397dba";
      context.lineWidth = 1.5;
      context.setLineDash([5, 4]);
      pathLine(provisional ? samples(provisional) : points);
      context.setLineDash([]);
    } else if (pending.length && cursor && EDIT[tool]) {
      context.strokeStyle = "#397dba";
      context.lineWidth = 1;
      context.setLineDash([5, 4]);
      pathLine([pending[0], cursor]);
      context.setLineDash([]);
    }
    if (snap && (tool !== "select" || gesture?.type === "grip")) {
      const [x, y] = screen(snap);
      context.strokeStyle = "#167452";
      context.lineWidth = 1.5;
      context.strokeRect(x - 5, y - 5, 10, 10);
    }
    if (gesture?.type === "marquee") {
      const [startX, startY] = gesture.start;
      const [endX, endY] = gesture.end;
      context.fillStyle = endX >= startX ? "#1745ae15" : "#1674521a";
      context.strokeStyle = endX >= startX ? "#1745ae" : "#167452";
      context.lineWidth = 1;
      context.fillRect(startX, startY, endX - startX, endY - startY);
      context.strokeRect(startX, startY, endX - startX, endY - startY);
    }
    views.scale.textContent = "Сетка " + format(gridStep()) + " мм";
    views.coords.textContent = cursor ? "X " + format(cursor[0]) + "  Y " + format(cursor[1]) + " мм" : "мм";
  }

  function fit(entities = current.entities) {
    if (!entities.length) {
      center = [0, 0];
      scale = 1;
    } else {
      const box = [Infinity, Infinity, -Infinity, -Infinity];
      for (const entity of entities) {
        const bounds = geometry(entity).bounds;
        box[0] = Math.min(box[0], bounds[0]);
        box[1] = Math.min(box[1], bounds[1]);
        box[2] = Math.max(box[2], bounds[2]);
        box[3] = Math.max(box[3], bounds[3]);
      }
      if (!box.every(Number.isFinite)) return;
      center = mix(box.slice(0, 2), box.slice(2), .5);
      scale = Math.max(.00001, Math.min(250, (width - 56) / Math.max(1, box[2] - box[0]), (height - 56) / Math.max(1, box[3] - box[1])));
    }
    draw();
  }

  function hit(point, accepted = () => true) {
    let miss = 8 / scale;
    let found = null;
    for (const entity of current.entities) {
      if (!accepted(entity)) continue;
      const { path, bounds } = geometry(entity);
      const [x, y] = point;
      if (x < bounds[0] - miss || x > bounds[2] + miss || y < bounds[1] - miss || y > bounds[3] + miss) continue;
      let candidate;
      if (entity.type === "circle" || entity.type === "arc") {
        const vector = sub(point, entity.center);
        const direction = angle(vector);
        const progress = Math.sign(entity.sweep) * (direction - entity.startAngle);
        if (entity.type === "circle" || positiveAngle(progress) <= Math.abs(entity.sweep)) {
          const nearest = add(entity.center, radial(direction, entity.radius));
          candidate = { miss: distance(point, nearest), point: nearest };
        } else {
          candidate = nearestSegment(point, path);
        }
      } else {
        candidate = nearestSegment(point, path);
      }
      if (candidate && candidate.miss <= miss) {
        miss = candidate.miss;
        found = { entity, point: candidate.point };
      }
    }
    return found;
  }

  function position(raw, ignoredId) {
    snap = null;
    const tolerance = 10 / scale;
    let best = tolerance;
    const consider = candidate => {
      const gap = distance(raw, candidate);
      if (gap < best) {
        best = gap;
        snap = candidate;
      }
    };
    if (fields.objects.checked) {
      for (const entity of current.entities) {
        if (entity.id === ignoredId) continue;
        const { points } = geometry(entity);
        for (const candidate of points) consider(candidate);
      }
      for (const candidate of [...pending, [0, 0]]) consider(candidate);
    }
    if (fields.crossings.checked) {
      const [x, y] = raw;
      const box = [x - tolerance, y - tolerance, x + tolerance, y + tolerance];
      const nearby = [];
      for (const entity of current.entities) {
        if (entity.id === ignoredId) continue;
        const { path, bounds } = geometry(entity);
        if (bounds[0] > box[2] || bounds[1] > box[3] || bounds[2] < box[0] || bounds[3] < box[1]) continue;
        for (let i = 1; i < path.length; i++) {
          const a = path[i - 1];
          const b = path[i];
          if (distance(projectToSegment(raw, a, b), raw) >= tolerance) continue;
          for (const other of nearby) {
            if (other.id === entity.id && i - other.index < 2) continue;
            const point = intersection(a, b, other.a, other.b);
            if (point) consider(point);
          }
          if (nearby.length < 128) nearby.push({ id: entity.id, index: i, a, b });
        }
      }
    }
    if (snap) return snap;
    let point = raw;
    if (fields.grid.checked) {
      const step = gridStep();
      point = point.map(value => Math.round(value / step) * step);
    }
    const anchor = pending.at(-1);
    if (anchor && fields.ortho.checked) {
      const [x, y] = sub(point, anchor);
      point = Math.abs(x) > Math.abs(y) ? [point[0], anchor[1]] : [anchor[0], point[1]];
    }
    return point;
  }

  function creation(points, closed, provisional = false) {
    const [start, middle, end] = points;
    const common = { type: tool, layer: "0" };
    switch (tool) {
      case "line":
        return middle && distance(start, middle) > EPSILON ? { ...common, start, end: middle } : null;
      case "circle":
        return middle && distance(start, middle) > EPSILON ? { ...common, center: start, radius: distance(start, middle) } : null;
      case "arc": {
        const arc = end && arcThrough(start, middle, end);
        return arc && { ...common, ...arc };
      }
      case "polyline":
      case "spline":
        if (points.length < (closed ? 3 : 2)) return null;
        return { ...common, [tool === "polyline" ? "points" : "fitPoints"]: points, closed };
      default:
        if (provisional) return null;
        throw new Error("Неизвестный инструмент построения.");
    }
  }

  function build(points, closed = false) {
    const entity = creation(points, closed);
    if (!entity) {
      log(tool === "arc" ? "Укажите три разные точки, лежащие вне одной прямой." : "Укажите разные точки.", true);
      return;
    }
    entity.id = newId();
    pending = [];
    snap = cursor = null;
    commit([...current.entities, entity], { ids: [entity.id] });
  }

  function operate(point) {
    if (running || offsetPreview || !point.every(Number.isFinite)) return;
    log();
    if (point.some(value => Math.abs(value) > MAX_COORDINATE)) {
      log("Координаты должны быть в пределах ±1 000 000 000 мм.", true);
      return;
    }
    if (tool === "select" && activeGrip) {
      const entities = changeGrip(activeGrip, point);
      if (!entities) {
        log("В этой точке объект вырождается.", true);
        return;
      }
      commit(entities);
      return;
    }
    if (tool in CREATE) {
      if (pending.length && distance(point, pending.at(-1)) < EPSILON) {
        log("Эта точка совпадает с предыдущей.", true);
        return;
      }
      const multiPoint = tool === "polyline" || tool === "spline";
      if (multiPoint && pending.length >= 3 && distance(point, pending[0]) < EPSILON) {
        build(pending, true);
        return;
      }
      const next = [...pending, point];
      if (!multiPoint && next.length === CREATE[tool].points.length) build(next);
      else {
        pending = next;
        update();
      }
      return;
    }
    if (tool === "trim" || tool === "notch") {
      const target = hit(point, entity => selected.has(entity.id) && !entity.notch);
      if (!target) {
        log("Укажите точку на выбранной геометрии.", true);
        return;
      }
      if (tool === "trim") {
        const cutterIds = selectedEntities().filter(entity => !entity.notch && entity.id !== target.entity.id).map(entity => entity.id);
        void editOnServer("trim", { id: target.entity.id, point: target.point, cutterIds });
      } else {
        const depth = numberFrom(fields.option);
        if (!(depth > 0) || !Number.isFinite(depth)) {
          log("Введите положительную глубину надсечки.", true);
          return;
        }
        void editOnServer("notch", { id: target.entity.id, point: target.point, depth });
      }
      return;
    }
    if (!(tool in EDIT)) return;
    if (tool !== "rotate" && !pending.length) {
      pending = [point];
      update();
      return;
    }
    let pointTransform;
    if (tool === "rotate") {
      const degrees = numberFrom(fields.option);
      if (!Number.isFinite(degrees)) {
        log("Введите угол поворота в градусах.", true);
        return;
      }
      pointTransform = angleTransform(point, degrees);
    } else {
      const base = pending[0];
      const delta = sub(point, base);
      const length = norm(delta);
      if (length < EPSILON) {
        log("Точки должны различаться.", true);
        return;
      }
      if (tool === "mirror") {
        const direction = mul(delta, 1 / length);
        pointTransform = p => {
          const vector = sub(p, base);
          return add(base, sub(mul(direction, 2 * dot(vector, direction)), vector));
        };
      } else {
        pointTransform = p => add(p, delta);
      }
    }
    const ids = familyIds(selected);
    const byId = new Map(current.entities.map(entity => [entity.id, entity]));
    const newIds = tool === "copy" ? new Map(Array.from(ids, id => [id, newId()])) : null;
    const changed = Array.from(ids, id => {
      const entity = byId.get(id);
      const result = transform(entity, pointTransform, tool === "mirror");
      if (newIds) {
        result.id = newIds.get(id);
        if (result.parentId) result.parentId = newIds.get(result.parentId);
      } else if (result.parentId && !ids.has(result.parentId)) {
        delete result.parentId;
      }
      return result;
    });
    const changedById = new Map(changed.map(entity => [entity.id, entity]));
    const entities = newIds
      ? [...current.entities, ...changed]
      : current.entities.map(entity => changedById.get(entity.id) || entity);
    const selection = newIds ? Array.from(selected, id => newIds.get(id)) : selected;
    choose("select");
    commit(entities, { ids: selection });
  }

  async function perform(task) {
    if (running || lifecycle.signal.aborted) return;
    running = true;
    log();
    update();
    try {
      await task();
    } catch (error) {
      if (!lifecycle.signal.aborted) log(errorMessage(error), true);
    } finally {
      running = false;
      if (!lifecycle.signal.aborted) update();
    }
  }

  function editOnServer(operation, options) {
    return perform(async () => {
      const result = await request("/api/cad/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entities: current.entities, operation, options }),
        signal: lifecycle.signal,
      });
      if (lifecycle.signal.aborted) return;
      const { entities, warnings = [], selectedIds = [] } = result;
      if (operation === "offset") {
        offsetPreview = { entities, original: options.id, ids: selectedIds, warnings };
        fields.cut.value = "new";
      } else {
        choose("select");
        commit(entities, { warnings: [...current.warnings, ...warnings], ids: selectedIds });
      }
    });
  }

  function previewEntities() {
    const { entities, original, ids } = offsetPreview;
    const originalCuts = fields.cut.value === "original";
    return entities.map(entity => {
      if (entity.id === original || entity.parentId === original) return { ...entity, reference: !originalCuts };
      if (ids.includes(entity.id) || ids.includes(entity.parentId)) return { ...entity, reference: originalCuts };
      return entity;
    });
  }

  function applyOffset() {
    if (!offsetPreview) return;
    const entities = previewEntities();
    const warnings = [...current.warnings, ...offsetPreview.warnings];
    const ids = fields.cut.value === "original" ? [offsetPreview.original] : offsetPreview.ids;
    offsetPreview = null;
    choose("select");
    commit(entities, { warnings, ids });
  }

  function setReference() {
    const items = selectedEntities();
    if (!items.length) return;
    const reference = !items.every(entity => entity.reference);
    const ids = familyIds(selected);
    commit(current.entities.map(entity => ids.has(entity.id) ? { ...entity, reference } : entity));
  }

  function removeSelected() {
    const ids = familyIds(selected);
    if (!ids.size || running || offsetPreview) return;
    choose("select");
    commit(current.entities.filter(entity => !ids.has(entity.id)), { ids: [] });
  }

  function changeGrip({ id, index }, point) {
    const entity = current.entities.find(item => item.id === id);
    const updated = setGrip(entity, index, point);
    if (updated === entity || point.some(value => Math.abs(value) > MAX_COORDINATE)) return null;
    const oldPath = geometry(entity).path;
    const newPath = geometry(updated).path;
    return current.entities.map(item => {
      if (item.id === id) return updated;
      if (item.parentId === id) return followGrip(item, entity, updated, oldPath, newPath);
      return item;
    });
  }

  function updateGrip(raw) {
    gesture.entities = changeGrip(gesture, position(raw, gesture.id));
  }

  function focusGrip({ id, index }, entities = current.entities) {
    activeGrip = { id, index };
    const entity = entities.find(item => item.id === id);
    const point = controls(entity)[index];
    fields.x.value = format(point[0]);
    fields.y.value = format(point[1]);
  }

  function onPointerDown(event) {
    if (!event.isPrimary && event.pointerType !== "mouse") return;
    if (gesture || ![0, 1, 2].includes(event.button)) return;
    canvas.focus({ preventScroll: true });
    const start = screenEvent(event);
    if (event.button || spacePressed) {
      event.preventDefault();
      gesture = { type: "pan", start, center, pointerId: event.pointerId };
    } else if (!running && !offsetPreview) {
      const point = world(start);
      if (tool !== "select") {
        operate(tool === "trim" || tool === "notch" ? point : position(point));
        return;
      }
      const selection = selected.size === 1 ? selectedEntities()[0] : null;
      const index = selection && controls(selection).findIndex(p => distance(screen(p), start) <= 7);
      if (!event.shiftKey && selection && index >= 0) {
        gesture = { type: "grip", id: selection.id, index, start, pointerId: event.pointerId };
      } else {
        gesture = { type: "marquee", start, end: start, shift: event.shiftKey, pointerId: event.pointerId };
      }
    }
    if (gesture) {
      canvas.setPointerCapture(event.pointerId);
      canvas.dataset.gesture = gesture.type;
    }
  }

  function onPointerMove(event) {
    if (gesture && gesture.pointerId !== event.pointerId) return;
    const at = screenEvent(event);
    const point = world(at);
    if (gesture?.type === "pan") {
      const [x, y] = sub(at, gesture.start);
      center = add(gesture.center, [-x / scale, y / scale]);
    } else if (gesture?.type === "marquee") {
      gesture.end = at;
    } else if (gesture?.type === "grip") {
      updateGrip(point);
    }
    const location = world(at);
    cursor = tool === "select" || tool === "trim" || tool === "notch" ? location : position(location);
    draw();
  }

  function completeGesture(event, cancelled = false) {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const completed = gesture;
    const end = screenEvent(event);
    const dragged = distance(completed.start, end) > 3;
    if (!cancelled && completed.type === "grip" && dragged) updateGrip(world(end));
    gesture = null;
    delete canvas.dataset.gesture;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (!cancelled && completed.type === "grip") {
      if (dragged && completed.entities) {
        focusGrip(completed, completed.entities);
        commit(completed.entities);
      } else {
        focusGrip(completed);
      }
    }
    if (!cancelled && completed.type === "marquee") {
      activeGrip = null;
      if (!completed.shift) selected.clear();
      if (dragged) {
        const box = geometryBounds([world(completed.start), world(end)]);
        const crossing = end[0] < completed.start[0];
        for (const entity of current.entities) {
          if (enclosed(geometry(entity).path, box, crossing)) selected.add(entity.id);
        }
      } else {
        const item = hit(world(end));
        if (item && completed.shift && selected.has(item.entity.id)) selected.delete(item.entity.id);
        else if (item) selected.add(item.entity.id);
      }
    }
    update();
  }

  const actionHandlers = {
    import: () => fields.file.click(),
    save: () => perform(async () => {
      const name = fields.name.value.trim();
      if (!name) throw new Error("Введите название чертежа.");
      await onSaveDocument({ name, ...current });
      if (lifecycle.signal.aborted) return;
      saved = current;
      savedName = name;
      fields.name.value = name;
      log("Чертёж сохранён.");
    }),
    part: () => perform(async () => {
      const included = familyIds(selected);
      const entities = current.entities.filter(entity => included.has(entity.id)).map(entity => {
        if (!entity.parentId || included.has(entity.parentId)) return entity;
        const { parentId, ...unbound } = entity;
        return unbound;
      });
      const result = await onSavePart({ entities });
      if (lifecycle.signal.aborted || result === false) return;
      const warnings = result?.warnings || [];
      updateWarnings([...current.warnings, ...warnings]);
      log("Деталь сохранена в реестре.");
    }),
    fit: () => fit(offsetPreview ? previewEntities() : current.entities),
    delete: removeSelected,
    undo: () => undo(previous, future),
    redo: () => undo(future, previous),
    join: () => editOnServer("join", { ids: selectedEntities().filter(entity => !entity.notch).map(entity => entity.id) }),
    finish: () => build(pending),
    close: () => build(pending, true),
    reference: setReference,
    apply: applyOffset,
    "cancel-preview": () => {
      offsetPreview = null;
      update();
    },
  };

  for (const [key, button] of Object.entries(actions)) {
    button.addEventListener("click", actionHandlers[key], listener);
  }
  for (const [key, button] of Object.entries(buttons)) {
    button.addEventListener("click", () => choose(key), listener);
  }
  fields.file.addEventListener("change", () => {
    const file = fields.file.files[0];
    fields.file.value = "";
    if (!file) return;
    if (current.entities.length && !root.defaultView.confirm("Заменить геометрию текущего чертежа импортированным файлом?")) return;
    void perform(async () => {
      const result = await request("/api/cad/import?filename=" + encodeURIComponent(file.name), {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
        signal: lifecycle.signal,
      });
      if (lifecycle.signal.aborted) return;
      const imported = result.document || result;
      const { entities, warnings = [] } = imported;
      fields.name.value = imported.name || file.name.replace(/\.[^.]+$/, "");
      choose("select");
      commit(entities, { warnings, ids: [] });
      fit();
      log("Импортировано объектов: " + entities.length);
    });
  }, listener);
  fields.cut.addEventListener("change", draw, listener);
  pointForm.addEventListener("submit", event => {
    event.preventDefault();
    const point = [numberFrom(fields.x), numberFrom(fields.y)];
    if (!point.every(Number.isFinite)) {
      log("Введите числовые координаты X и Y в миллиметрах.", true);
      return;
    }
    operate(point);
    fields.x.select();
  }, listener);
  optionForm.addEventListener("submit", event => {
    event.preventDefault();
    if (tool !== "offset") {
      canvas.focus();
      return;
    }
    const value = numberFrom(fields.option);
    if (!Number.isFinite(value) || Math.abs(value) < EPSILON) {
      log("Введите ненулевое смещение в миллиметрах.", true);
      return;
    }
    if (selected.size === 1) void editOnServer("offset", { id: selected.values().next().value, distance: value, keepOriginal: true });
  }, listener);
  for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"]) {
    const action = {
      pointerdown: onPointerDown,
      pointermove: onPointerMove,
      pointerup: event => completeGesture(event),
      pointercancel: event => completeGesture(event, true),
      lostpointercapture: event => completeGesture(event, true),
    }[type];
    canvas.addEventListener(type, action, listener);
  }
  canvas.addEventListener("pointerleave", () => {
    cursor = snap = null;
    draw();
  }, listener);
  canvas.addEventListener("contextmenu", event => event.preventDefault(), listener);
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    if (gesture) return;
    const at = screenEvent(event);
    const anchor = world(at);
    const delta = event.deltaY * (event.deltaMode === 1 ? 15 : 1);
    scale = Math.min(250, Math.max(.00001, scale * Math.exp(-delta * .0015)));
    center = add(center, sub(anchor, world(at)));
    draw();
  }, { ...listener, passive: false });
  canvas.addEventListener("blur", () => { spacePressed = false; }, listener);
  canvas.addEventListener("keyup", event => {
    if (event.code === "Space") spacePressed = false;
  }, listener);
  container.addEventListener("keydown", event => {
    const key = event.key.toLowerCase();
    if (key === "escape") {
      event.preventDefault();
      event.stopPropagation();
      if (running) return;
      offsetPreview = null;
      choose("select");
      return;
    }
    if (event.target.matches("input, select")) return;
    if ((event.ctrlKey || event.metaKey) && (key === "z" || key === "y")) {
      event.preventDefault();
      event.stopPropagation();
      if (key === "y" || event.shiftKey) undo(future, previous);
      else undo(previous, future);
    } else if (event.target === canvas && event.code === "Space") {
      event.preventDefault();
      spacePressed = true;
    } else if (event.target === canvas && key === "delete") {
      event.preventDefault();
      removeSelected();
    } else if (event.target === canvas && key === "enter" && !actions.finish.disabled && !views.finish.hidden) {
      event.preventDefault();
      build(pending);
    }
  }, listener);

  const observer = new ResizeObserver(([entry]) => {
    const box = entry.contentRect;
    if (!box.width || !box.height) return;
    width = box.width;
    height = box.height;
    pixelRatio = root.defaultView.devicePixelRatio || 1;
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    if (fitOnSize) {
      fitOnSize = false;
      fit();
    } else draw();
  });
  observer.observe(canvas);
  updateWarnings(current.warnings);
  update();
  return {
    hasUnsavedChanges: () => saved !== current || savedName !== fields.name.value.trim(),
    destroy() {
      lifecycle.abort();
      observer.disconnect();
      container.classList.remove("cad-editor");
      container.replaceChildren();
    },
  };
}
