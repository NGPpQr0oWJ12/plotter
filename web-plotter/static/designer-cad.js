import { mountCadShortcuts } from "/designer-shortcuts.js";
import { confirmAction } from "/confirm-action.js";

const EPSILON = 1e-8;
const CAD_POINT_EPSILON = 0.001;
const MAX_COORDINATE = 1e9;
const CAD_SELECTED_COLOR = "#1688de";
const CAD_NOTCH_COLOR = "#e45a08";
const LEGACY_DEFAULT_UNITS_WARNING = "В чертеже не заданы единицы. Одна единица принята за 1 мм.";
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
    const result = [points[0]];
    for (let i = 0; i < count - Number(!entity.closed); i++) {
      const a = at(i - 1);
      const b = at(i);
      const c = at(i + 1);
      const d = at(i + 2);
      const chord = sub(c, b);
      let start = mul(sub(c, a), .5);
      let end = mul(sub(d, b), .5);
      if (entity.fitMethod === "centripetal") {
        const previous = Math.sqrt(distance(a, b));
        const middle = Math.sqrt(norm(chord));
        const following = Math.sqrt(distance(c, d));
        if (middle <= EPSILON) continue;
        start = previous ? add(sub(mul(sub(b, a), middle / previous), mul(sub(c, a), middle / (previous + middle))), chord) : chord;
        end = following ? add(sub(chord, mul(sub(d, b), middle / (middle + following))), mul(sub(d, c), middle / following)) : chord;
      }
      const quadratic = sub(mul(chord, 3), add(mul(start, 2), end));
      const cubic = sub(add(start, end), mul(chord, 2));
      const evaluate = t => add(b, mul(add(start, mul(add(quadratic, mul(cubic, t)), t)), t));
      sampleSplineSpan(result, evaluate, 0, 1, b, c);
    }
    return result;
  }
  const { controlPoints: points = [], knots = [], weights = [], degree } = entity;
  const count = points.length;
  if (!degree || count <= degree || knots.length !== count + degree + 1) return points;
  const evaluate = (span, at) => {
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
    return [x / weight, y / weight];
  };
  const result = [];
  for (let span = degree; span < count; span++) {
    const left = knots[span];
    const right = knots[span + 1];
    if (right <= left) continue;
    const at = value => evaluate(span, value);
    const start = result.at(-1) || at(left);
    if (!result.length) result.push(start);
    sampleSplineSpan(result, at, left, right, start, at(right));
  }
  if (entity.closed && result.length && distance(result[0], result.at(-1)) > EPSILON) result.push(result[0]);
  return result;
}

function sampleSplineSpan(result, evaluate, left, right, start, end, depth = 0) {
  const step = (right - left) / 4;
  const samples = [evaluate(left + step), evaluate(left + 2 * step), evaluate(right - step)];
  if (depth < 12 && samples.some(point => distance(point, projectToSegment(point, start, end)) > .05)) {
    const middle = (left + right) / 2;
    sampleSplineSpan(result, evaluate, left, middle, start, samples[1], depth + 1);
    sampleSplineSpan(result, evaluate, middle, right, samples[1], end, depth + 1);
  } else result.push(end);
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
    case "point": return [entity.point];
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
  if (entity.type === "polyline" && entity.bulges) {
    const bounds = geometryBounds(path);
    for (let index = 0; index < entity.points.length - Number(!entity.closed); index++) {
      const { arc } = polylineEdge(entity, index);
      if (!arc) continue;
      const limits = curveBounds({ type: "arc", ...arc }, [arcPoint(arc, 0), arcPoint(arc, 1)]);
      bounds[0] = Math.min(bounds[0], limits[0]);
      bounds[1] = Math.min(bounds[1], limits[1]);
      bounds[2] = Math.max(bounds[2], limits[2]);
      bounds[3] = Math.max(bounds[3], limits[3]);
    }
    return bounds;
  }
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

function polylineEdge(entity, index) {
  const start = entity.points[index];
  const end = entity.points[(index + 1) % entity.points.length];
  const bulge = entity.bulges?.[index];
  return { start, end, arc: bulge ? bulgeArc(start, end, bulge) : null };
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
    case "point": return [entity.point];
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

function pathMidpoint(points, start = 0, end = points.length - 1) {
  if (end <= start) return null;
  let length = 0;
  for (let index = start; index < end; index++) length += distance(points[index], points[index + 1]);
  if (length <= EPSILON) return null;
  let remaining = length / 2;
  for (let index = start; index < end; index++) {
    const segment = distance(points[index], points[index + 1]);
    if (segment > 0 && remaining <= segment) return mix(points[index], points[index + 1], remaining / segment);
    remaining -= segment;
  }
  return points[end];
}

function ellipseArcMidpoint(entity) {
  const range = Math.abs(entity.sweep);
  const start = entity.startAngle;
  const speed = degrees => Math.hypot(Math.sin(degrees * DEG), entity.ratio * Math.cos(degrees * DEG));
  const count = Math.max(32, Math.ceil(range * 4));
  const step = entity.sweep / count;
  const lengths = [0];
  for (let index = 0; index < count; index++) {
    const angle = start + index * step;
    const area = (speed(angle) + 4 * speed(angle + step / 2) + speed(angle + step)) / 6;
    lengths.push(lengths[index] + area);
  }
  const target = lengths.at(-1) / 2;
  const index = lengths.findIndex(length => length >= target);
  const fraction = (target - lengths[index - 1]) / (lengths[index] - lengths[index - 1]);
  return ellipsePoint(entity, (index - 1 + fraction) / count);
}

function snapPoints(entity, path) {
  const nodes = (points, type = "Узел") => points.filter(Boolean).map(point => ({ point, type }));
  const midpoints = points => nodes(points, "Середина");
  const quadrants = points => nodes(points, "Квадрант");
  switch (entity.type) {
    case "line": return [...nodes(controls(entity)), ...midpoints([mix(entity.start, entity.end, .5)])];
    case "circle": return [...quadrants([0, 90, 180, 270].map(a => add(entity.center, radial(a, entity.radius)))),
      ...nodes([entity.center])];
    case "arc": {
      const span = Math.abs(entity.sweep);
      const axial = [0, 90, 180, 270].map(degrees => positiveAngle(Math.sign(entity.sweep) * (degrees - entity.startAngle)))
        .filter(progress => progress > EPSILON && progress < span - EPSILON);
      return [...midpoints([arcPoint(entity, .5)]), ...quadrants(axial.map(progress => arcPoint(entity, progress / span))),
        ...nodes([arcPoint(entity, 0), arcPoint(entity, 1), entity.center])];
    }
    case "polyline": {
      const points = nodes(entity.points);
      for (let i = 0; i < entity.points.length - Number(!entity.closed); i++) {
        const { start, end, arc } = polylineEdge(entity, i);
        if (distance(start, end) > EPSILON) points.push({ point: arc ? arcPoint(arc, .5) : mix(start, end, .5), type: "Середина" });
      }
      return points;
    }
    case "spline": {
      const middles = [pathMidpoint(path)];
      if (entity.fitPoints?.length) {
        let start = 0;
        // The adaptive sampler ends each fit span at the original fit-point object.
        for (const point of [...entity.fitPoints.slice(1), ...(entity.closed ? entity.fitPoints.slice(0, 1) : [])]) {
          const end = path.indexOf(point, start + 1);
          if (end === -1) continue;
          middles.push(pathMidpoint(path, start, end));
          start = end;
        }
      }
      return [...nodes(entity.fitPoints ?? [path[0], path.at(-1)]), ...midpoints(middles)];
    }
    case "ellipse": {
      const span = Math.abs(entity.sweep);
      const [a, b] = entity.axis;
      const xExtreme = angle([a, -b * entity.ratio]);
      const yExtreme = angle([b, a * entity.ratio]);
      const axes = [0, 90, 180, 270, xExtreme, xExtreme + 180, yExtreme, yExtreme + 180];
      const candidates = axes.map(degrees => positiveAngle(Math.sign(entity.sweep) * (degrees - entity.startAngle)))
        .filter(progress => progress <= span + EPSILON).map(progress => ellipsePoint(entity, progress / span));
      if (span < 360 - EPSILON) {
        return [...midpoints([ellipseArcMidpoint(entity), ellipsePoint(entity, .5)]), ...quadrants(candidates),
          ...nodes([ellipsePoint(entity, 0), ellipsePoint(entity, 1), entity.center])];
      }
      return [...midpoints(candidates), ...nodes([entity.center])];
    }
    default: return nodes(controls(entity));
  }
}

function setGrip(entity, index, point) {
  switch (entity.type) {
    case "point": return { ...entity, point };
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
  for (const field of ["start", "end", "center", "point"]) if (entity[field]) next[field] = point(entity[field]);
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

function snapConic(entity) {
  const ellipse = entity.type === "ellipse";
  const u = ellipse ? entity.axis : [entity.radius, 0];
  const v = mul([-u[1], u[0]], ellipse ? entity.ratio : 1);
  const local = point => {
    const offset = sub(point, entity.center);
    return [dot(offset, u) / dot(u, u), dot(offset, v) / dot(v, v)];
  };
  const includes = point => {
    if (entity.type === "circle") return true;
    const span = Math.abs(entity.sweep);
    const progress = positiveAngle(Math.sign(entity.sweep) * (angle(local(point)) - entity.startAngle));
    return progress <= span + EPSILON || progress >= 360 - EPSILON;
  };
  return { entity, local, includes, circle: !ellipse };
}

function snapCrossings(first, second) {
  if (!first.curve && !second.curve) {
    const point = intersection(first.a, first.b, second.a, second.b);
    return point ? [point] : [];
  }
  if (first.curve && second.curve) {
    if (!first.curve.circle || !second.curve.circle) return [];
    const { center: a, radius: r } = first.curve.entity;
    const { center: b, radius: q } = second.curve.entity;
    const between = sub(b, a);
    const span = norm(between);
    if (span < EPSILON || span > r + q + EPSILON || span < Math.abs(r - q) - EPSILON) return [];
    const along = (span ** 2 + r ** 2 - q ** 2) / (2 * span);
    const foot = add(a, mul(between, along / span));
    const offset = Math.sqrt(Math.max(0, r ** 2 - along ** 2));
    const normal = mul([-between[1], between[0]], offset / span);
    return [add(foot, normal), sub(foot, normal)].filter(point => first.curve.includes(point) && second.curve.includes(point));
  }
  const { a, b } = first.curve ? second : first;
  const curve = first.curve || second.curve;
  const start = curve.local(a);
  const vector = sub(curve.local(b), start);
  const squared = dot(vector, vector);
  if (!squared) return [];
  const fraction = -dot(start, vector) / squared;
  const closest = add(start, mul(vector, fraction));
  const difference = 1 - dot(closest, closest);
  if (difference < -EPSILON) return [];
  const spread = Math.sqrt(Math.max(0, difference) / squared);
  return [fraction - spread, fraction + spread].filter(t => t >= -EPSILON && t <= 1 + EPSILON)
    .map(t => mix(a, b, Math.max(0, Math.min(1, t)))).filter(curve.includes);
}

function crossingSpans(entity, path, bounds) {
  const line = (a, b) => ({ a, b, bounds: geometryBounds([a, b]) });
  const curve = (shape, limits) => ({ curve: snapConic(shape), bounds: limits });
  if (["circle", "arc", "ellipse"].includes(entity.type)) return [curve(entity, bounds)];
  if (entity.type === "polyline") {
    return Array.from({ length: entity.points.length - Number(!entity.closed) }, (_, index) => {
      const { start, end, arc } = polylineEdge(entity, index);
      if (!arc) return line(start, end);
      const shape = { type: "arc", ...arc };
      return curve(shape, curveBounds(shape, [start, end]));
    });
  }
  return path.slice(1).map((point, index) => line(path[index], point));
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
  point: { label: "Вспомогательная точка", points: ["Положение точки"] },
  line: { label: "Отрезок", points: ["Начальная точка", "Конечная точка"] },
  polyline: { label: "Полилиния", points: ["Начальная точка", "Следующая вершина"] },
  circle: { label: "Окружность", points: ["Центр окружности", "Точка на окружности"] },
  arc: { label: "Дуга", points: ["Начало дуги", "Промежуточная точка", "Конец дуги"] },
  spline: { label: "Сплайн по точкам", points: ["Начальная точка", "Следующая точка сплайна · Enter завершает"] },
};
const FIGURES = {
  rectangle: { label: "Прямоугольник", points: ["Первый угол", "Противоположный угол"] },
  square: { label: "Квадрат", points: ["Первый угол", "Противоположный угол"] },
  triangle: { label: "Треугольник", points: ["Первая вершина", "Вторая вершина", "Третья вершина"] },
  polygon: { label: "Правильный многоугольник", points: ["Центр многоугольника", "Вершина"] },
  ellipse: { label: "Эллипс", points: ["Центр эллипса", "Конец первой оси", "Точка для второй полуоси"] },
};
const DRAW = { ...CREATE, ...FIGURES };
const DIRECT_DISTANCE_LABELS = {
  line: "Длина", polyline: "Длина сегмента", circle: "Радиус", spline: "Расстояние до точки",
  triangle: "Расстояние до вершины", arc: "Расстояние до точки", polygon: "Радиус",
  move: "Смещение", copy: "Смещение", mirror: "Длина оси", measure: "Расстояние",
};
const RECTANGLE_SIDES = ["A", "B"];
const TOOL_PARAMETERS = {
  offset: ["Припуск", "Смещение, мм", "Предпросмотр", "5"],
  rotate: ["Поворот", "Угол, °", "Выбрать центр", "90"],
  polygon: ["Правильный многоугольник", "Количество сторон", "Начать построение", "6"],
  notch: ["Надсечка", "Глубина, мм", "Выбрать кромку", "3"],
  "notch-series": ["Серия надсечек", "Глубина, мм", "Выбрать кромку", "3"],
  "notch-edit": ["Изменить надсечку", "Глубина, мм", "Изменить глубину"],
  scale: ["Масштабирование", "", "Применить"],
};
const EDIT = {
  move: { label: "Перенести", points: ["Базовая точка", "Новая точка"] },
  copy: { label: "Копировать", points: ["Базовая точка", "Точка вставки"] },
  rotate: { label: "Повернуть", points: ["Центр поворота"] },
  scale: { label: "Масштабировать" },
  mirror: { label: "Отразить", points: ["Начало оси", "Конец оси"] },
  trim: { label: "Обрезать", points: ["Участок для удаления"] },
  notch: { label: "Надсечка", points: ["Точка на контуре"] },
  "notch-series": { label: "Серия надсечек", points: ["Точка на контуре"] },
  "notch-edit": { label: "Изменить надсечку", points: ["Точка на контуре"] },
};
const ENTITY_NAMES = { ...Object.fromEntries(Object.entries(DRAW).map(([key, value]) => [key, value.label])) };

const CAD_ICONS = {
  back: '<path d="m14 6-6 6 6 6M8 12h12"/>',
  select: '<use href="#i-cursor"/>',
  point: '<path d="M12 3v5m0 8v5M3 12h5m8 0h5"/><circle cx="12" cy="12" r="2"/>',
  rectangle: '<rect x="3" y="6" width="18" height="12"/>',
  square: '<rect x="5" y="5" width="14" height="14"/>',
  triangle: '<path d="m12 4 9 15H3Z"/>',
  polygon: '<path d="m12 3 8 5v8l-8 5-8-5V8Z"/>',
  ellipse: '<ellipse cx="12" cy="12" rx="9" ry="5" transform="rotate(-25 12 12)"/>',
  line: '<path d="M6 18 18 6"/><rect x="3" y="17" width="4" height="4"/><rect x="17" y="3" width="4" height="4"/>',
  polyline: '<path d="m4 17 5-9 5 5 6-7"/><circle cx="4" cy="17" r="1.5"/><circle cx="9" cy="8" r="1.5"/><circle cx="14" cy="13" r="1.5"/><circle cx="20" cy="6" r="1.5"/>',
  circle: '<circle cx="12" cy="12" r="8"/><path d="M10 12h4m-2-2v4"/>',
  arc: '<path d="M4 16a8 8 0 0 1 16 0"/><path d="M3 16h2m14 0h2M12 7v2"/>',
  spline: '<path d="M3 16c5 0 3-9 8-9s3 9 10 9"/><circle cx="3" cy="16" r="1"/><circle cx="11" cy="7" r="1"/><circle cx="21" cy="16" r="1"/>',
  move: '<path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3m12-6 3 3-3 3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2"/>',
  rotate: '<use href="#i-turn-cw"/>',
  scale: '<path d="M13 4h7v7m0-7-8 8"/><rect x="4" y="12" width="8" height="8" rx="1"/>',
  mirror: '<path d="M12 3v18M9 5 3 18h6Zm6 0 6 13h-6Z"/>',
  trim: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8 8l12 12M8 16 20 4"/>',
  join: '<path d="M12 9V5H5a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h7m0-2h7a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-7v-4M8 12h8"/>',
  offset: '<path d="M5 20V5h15M9 20V9h11m-5 6h5m-2-2 2 2-2 2"/>',
  notch: '<path d="M3 17h7l2-5 2 5h7m-9-9V4m-2 2 2 2 2-2"/>',
  "notch-series": '<path d="M3 17h18M6 17v-6m6 6v-6m6 6v-6M3 6h18"/>',
  "notch-edit": '<path d="M3 18h18m-9 0V8M4 6h16m-3-3 3 3-3 3M7 3 4 6l3 3"/>',
  erase: '<path d="m4 13 9-9a2 2 0 0 1 3 0l4 4a2 2 0 0 1 0 3l-9 9H8l-4-4a2 2 0 0 1 0-3Zm4-4 8 8m-5 3h10"/>',
  undo: '<use href="#i-undo"/>',
  redo: '<use href="#i-redo"/>',
  fit: '<use href="#i-fit"/>',
  measure: '<use href="#i-ruler"/>',
  tools: '<use href="#i-sliders"/>',
  repair: '<path d="m3 9 3-3 3 3m-3-3v6a5 5 0 0 0 5 5h2m8-2-3 3-3-3m3 3v-6a5 5 0 0 0-5-5h-2"/>',
  reference: '<path d="M4 5h6m4 0h6M20 9v6m0 4h-6m-4 0H4m0-4V9"/>',
  shortcuts: '<path d="m9 3-.5 2.2-2 .9-2-.8-1.5 2.6 1.6 1.5v2.3L3 13.2l1.5 2.6 2-.8 2 .9L9 18h3l.5-2.1 2-.9 2 .8 1.5-2.6-1.6-1.5V9.4L18 7.9l-1.5-2.6-2 .8-2-.9L12 3Z" transform="translate(1 1)"/><circle cx="11.5" cy="11.5" r="2.5"/>',
};

function cadIcon(key) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${CAD_ICONS[key]}</svg>`;
}

function iconButton(key, label, mode = "action", title = label) {
  const pressed = mode === "tool" ? ` aria-pressed="${key === "select"}"` : "";
  return `<button type="button" class="button cad-icon-button" data-${mode}="${key}" aria-label="${label}" title="${title}"${pressed}>${cadIcon(key)}</button>`;
}

function toolButtons(tools) {
  return Object.entries(tools).map(([key, { label }]) => iconButton(key, label, "tool")).join("");
}

let cadToolsPanelSequence = 0;

const markup = `
  <div class="cad-header">
    <div class="cad-document">
      <button type="button" class="button" data-action="back">${cadIcon("back")}К реестру</button>
      <label class="cad-name"><span class="visually-hidden">Название чертежа</span><input data-field="name" maxlength="120"></label>
    </div>
    <div class="cad-actions" role="group" aria-label="Чертёж">
      <button type="button" class="button" data-action="import">Импорт DXF / DWG</button>
      <input type="file" data-field="file" accept=".dxf,.dwg" hidden>
      <button type="button" class="button" data-action="save">Сохранить чертёж</button>
      <button type="button" class="button primary" data-action="part">Сохранить деталь</button>
    </div>
  </div>
  <div class="cad-work">
    <div class="cad-tools" role="group" aria-label="Инструменты чертежа">
      <div class="cad-tool-group" role="group" aria-label="Построение">
        ${iconButton("select", "Выбор", "tool")}
        ${toolButtons(CREATE)}
      </div>
      <div class="cad-tool-group" role="group" aria-label="Фигуры">
        <span class="cad-tool-caption">Фигуры</span>
        ${toolButtons(FIGURES)}
      </div>
      <div class="cad-tool-group" role="group" aria-label="Правка">
        ${toolButtons(Object.fromEntries(Object.entries(EDIT).filter(([key]) => !key.startsWith("notch"))))}
        ${iconButton("join", "Соединить", "action", "Соединить выбранные фрагменты в один объект")}
        ${iconButton("offset", "Припуск", "tool")}
        ${iconButton("erase", "Ластик", "tool")}
      </div>
      <div class="cad-tool-group" role="group" aria-label="Надсечки">
        <span class="cad-tool-caption">Надсечки</span>
        ${toolButtons(Object.fromEntries(Object.entries(EDIT).filter(([key]) => key.startsWith("notch"))))}
      </div>
      <div class="cad-tool-group" role="group" aria-label="История и вид">
        ${iconButton("undo", "Отменить", "action", "Отменить (Ctrl+Z)")}
        ${iconButton("redo", "Повторить", "action", "Повторить (Ctrl+Y или Ctrl+Shift+Z)")}
        ${iconButton("fit", "Вписать", "action", "Показать чертёж целиком")}
        ${iconButton("measure", "Измерить расстояние", "tool")}
      </div>
    </div>
    <div class="cad-view">
      <canvas tabindex="0" aria-label="Графическое поле чертежа"></canvas>
      <div class="cad-grid-status" data-view="scale"></div>
      <output class="cad-distance" data-view="distance-entry" hidden><span data-view="distance-value"></span><kbd data-view="distance-key">Enter</kbd></output>
      <details class="cad-warnings" data-view="warnings" hidden>
        <summary></summary><ul></ul>
      </details>
      <div class="cad-message" data-view="message" role="status" hidden></div>
      <div class="cad-preview-actions" data-view="notch-preview" hidden>
        <button class="button primary" type="button" data-action="apply-notches">Добавить надсечки</button>
        <button class="button" type="button" data-action="cancel-notches">Отмена</button>
      </div>
      <div class="cad-preview-actions" data-view="offset-preview" hidden>
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
  </div>
  <div class="cad-footer">
      <div class="cad-command" data-view="context" hidden>
        <div class="cad-command-fields" data-view="measurements" hidden>
          <button class="button" type="button" data-action="remove-measurement">Убрать последнее</button>
          <button class="button" type="button" data-action="clear-measurements">Очистить измерения</button>
        </div>
        <div class="cad-finish" data-view="erase" hidden>
          <button class="button primary" type="button" data-action="erase-selection">Удалить выбранное</button>
        </div>
        <div class="cad-finish" data-view="finish" hidden>
          <button class="button" type="button" data-action="finish">Завершить</button>
          <button class="button" type="button" data-action="close">Замкнуть</button>
        </div>
      </div>
    <div class="cad-selection-status" data-view="selection-summary" hidden><span class="cad-selection-description" data-view="selection"></span></div>
    <div class="cad-action-grid">
      <div class="cad-selection-actions" data-view="selection-tools" hidden>
        <button class="button cad-action-repair" type="button" data-action="repair-part" hidden>${cadIcon("repair")}<span>Восстановить деталь</span></button>
        <button class="button cad-action-reference" type="button" data-action="reference" hidden>${cadIcon("reference")}<span></span></button>
      </div>
      <div class="cad-settings">
        <button type="button" class="button cad-action-tools" data-action="tools" aria-expanded="false">${cadIcon("tools")}<span>Инструменты</span></button>
        <div class="cad-snaps" data-view="tools-panel" role="group" aria-label="Инструменты" hidden>
          <strong>Привязки</strong>
          <label class="cad-checkbox" title="Привязка к узлам и серединам рёбер"><input type="checkbox" data-field="objects" checked> Узлы</label>
          <label class="cad-checkbox" title="Привязка к пересечениям"><input type="checkbox" data-field="crossings"> Пересечения</label>
          <label class="cad-checkbox" title="Привязка к ближайшей точке ребра"><input type="checkbox" data-field="edges"> Рёбра</label>
          <label class="cad-checkbox" title="Перпендикуляр к прямому ребру от последней точки"><input type="checkbox" data-field="perpendicular"> Перпендикуляр</label>
          <label class="cad-checkbox" title="Горизонталь и вертикаль от наведённой опорной точки"><input type="checkbox" data-field="tracking"> Направляющие</label>
          <label class="cad-checkbox" title="Привязка к сетке"><input type="checkbox" data-field="grid"> Сетка</label>
          <label class="cad-checkbox" title="Ограничить построение по осям. Удерживайте Shift для временного ограничения."><input type="checkbox" data-field="ortho"> Орто</label>
        </div>
        <button type="button" class="button cad-action-shortcuts" data-action="shortcuts">${cadIcon("shortcuts")}<span>Горячие клавиши</span></button>
      </div>
    </div>
  </div>
  <dialog class="plotter-dialog cad-parameter-dialog" data-view="parameters">
    <header class="drawing-dialog-heading"><h2 data-view="parameter-title"></h2></header>
    <form data-form="option" novalidate>
      <div class="drawing-dialog-body cad-parameter-fields">
        <label><span data-view="option-label"></span><input data-field="option" inputmode="decimal" required></label>
        <div class="cad-command-fields" data-view="notch-series" hidden>
          <label>Расположение<select data-field="notch-scope"><option value="edge">На выбранном ребре</option><option value="contour">По всему замкнутому контуру</option></select></label>
          <label>Количество<input data-field="notch-count" type="number" min="1" max="100" value="3" required></label>
        </div>
        <label class="cad-checkbox" data-view="scale-mode" hidden><input type="checkbox" data-field="proportional"> Сохранять пропорции</label>
        <p role="alert" data-view="parameter-error" hidden></p>
      </div>
      <footer class="dialog-actions">
        <button class="button" type="button" data-action="cancel-parameters">Отмена</button>
        <button class="button" type="button" data-action="relocate-notch" hidden>Перенести надсечку</button>
        <button class="button primary" type="submit" data-view="option-button"></button>
      </footer>
    </form>
  </dialog>
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

export function mountCadEditor(container, { document: draft, onBack, onSaveDocument, onSavePart, request, layoutPart = false }) {
  container.classList.add("cad-editor");
  container.innerHTML = markup;
  const root = container.ownerDocument;
  const window = root.defaultView;
  const canvas = container.querySelector("canvas");
  const context = canvas.getContext("2d");
  const fields = Object.fromEntries(Array.from(container.querySelectorAll("[data-field]"), node => [node.dataset.field, node]));
  const actions = Object.fromEntries(Array.from(container.querySelectorAll("[data-action]"), node => [node.dataset.action, node]));
  const views = Object.fromEntries(Array.from(container.querySelectorAll("[data-view]"), node => [node.dataset.view, node]));
  const buttons = Object.fromEntries(Array.from(container.querySelectorAll("[data-tool]"), node => [node.dataset.tool, node]));
  const optionForm = container.querySelector('[data-form="option"]');
  const parameterDialog = views.parameters;
  views["parameter-title"].id = "cad-parameter-title";
  parameterDialog.setAttribute("aria-labelledby", views["parameter-title"].id);
  const toolsPanel = views["tools-panel"];
  toolsPanel.id = `cad-tools-panel-${++cadToolsPanelSequence}`;
  actions.tools.setAttribute("aria-controls", toolsPanel.id);
  const lifecycle = new AbortController();
  const listener = { signal: lifecycle.signal };
  const previous = [];
  const future = [];
  const geometryCache = new WeakMap();
  const initial = { entities: draft?.entities || [], warnings: (draft?.warnings || []).filter(warning => warning !== LEGACY_DEFAULT_UNITS_WARNING) };
  let current = initial;
  let saved = initial;
  let savedName = draft?.name || "Новый чертёж";
  let selected = new Set();
  const selectedEdges = new Map();
  let tool = "select";
  let pending = [];
  let distanceInput = "";
  let rectangleAxis = 0;
  let rectangleSizes = ["", ""];
  let rectangleEditing = false;
  let rectangleDirection = null;
  let directionPoint = null;
  let directionStart = null;
  let offsetPreview = null;
  let notchPreview = null;
  let notchTarget = null;
  let placingNotch = false;
  let scaleBox = null;
  let scaleSide = null;
  let scaleIds = new Set();
  const measurements = [];
  let cursor = null;
  let lastPointer = null;
  let pointerFrame = null;
  let pendingPointer = null;
  let snap = null;
  let snapType = "";
  let trackingOrigin = null;
  let guideAxes = [];
  let hover = null;
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
  if (layoutPart) {
    fields.name.readOnly = true;
    actions.back.lastChild.textContent = "К раскладке";
    actions.import.hidden = actions.part.hidden = true;
    actions.save.textContent = "Сохранить в раскрой";
    actions.save.classList.add("primary");
  }

  function geometry(entity) {
    let cached = geometryCache.get(entity);
    if (!cached) {
      const path = samples(entity);
      cached = { path, bounds: curveBounds(entity, path) };
      geometryCache.set(entity, cached);
    }
    return cached;
  }

  function nearbyCrossings(raw, tolerance, ignoredId, consider) {
    const [x, y] = raw;
    const visible = bounds => bounds[0] <= x + tolerance && bounds[2] >= x - tolerance
      && bounds[1] <= y + tolerance && bounds[3] >= y - tolerance;
    const nearby = [];
    for (const entity of current.entities) {
      if (entity.id === ignoredId || entity.type === "point") continue;
      const cached = geometry(entity);
      if (!visible(cached.bounds)) continue;
      cached.crossings ??= crossingSpans(entity, cached.path, cached.bounds);
      const count = cached.crossings.length;
      const loop = entity.closed || entity.type === "circle" || Math.abs(entity.sweep) >= 360 - EPSILON;
      for (const [index, segment] of cached.crossings.entries()) {
        if (!visible(segment.bounds)) continue;
        if (!segment.curve && distance(raw, projectToSegment(raw, segment.a, segment.b)) > tolerance) continue;
        for (const other of nearby) {
          if (other.id === entity.id && (index - other.index <= 1 || loop && index - other.index === count - 1)) continue;
          if (segment.curve && other.segment.curve && (!segment.curve.circle || !other.segment.curve.circle)) {
            const first = segment.curve.circle ? other : { segment, path: cached.path };
            const second = first === other ? segment : other.segment;
            const points = first.path;
            for (let i = 1; i < points.length; i++) {
              const a = points[i - 1];
              const b = points[i];
              if (distance(raw, projectToSegment(raw, a, b)) > tolerance) continue;
              for (const point of snapCrossings({ a, b }, second)) consider(point, "Пересечение");
            }
          } else {
            for (const point of snapCrossings(segment, other.segment)) consider(point, "Пересечение");
          }
        }
        if (nearby.length < 128) nearby.push({ id: entity.id, index, segment, path: cached.path });
      }
    }
  }

  function newId() {
    return root.defaultView.crypto.randomUUID?.() || "cad-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  function selectedEntities() {
    return current.entities.filter(entity => selected.has(entity.id));
  }

  function joinIds() {
    const ids = new Set([...selected, ...selectedEdges.keys()]);
    if (!ids.size) return [];
    const byId = new Map(current.entities.filter(entity => ids.has(entity.id)).map(entity => [entity.id, entity]));
    const eligible = id => {
      const entity = byId.get(id);
      return entity && !entity.notch && !entity.closed && !["point", "circle"].includes(entity.type)
        && !(entity.type === "ellipse" && Math.abs(entity.sweep) === 360);
    };
    const whole = [...selected].filter(eligible);
    const edges = [...selectedEdges].filter(([id, picked]) => eligible(id) && picked.size === byId.get(id).points.length - 1);
    return [...whole, ...edges.map(([id]) => id)];
  }

  function selectedGrips(entity) {
    const points = controls(entity);
    const edges = selectedEdges.get(entity.id);
    if (!edges) return points.entries();
    const vertices = new Set();
    for (const index of edges) {
      vertices.add(index);
      vertices.add((index + 1) % points.length);
    }
    return Array.from(vertices, index => [index, points[index]]);
  }

  function gripOwner() {
    if (tool !== "select" || selected.size + selectedEdges.size !== 1) return null;
    const id = (selectedEdges.size ? selectedEdges.keys() : selected.values()).next().value;
    const entity = current.entities.find(item => item.id === id);
    return entity?.notch && entity.parentId ? null : entity;
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

  function showTools(open, restoreFocus = false) {
    toolsPanel.hidden = !open;
    actions.tools.setAttribute("aria-expanded", String(open));
    if (restoreFocus) actions.tools.focus({ preventScroll: true });
  }

  function update() {
    const items = selectedEntities();
    const count = items.length;
    const settings = count > 0;
    const contours = items.filter(entity => !entity.notch && entity.type !== "point").length;
    const first = items[0];
    const active = Boolean(gesture?.type === "grip" || gesture?.type === "scale" || offsetPreview || notchPreview);
    for (const [key, button] of Object.entries(buttons)) {
      button.setAttribute("aria-pressed", String(key === tool));
      button.disabled = running || active || (key in EDIT && !key.startsWith("notch") && !settings)
        || (key === "offset" && (count !== 1 || !contours))
        || (key === "scale" && !items.some(entity => entity.type !== "point"))
        || (key === "trim" && contours < 2);
    }
    actions["erase-selection"].disabled = running || active || !settings;
    actions.join.disabled = running || active || joinIds().length < 2;
    actions.undo.disabled = running || active || !previous.length;
    actions.redo.disabled = running || active || !future.length;
    actions.import.disabled = running || active;
    actions.save.disabled = running || active;
    actions.part.disabled = running || active || !items.some(entity => !entity.reference && !entity.notch);
    views["notch-preview"].hidden = !notchPreview;
    views.measurements.hidden = tool !== "measure" || !measurements.length;
    views.erase.hidden = tool !== "erase" || !settings;
    canvas.dataset.tool = tool;
    const finishable = tool === "spline" || tool === "polyline";
    views.finish.hidden = !finishable || pending.length < 2;
    actions.finish.disabled = pending.length < 2;
    actions.close.disabled = pending.length < 3;
    views["offset-preview"].hidden = !offsetPreview;
    views["preview-warning"].hidden = !offsetPreview?.warnings.length;
    views["preview-warning"].textContent = offsetPreview?.warnings.map(warning => warning?.message || String(warning)).join(" ") || "";
    views.context.hidden = [...views.context.children].every(child => child.hidden);
    const edgeCount = Array.from(selectedEdges.values()).reduce((total, edges) => total + edges.size, 0);
    const selection = [...new Map([...items, ...current.entities.filter(entity => selectedEdges.has(entity.id))]
      .map(entity => [entity.id, entity])).values()];
    const needsReview = selection.filter(entity => entity.importDamaged).length;
    const diagnostic = [...new Set(selection.flatMap(entity => entity.importDamaged
      ? entity.importIssues || ["Повреждение отмечено при импорте. Точная причина не сохранена."] : []))];
    if (count + edgeCount === 1) {
      const [entity] = selection;
      views.selection.textContent = [edgeCount ? "Ребро полилинии" : ENTITY_NAMES[entity.type] || entity.type, entity.layer,
        entity.reference ? "вспомогательный" : "", entity.importDamaged ? "Проверьте после импорта" : ""].filter(Boolean).join(" · ");
    } else {
      views.selection.textContent = [
        count || !edgeCount ? `Выбрано объектов: ${count}` : "",
        edgeCount ? `Выбрано рёбер: ${edgeCount}` : "",
        needsReview ? `Требуют проверки после импорта: ${needsReview}` : "",
      ].filter(Boolean).join(" · ");
    }
    if (diagnostic.length) views.selection.append(` ${diagnostic.join(" ")}`);
    views["selection-summary"].hidden = (!count && !edgeCount) || tool !== "select";
    actions["repair-part"].hidden = tool !== "select" || !selection.length
      || !selection.every(entity => !entity.notch && entity.type !== "point");
    actions["repair-part"].disabled = running || active;
    actions.reference.hidden = !first || tool !== "select" || items.every(entity => entity.type === "point");
    actions.reference.disabled = running || active;
    actions.reference.querySelector("span").textContent = items.every(entity => entity.reference) ? "Назначить для реза" : "Сделать вспомогательным";
    views["selection-tools"].hidden = actions["repair-part"].hidden && actions.reference.hidden;
    draw();
  }

  function commit(entities, { warnings = current.warnings, ids = selected } = {}) {
    previous.push(current);
    if (previous.length > 50) previous.shift();
    future.length = 0;
    current = { entities, warnings };
    if (ids !== selected) selectedEdges.clear();
    selected = new Set(ids);
    updateWarnings(warnings);
    update();
  }

  function choose(next) {
    clearPointerMove();
    cancelGesture();
    if (tool === "measure") {
      measurements.length = 0;
      if (next === "measure") next = "select";
    }
    tool = next;
    scaleSide = null;
    placingNotch = false;
    scaleIds = tool === "scale" ? familyIds(selected) : new Set();
    scaleBox = tool === "scale" ? selectionBounds() : null;
    if (next !== "select") selectedEdges.clear();
    pending = [];
    resetDirectDistance();
    cursor = snap = null;
    hover = notchTarget = trackingOrigin = null;
    lastPointer = null;
    guideAxes = [];
    log();
    update();
    canvas.focus({ preventScroll: true });
  }

  function openParameters() {
    if (running || root.querySelector("dialog[open]")) return;
    showTools(false);
    const [title, label, submit] = TOOL_PARAMETERS[tool];
    views["parameter-title"].textContent = title;
    views["option-label"].textContent = tool === "scale" ? `${scaleSide.axis === 0 ? "Ширина" : "Высота"}, мм` : label;
    views["option-button"].textContent = submit;
    views["scale-mode"].hidden = tool !== "scale";
    views["notch-series"].hidden = tool !== "notch-series";
    actions["relocate-notch"].hidden = tool !== "notch-edit";
    parameterError();
    parameterDialog.showModal();
    fields.option.focus({ preventScroll: true });
    fields.option.select();
    if (tool === "scale") draw();
  }

  function parameterError(message = "") {
    views["parameter-error"].textContent = message;
    views["parameter-error"].hidden = !message;
  }

  function closeParameters() {
    parameterDialog.close();
    canvas.focus({ preventScroll: true });
  }

  function cancelParameters() {
    closeParameters();
    if (tool === "scale") {
      scaleSide = null;
      update();
    } else choose("select");
  }

  function undo(from, to) {
    if (!from.length || running || gesture?.type === "grip" || offsetPreview || notchPreview) return;
    cancelGesture();
    to.push(current);
    current = from.pop();
    selected = new Set();
    selectedEdges.clear();
    choose(tool === "erase" ? "erase" : "select");
    updateWarnings(current.warnings);
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

  function pathLine(points, stroke = () => context.stroke()) {
    if (!points.length) return;
    context.beginPath();
    context.moveTo(...screen(points[0]));
    for (const point of points.slice(1)) context.lineTo(...screen(point));
    stroke();
  }

  function selectionBounds() {
    const points = selectedEntities().flatMap(entity => {
      const [left, bottom, right, top] = geometry(entity).bounds;
      return [[left, bottom], [right, top]];
    });
    const bounds = geometryBounds(points);
    return bounds.every(Number.isFinite) ? bounds : null;
  }

  function scaleHandles() {
    const [left, bottom, right, top] = scaleBox;
    const middleX = (left + right) / 2;
    const middleY = (bottom + top) / 2;
    return [
      { axis: 0, point: [left, middleY], origin: [right, middleY] },
      { axis: 0, point: [right, middleY], origin: [left, middleY] },
      { axis: 1, point: [middleX, bottom], origin: [middleX, top] },
      { axis: 1, point: [middleX, top], origin: [middleX, bottom] },
    ].filter(({ axis, point, origin }) => Math.abs(point[axis] - origin[axis]) > CAD_POINT_EPSILON);
  }

  function scaleHandleAt(at, handles = scaleHandles()) {
    let nearest = null;
    let distanceLimit = 10;
    for (const handle of handles) {
      const gap = distance(screen(handle.point), at);
      if (gap > distanceLimit || nearest && gap === distanceLimit) continue;
      nearest = handle;
      distanceLimit = gap;
    }
    return nearest;
  }

  function scaleParameters(side, size, proportional) {
    const { axis, point, origin } = side;
    const factor = size / Math.abs(point[axis] - origin[axis]);
    return { origin, sx: axis === 0 || proportional ? factor : 1, sy: axis === 1 || proportional ? factor : 1 };
  }

  function drawScaleFrame() {
    const scaling = gesture?.type === "scale";
    const dragging = scaling && gesture.parameters;
    const handles = scaleHandles();
    const hovered = !gesture && !running && !parameterDialog.open && lastPointer
      ? scaleHandleAt(lastPointer.at, handles) : null;
    const ready = scaling ? scaleSide : hovered;
    const axis = ready && (ready.axis === 0 ? "x" : "y");
    if (axis && canvas.dataset.scaleAxis !== axis) canvas.dataset.scaleAxis = axis;
    else if (!axis && canvas.dataset.scaleAxis) delete canvas.dataset.scaleAxis;
    const [left, bottom, right, top] = scaleBox;
    const [leftX, topY] = screen([left, top]);
    const [rightX, bottomY] = screen([right, bottom]);
    context.save();
    if (dragging) {
      const { origin, sx, sy } = gesture.parameters;
      const [x, y] = screen(origin);
      context.translate(x, y);
      context.scale(sx, sy);
      context.translate(-x, -y);
      for (const entity of current.entities) {
        if (scaleIds.has(entity.id)) drawEntity(entity, selected.has(entity.id), true);
      }
    }
    context.strokeStyle = CAD_SELECTED_COLOR;
    context.lineWidth = 1;
    context.setLineDash([5, 4]);
    context.strokeRect(leftX, topY, rightX - leftX, bottomY - topY);
    context.setLineDash([]);
    if (!dragging) {
      for (const handle of handles) {
        const [x, y] = screen(handle.point);
        const active = handle === hovered || scaleSide?.axis === handle.axis && handle.point[handle.axis] === scaleSide.point[handle.axis];
        const radius = active ? 7 : 5;
        context.fillStyle = active ? CAD_SELECTED_COLOR : "#fff";
        context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
        context.strokeRect(x - radius, y - radius, radius * 2, radius * 2);
      }
    }
    context.restore();
  }

  function updateScaleGesture(point, shift) {
    const { axis, point: handle, origin } = scaleSide;
    const moved = handle[axis] + point[axis] - world(gesture.start)[axis];
    const size = Math.max(CAD_POINT_EPSILON, Math.sign(handle[axis] - origin[axis]) * (moved - origin[axis]));
    gesture.parameters = scaleParameters(scaleSide, size, shift || fields.proportional.checked);
  }

  function applyScale(parameters) {
    if (!parameters || parameters.sx === 1 && parameters.sy === 1) return;
    void editOnServer("scale", { ids: [...selected], ...parameters });
  }

  function drawGrid() {
    const step = gridStep();
    const topLeft = world([0, 0]);
    const bottomRight = world([width, height]);
    context.strokeStyle = "#e6e6e6";
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
    context.strokeStyle = "#aaa";
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
    context.fillStyle = "#4b4b4b";
    context.font = "11px Inter, Segoe UI, sans-serif";
    if (originX >= 0 && originX <= width && originY >= 0 && originY <= height) {
      context.fillText("0", originX + 5, originY + 14);
    }
  }

  function drawEntity(entity, highlighted = false, preview = false, offset = null) {
    const { path, bounds } = geometry(entity);
    const dx = offset?.[0] ?? 0;
    const dy = offset?.[1] ?? 0;
    const [minX, maxY] = world([-10 - dx, -10 - dy]);
    const [maxX, minY] = world([width + 10 - dx, height + 10 - dy]);
    if (bounds[2] < minX || bounds[0] > maxX || bounds[3] < minY || bounds[1] > maxY) return;
    const baseColor = offset && !entity.notch ? "#646464"
      : entity.importDamaged ? "#b00020" : entity.notch ? CAD_NOTCH_COLOR : entity.reference ? "#777" : "#171717";
    const overlay = highlighted && (entity.importDamaged || entity.notch);
    context.strokeStyle = highlighted ? CAD_SELECTED_COLOR : baseColor;
    context.lineWidth = overlay ? 5 : highlighted ? 3 : preview || entity.notch || entity.importDamaged ? 2.25 : 1.5;
    context.setLineDash(!highlighted && entity.reference ? [6, 4] : []);
    const stroke = () => {
      context.stroke();
      if (overlay) {
        context.strokeStyle = baseColor;
        context.lineWidth = 2;
        context.stroke();
      }
    };
    if (entity.type === "point") {
      const [x, y] = screen(entity.point);
      context.setLineDash([]);
      context.beginPath();
      context.moveTo(x - 5, y);
      context.lineTo(x + 5, y);
      context.moveTo(x, y - 5);
      context.lineTo(x, y + 5);
      stroke();
    } else if (entity.type === "circle" || entity.type === "arc") {
      context.beginPath();
      const start = entity.type === "arc" ? entity.startAngle : 0;
      const sweep = entity.type === "arc" ? entity.sweep : 360;
      context.arc(...screen(entity.center), entity.radius * scale, -start * DEG, -(start + sweep) * DEG, sweep > 0);
      stroke();
    } else if (entity.type === "ellipse") {
      context.beginPath();
      const start = entity.startAngle;
      const sweep = entity.sweep;
      const radius = norm(entity.axis) * scale;
      context.ellipse(...screen(entity.center), radius, radius * entity.ratio, -angle(entity.axis) * DEG,
        -start * DEG, -(start + sweep) * DEG, sweep > 0);
      stroke();
    } else {
      pathLine(path, stroke);
    }
    context.setLineDash([]);
  }

  function drawMovePreview(base, target) {
    const [dx, dy] = sub(target, base);
    const offset = [dx * scale, -dy * scale];
    if (norm(offset) < 1) return;
    const ids = familyIds(selected);
    context.save();
    context.translate(...offset);
    for (const entity of current.entities) {
      if (ids.has(entity.id)) drawEntity(entity, false, true, offset);
    }
    context.restore();
  }

  function draw() {
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);
    drawGrid();
    const shown = offsetPreview ? previewEntities() : notchPreview?.entities || gesture?.entities || current.entities;
    for (const entity of shown) {
      if (gesture?.type === "scale" && gesture.parameters && scaleIds.has(entity.id)) continue;
      if (!offsetPreview && !notchPreview && selected.has(entity.id)) continue;
      const activePreview = offsetPreview && [offsetPreview.original, ...offsetPreview.ids].includes(entity.parentId || entity.id);
      drawEntity(entity, false, Boolean(hover?.id === entity.id || activePreview || notchPreview?.ids.includes(entity.id)));
    }
    for (const entity of shown) {
      if (offsetPreview || notchPreview) continue;
      if (selected.has(entity.id) && !(gesture?.type === "scale" && gesture.parameters)) drawEntity(entity, true);
      const edges = selectedEdges.get(entity.id);
      if (edges) {
        context.strokeStyle = CAD_SELECTED_COLOR;
        context.lineWidth = entity.importDamaged ? 4.5 : 3;
        context.setLineDash([]);
        for (const index of edges) drawEdge(entity, index);
        if (entity.importDamaged) {
          context.strokeStyle = "#b00020";
          context.lineWidth = 1.5;
          for (const index of edges) drawEdge(entity, index);
        }
      }
    }
    const owner = gripOwner();
    if (owner && !offsetPreview && !notchPreview) {
      const entity = shown.find(item => item.id === owner.id);
      for (const [index, grip] of selectedGrips(entity)) {
        const [x, y] = screen(grip);
        context.fillStyle = gesture?.type === "grip" && gesture.id === entity.id && gesture.index === index ? "#171717" : "#fff";
        context.strokeStyle = "#171717";
        context.lineWidth = 1.5;
        context.fillRect(x - 3, y - 3, 6, 6);
        context.strokeRect(x - 3, y - 3, 6, 6);
      }
    }
    const previewPoint = typedDistancePoint() || cursor;
    drawDirectDistance();
    if (pending.length && previewPoint && DRAW[tool]) {
      const points = [...pending, previewPoint];
      const provisional = creation(points, false, true);
      context.strokeStyle = "#555";
      context.lineWidth = 1.5;
      context.setLineDash([5, 4]);
      pathLine(provisional ? samples(provisional) : points);
      context.setLineDash([]);
    } else if (pending.length && previewPoint && EDIT[tool]) {
      if (tool === "move") drawMovePreview(pending[0], previewPoint);
      context.strokeStyle = "#555";
      context.lineWidth = 1;
      context.setLineDash([5, 4]);
      pathLine([pending[0], previewPoint]);
      context.setLineDash([]);
    }
    if (tool === "scale" && scaleBox) drawScaleFrame();
    else if (canvas.dataset.scaleAxis) delete canvas.dataset.scaleAxis;
    if (tool === "measure" && (measurements.length || pending.length && previewPoint)) {
      drawMeasurements(pending.length && previewPoint ? [pending[0], previewPoint] : null);
    }
    if (pending.length && DRAW[tool]) {
      context.fillStyle = "#171717";
      for (const p of pending) {
        const [x, y] = screen(p);
        context.fillRect(x - 2.5, y - 2.5, 5, 5);
      }
    }
    const guideOrigin = ["Горизонталь", "Вертикаль"].includes(snapType) ? snap : trackingOrigin;
    if (guideAxes.length && guideOrigin) {
      const [x, y] = screen(guideOrigin);
      context.strokeStyle = "#777";
      context.lineWidth = 1;
      context.setLineDash([6, 4]);
      context.beginPath();
      if (guideAxes.includes(0)) { context.moveTo(x, 0); context.lineTo(x, height); }
      if (guideAxes.includes(1)) { context.moveTo(0, y); context.lineTo(width, y); }
      context.stroke();
      context.setLineDash([]);
    }
    if (snap && (tool !== "select" || gesture?.type === "grip")) {
      const [x, y] = screen(snap);
      context.strokeStyle = snapType === "Середина" ? CAD_SELECTED_COLOR : "#171717";
      context.lineWidth = 1.5;
      context.beginPath();
      if (snapType === "Середина") {
        context.moveTo(x, y - 7); context.lineTo(x + 6, y + 5); context.lineTo(x - 6, y + 5); context.closePath();
      } else if (snapType === "Пересечение" || snapType === "Сетка") {
        context.moveTo(x - 5, y); context.lineTo(x + 5, y); context.moveTo(x, y - 5); context.lineTo(x, y + 5);
      } else if (snapType === "Ребро" || snapType === "Перпендикуляр" || snapType === "Квадрант") {
        context.moveTo(x, y - 6); context.lineTo(x + 6, y); context.lineTo(x, y + 6); context.lineTo(x - 6, y); context.closePath();
      } else context.rect(x - 5, y - 5, 10, 10);
      context.stroke();
      if (snapType === "Середина") {
        context.font = "600 13px Inter, Segoe UI, sans-serif";
        const length = context.measureText(snapType).width + 12;
        const left = Math.max(3, Math.min(width - length - 3, x + 11));
        const top = Math.max(3, Math.min(height - 23, y - 24));
        context.fillStyle = "#fff";
        context.fillRect(left, top, length, 20);
        context.fillStyle = "#171717";
        context.fillText(snapType, left + 6, top + 14);
      }
    }
    if (gesture?.type === "marquee") {
      const [startX, startY] = gesture.start;
      const [endX, endY] = gesture.end;
      const crossing = endX < startX;
      context.fillStyle = crossing ? "#0000001c" : "#0000000e";
      context.strokeStyle = "#171717";
      context.lineWidth = 1;
      context.fillRect(startX, startY, endX - startX, endY - startY);
      context.setLineDash(crossing ? [5, 4] : []);
      context.strokeRect(startX, startY, endX - startX, endY - startY);
      context.setLineDash([]);
    }
    if (tool === "erase" && cursor && (!gesture || gesture.type === "erase")) {
      context.strokeStyle = "#555";
      context.lineWidth = 1.5;
      if (gesture?.type === "erase") {
        context.setLineDash([4, 3]);
        pathLine([...gesture.points, cursor]);
        context.setLineDash([]);
      }
      context.beginPath();
      context.arc(...screen(cursor), 8, 0, 2 * Math.PI);
      context.stroke();
    }
    const gridLabel = `Сетка ${format(gridStep())} мм`;
    if (views.scale.textContent !== gridLabel) views.scale.textContent = gridLabel;
  }

  function drawMeasurements(preview) {
    const lines = preview ? [...measurements, preview] : measurements;
    const labelRows = [];
    context.font = "500 14px Inter, Segoe UI, sans-serif";
    for (const line of lines) {
      const provisional = line === preview;
      const [start, end] = line.map(screen);
      if (Math.min(start[0], end[0]) > width || Math.min(start[1], end[1]) > height
          || Math.max(start[0], end[0]) < 0 || Math.max(start[1], end[1]) < 0) continue;
      context.strokeStyle = provisional ? "#555" : "#171717";
      context.lineWidth = 1.5;
      context.setLineDash(provisional ? [3, 4] : [6, 4]);
      pathLine(line);
      context.setLineDash([]);
      context.fillStyle = context.strokeStyle;
      for (const [x, y] of [start, end]) {
        context.beginPath();
        context.arc(x, y, 2.5, 0, Math.PI * 2);
        context.fill();
      }
      const [middleX, middleY] = mix(start, end, .5);
      const label = `${format(distance(...line))} мм`;
      const length = context.measureText(label).width + 12;
      const x = Math.max(3, Math.min(width - length - 3, middleX - length / 2));
      const initialY = Math.max(3, Math.min(height - 23, middleY - 27));
      let y = initialY;
      for (let offset = 0; offset < height; offset += 22) {
        const available = [initialY - offset, initialY + offset].find(position => position >= 3 && position + 23 <= height
          && !labelRows.some(row => x < row.x + row.length && x + length > row.x && Math.abs(position - row.y) < 22));
        if (available === undefined) continue;
        y = available;
        break;
      }
      labelRows.push({ x, y, length, label });
    }
    for (const { x, y, length, label } of labelRows) {
      context.fillStyle = "#fff";
      context.fillRect(x, y, length, 20);
      context.fillStyle = "#171717";
      context.fillText(label, x + 6, y + 15);
    }
  }

  function drawEdge(entity, index) {
    const { start, end, arc } = polylineEdge(entity, index);
    if (!arc) return pathLine([start, end]);
    context.beginPath();
    context.arc(...screen(arc.center), arc.radius * scale, -arc.startAngle * DEG,
      -(arc.startAngle + arc.sweep) * DEG, arc.sweep > 0);
    context.stroke();
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

  function closestOnArc(point, arc) {
    const direction = angle(sub(point, arc.center));
    const progress = positiveAngle(Math.sign(arc.sweep) * (direction - arc.startAngle));
    if (progress <= Math.abs(arc.sweep)) return add(arc.center, radial(direction, arc.radius));
    const start = arcPoint(arc, 0);
    const end = arcPoint(arc, 1);
    return distance(point, start) <= distance(point, end) ? start : end;
  }

  function closestOnEllipse(point, entity, path) {
    const nearby = nearestSegment(point, path);
    if (!nearby) return null;
    const conic = snapConic(entity);
    const span = Math.abs(entity.sweep);
    const progress = positiveAngle(Math.sign(entity.sweep) * (angle(conic.local(nearby.point)) - entity.startAngle));
    const step = 6 / span;
    const guess = Math.min(1, progress / span);
    const full = span >= 360 - EPSILON;
    let left = full ? guess - step : Math.max(0, guess - step);
    let right = full ? guess + step : Math.min(1, guess + step);
    for (let i = 0; i < 36; i++) {
      const a = (2 * left + right) / 3;
      const b = (left + 2 * right) / 3;
      if (distance(point, ellipsePoint(entity, a)) <= distance(point, ellipsePoint(entity, b))) right = b;
      else left = a;
    }
    const candidates = [ellipsePoint(entity, (left + right) / 2), path[0], path.at(-1)];
    return candidates.map(pointOnEdge => ({ point: pointOnEdge, miss: distance(point, pointOnEdge) }))
      .reduce((first, second) => first.miss <= second.miss ? first : second);
  }

  function closestPolylineEdge(entity, point, maximum) {
    let found = null;
    let miss = maximum;
    const count = entity.points.length - Number(!entity.closed);
    for (let index = 0; index < count; index++) {
      const { start, end, arc } = polylineEdge(entity, index);
      const nearest = arc ? closestOnArc(point, arc) : projectToSegment(point, start, end);
      const gap = distance(point, nearest);
      if (gap <= miss && (!found || gap < miss)) {
        found = { index, point: nearest, miss: gap };
        miss = gap;
      }
    }
    return found;
  }

  function hit(point, accepted = () => true, tolerance = 8 / scale, preferNotch = true, preciseEllipse = false) {
    let miss = tolerance;
    let found = null;
    for (const entity of current.entities) {
      if (!accepted(entity)) continue;
      const { path, bounds } = geometry(entity);
      const [x, y] = point;
      if (x < bounds[0] - miss || x > bounds[2] + miss || y < bounds[1] - miss || y > bounds[3] + miss) continue;
      let candidate;
      if (entity.type === "point") {
        candidate = { miss: distance(point, entity.point), point: entity.point };
      } else if (entity.type === "circle" || entity.type === "arc") {
        const vector = sub(point, entity.center);
        const direction = angle(vector);
        const progress = Math.sign(entity.sweep) * (direction - entity.startAngle);
        if (entity.type === "circle" || positiveAngle(progress) <= Math.abs(entity.sweep)) {
          const nearest = add(entity.center, radial(direction, entity.radius));
          candidate = { miss: distance(point, nearest), point: nearest };
        } else {
          candidate = nearestSegment(point, path);
        }
      } else if (entity.type === "polyline") {
        candidate = closestPolylineEdge(entity, point, miss);
      } else if (entity.type === "ellipse" && preciseEllipse) {
        candidate = closestOnEllipse(point, entity, path);
      } else {
        candidate = nearestSegment(point, path);
      }
      if (candidate && candidate.miss <= miss && !(preferNotch && found?.entity.notch && !entity.notch && candidate.miss === miss)) {
        miss = candidate.miss;
        found = { entity, point: candidate.point, edge: candidate.index };
      }
    }
    return found;
  }

  function pickAt(point) {
    if (tool === "notch-edit") {
      const parentId = current.entities.find(entity => entity.id === notchTarget)?.parentId;
      return hit(point, entity => editableNotch(entity) || notchTarget && (parentId ? entity.id === parentId : cuttingEdge(entity)));
    }
    if (tool.startsWith("notch")) return hit(point, entity => !entity.notch && entity.type !== "point");
    return hit(point);
  }

  const editableNotch = entity => entity.notch && ["line", "polyline"].includes(entity.type) && !entity.closed;
  const cuttingEdge = entity => !entity.notch && entity.type !== "point";

  function position(raw, ignoredId, bypass = false, shift = false, anchor = pending.at(-1)) {
    snap = null;
    snapType = "";
    guideAxes = [];
    if (bypass) return raw;
    const tolerance = 10 / scale;
    const locked = anchor && tool !== "rectangle" && tool !== "square" && (shift || fields.ortho.checked);
    const axis = locked ? Number(Math.abs(raw[0] - anchor[0]) > Math.abs(raw[1] - anchor[1])) : -1;
    const restrict = point => locked ? point.map((value, index) => index === axis ? anchor[index] : value) : point;
    if (!fields.tracking.checked) trackingOrigin = null;
    let best = tolerance;
    let guideCandidate = null;
    let guideDistance = tolerance;
    const consider = (candidate, type) => {
      if (locked && Math.abs(candidate[axis] - anchor[axis]) > CAD_POINT_EPSILON) return;
      const gap = distance(raw, candidate);
      if (gap < best || snap && Math.abs(gap - best) <= EPSILON && type === "Середина" && snapType !== type) {
        best = gap;
        snap = restrict(candidate);
        snapType = type;
      }
    };
    if (fields.objects.checked || fields.tracking.checked) {
      for (const entity of current.entities) {
        if (entity.id === ignoredId) continue;
        const cached = geometry(entity);
        cached.snaps ??= snapPoints(entity, cached.path);
        for (const { point, type } of cached.snaps) {
          if (fields.objects.checked) consider(point, type);
          if (fields.tracking.checked) {
            const miss = distance(raw, point);
            if (miss < guideDistance) {
              guideDistance = miss;
              guideCandidate = point;
            }
          }
        }
      }
      if (fields.objects.checked) {
        for (const candidate of [...pending, [0, 0]]) consider(candidate, "Узел");
      }
      if (guideCandidate) trackingOrigin = guideCandidate;
    }
    if (fields.crossings.checked) nearbyCrossings(raw, tolerance, ignoredId, consider);
    if (snap) {
      if (fields.tracking.checked && (!trackingOrigin || distance(trackingOrigin, snap) > EPSILON)) trackingOrigin = snap;
      return snap;
    }
    if (anchor && fields.perpendicular.checked) {
      for (const entity of current.entities) {
        if (entity.id === ignoredId || entity.notch) continue;
        const points = entity.type === "line" ? [entity.start, entity.end] : entity.type === "polyline" ? entity.points : null;
        if (!points) continue;
        for (let index = 0; index < points.length - Number(!entity.closed); index++) {
          if (entity.bulges?.[index]) continue;
          const a = points[index];
          const b = points[(index + 1) % points.length];
          const vector = sub(b, a);
          const fraction = dot(sub(anchor, a), vector) / dot(vector, vector);
          if (fraction > 0 && fraction < 1) consider(mix(a, b, fraction), "Перпендикуляр");
        }
      }
      if (snap) return snap;
    }
    let point = raw;
    if (!locked && fields.tracking.checked && trackingOrigin) {
      guideAxes = [0, 1].filter(axis => Math.abs(raw[axis] - trackingOrigin[axis]) < tolerance);
      if (guideAxes.length) {
        snap = raw.map((value, axis) => guideAxes.includes(axis) ? trackingOrigin[axis] : value);
        snapType = "Направляющая";
        return snap;
      }
    }
    if (fields.edges.checked) {
      const edge = hit(raw, entity => entity.id !== ignoredId && !entity.notch && entity.type !== "point", tolerance, false, true);
      if (edge) {
        consider(edge.point, "Ребро");
        if (snap) return snap;
      }
    }
    if (fields.grid.checked) {
      const step = gridStep();
      point = point.map(value => Math.round(value / step) * step);
    }
    if (locked) {
      guideAxes = [axis];
      const result = restrict(point);
      snap = result;
      snapType = axis ? "Горизонталь" : "Вертикаль";
      return result;
    }
    if (fields.grid.checked) {
      snap = point;
      snapType = "Сетка";
    }
    return point;
  }

  function directDistance() {
    if (!pending.length || gesture || running || offsetPreview || notchPreview) return null;
    const anchor = pending.at(-1);
    if (tool === "rectangle") return { anchor };
    const label = DIRECT_DISTANCE_LABELS[tool];
    if (label) return { anchor, label };
    if (tool === "square") return { anchor, label: "Сторона" };
    if (tool === "ellipse") return pending.length === 1
      ? { anchor, label: "Первая полуось" } : { anchor: pending[0], label: "Вторая полуось" };
    return null;
  }

  function resetDirectDistance() {
    distanceInput = "";
    rectangleAxis = 0;
    rectangleSizes = ["", ""];
    rectangleEditing = false;
    rectangleDirection = null;
    directionPoint = null;
    directionStart = null;
  }

  function typedDistancePoint() {
    const entry = directDistance();
    if (!entry) return null;
    const { anchor } = entry;
    if (tool === "rectangle") {
      if (!rectangleSizes.some(Boolean)) return null;
      const delta = sub(directionPoint || anchor, anchor);
      const sides = rectangleSizes.map((value, index) => value
        ? ((delta[index] || rectangleDirection?.[index] || 1) < 0 ? -1 : 1) * validSize(value) : delta[index]);
      return sides.every(Number.isFinite) ? add(anchor, sides) : null;
    }
    const size = validSize(distanceInput);
    if (!Number.isFinite(size)) return null;
    const vector = directionPoint ? sub(directionPoint, anchor) : [1, 0];
    const length = norm(vector);
    if (tool === "square") return add(anchor, vector.map(value => value < 0 ? -size : size));
    if (tool === "ellipse" && pending.length === 2) {
      const axis = sub(pending[1], anchor);
      const axisLength = norm(axis);
      if (axisLength <= CAD_POINT_EPSILON) return null;
      const side = cross(axis, vector) < 0 ? -1 : 1;
      return add(anchor, mul([-axis[1], axis[0]], side * size / axisLength));
    }
    return add(anchor, length > EPSILON ? mul(vector, size / length) : [size, 0]);
  }

  function validSize(value) {
    const size = Number(value.replace(",", "."));
    return size > CAD_POINT_EPSILON && size <= MAX_COORDINATE ? size : NaN;
  }

  function drawDirectDistance() {
    const entry = directDistance();
    const output = views["distance-entry"];
    const rectangle = tool === "rectangle";
    const hidden = !entry || !(rectangle ? directionPoint || rectangleEditing : distanceInput);
    if (output.hidden !== hidden) output.hidden = hidden;
    if (output.hidden) return;
    let value;
    if (rectangle) {
      const live = sub(directionPoint || entry.anchor, entry.anchor).map(Math.abs);
      value = RECTANGLE_SIDES.map((name, index) => {
        const value = rectangleSizes[index] || (rectangleEditing && rectangleAxis === index ? "…" : format(live[index]));
        return `${name}${rectangleEditing && rectangleAxis === index ? ` [${value}]` : ` ${value}`}`;
      }).join(" × ") + " мм";
    } else {
      value = `${entry.label} ${distanceInput} мм`;
    }
    if (views["distance-value"].textContent !== value) views["distance-value"].textContent = value;
    const keys = rectangle ? "Tab · Enter" : "Enter";
    if (views["distance-key"].textContent !== keys) views["distance-key"].textContent = keys;
    const [x, y] = lastPointer?.at || screen(entry.anchor);
    output.style.setProperty("--pointer-x", `${x}px`);
    output.style.setProperty("--pointer-y", `${y}px`);
    output.toggleAttribute("data-flip-x", x > width / 2);
  }

  function handleDirectDistance(event, key) {
    if (!directDistance() || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return false;
    if (tool === "rectangle") return handleRectangleDistance(event, key);
    if (/^\d$/.test(key) || key === "." || key === ",") {
      if (distanceInput.length < 16 && (/^\d$/.test(key) || !/[.,]/.test(distanceInput))) distanceInput += key;
      log();
      draw();
      return true;
    }
    if (!distanceInput) return false;
    if (key === "backspace") {
      distanceInput = distanceInput.slice(0, -1);
      draw();
    } else if (key === "escape") {
      distanceInput = "";
      log();
      draw();
    } else if (key === "enter") {
      if (event.repeat) return true;
      const point = typedDistancePoint();
      if (!point) {
        log("Укажите направление курсором и введите положительный размер в миллиметрах.", true);
      } else {
        resetDirectDistance();
        operate(point);
      }
    } else return false;
    return true;
  }

  function handleRectangleDistance(event, key) {
    const isDigit = /^\d$/.test(key);
    const isSeparator = key === "." || key === ",";
    if (isDigit || isSeparator) {
      rectangleEditing = true;
      const input = rectangleSizes[rectangleAxis];
      if (input.length < 16 && (isDigit || !/[.,]/.test(input))) rectangleSizes[rectangleAxis] += key;
      log();
    } else if (key === "tab" && rectangleEditing) {
      rectangleAxis = 1 - rectangleAxis;
    } else if (key === "backspace" && rectangleEditing) {
      rectangleSizes[rectangleAxis] = rectangleSizes[rectangleAxis].slice(0, -1);
    } else if (key === "escape" && rectangleEditing) {
      rectangleAxis = 0;
      rectangleSizes = ["", ""];
      rectangleEditing = false;
      log();
    } else if (key === "enter" && rectangleEditing) {
      if (event.repeat) return true;
      if (rectangleAxis === 0 && rectangleSizes[0] && !rectangleSizes[1]) {
        if (!Number.isFinite(validSize(rectangleSizes[0]))) log("Введите положительный размер стороны A.", true);
        else rectangleAxis = 1;
      } else {
        const point = typedDistancePoint();
        if (point && sub(point, pending[0]).every(side => Math.abs(side) >= CAD_POINT_EPSILON)) operate(point);
        else log("Задайте положительные размеры сторон A и B.", true);
      }
    } else return false;
    draw();
    return true;
  }

  function creation(points, closed, provisional = false) {
    const [start, middle, end] = points;
    const common = { type: tool, layer: "0" };
    const polygon = corners => ({ ...common, type: "polyline", points: corners, closed: true });
    switch (tool) {
      case "point": return start && { ...common, point: start, reference: true };
      case "line":
        return middle && distance(start, middle) > EPSILON ? { ...common, start, end: middle } : null;
      case "circle":
        return middle && distance(start, middle) > EPSILON ? { ...common, center: start, radius: distance(start, middle) } : null;
      case "arc": {
        const arc = end && arcThrough(start, middle, end);
        return arc && { ...common, ...arc };
      }
      case "rectangle":
      case "square": {
        if (!middle) return null;
        let [dx, dy] = sub(middle, start);
        if (tool === "square") {
          const side = Math.max(Math.abs(dx), Math.abs(dy));
          dx = (dx < 0 ? -1 : 1) * side;
          dy = (dy < 0 ? -1 : 1) * side;
        }
        if (Math.min(Math.abs(dx), Math.abs(dy)) < CAD_POINT_EPSILON) return null;
        const [x, y] = start;
        return polygon([start, [x + dx, y], [x + dx, y + dy], [x, y + dy]]);
      }
      case "triangle": {
        if (!end || Math.abs(cross(sub(middle, start), sub(end, start))) <= CAD_POINT_EPSILON ** 2) return null;
        return polygon([start, middle, end]);
      }
      case "polygon": {
        const sides = numberFrom(fields.option);
        if (!middle || !Number.isInteger(sides) || sides < 3 || sides > 100) return null;
        const vector = sub(middle, start);
        const radius = norm(vector);
        if (radius < CAD_POINT_EPSILON) return null;
        const firstAngle = angle(vector);
        return polygon(Array.from({ length: sides }, (_, index) => add(start, radial(firstAngle + 360 * index / sides, radius))));
      }
      case "ellipse": {
        if (!end) return null;
        const axis = sub(middle, start);
        const radius = norm(axis);
        if (radius < CAD_POINT_EPSILON) return null;
        const minor = cross(axis, sub(end, start)) / radius;
        if (Math.abs(minor) < CAD_POINT_EPSILON) return null;
        const other = mul([-axis[1], axis[0]], minor / radius);
        const swapped = Math.abs(minor) > radius;
        return { ...common, center: start, axis: swapped ? other : axis, ratio: swapped ? radius / Math.abs(minor) : Math.abs(minor) / radius, startAngle: 0, sweep: 360 };
      }
      case "polyline":
        if (points.length < (closed ? 3 : 2)) return null;
        return { ...common, points, closed };
      case "spline":
        if (points.length < (closed ? 3 : 2)) return null;
        return { ...common, fitPoints: points, fitMethod: "centripetal", closed };
      default:
        if (provisional) return null;
        throw new Error("Неизвестный инструмент построения.");
    }
  }

  function build(points, closed = false) {
    const entity = creation(points, closed);
    if (!entity) {
      log(({ arc: "Укажите три точки, лежащие вне одной прямой.", triangle: "Вершины треугольника должны лежать вне одной прямой.",
        polygon: "Укажите разные точки и целое число сторон от 3 до 100.", ellipse: "Укажите две ненулевые полуоси эллипса.",
      })[tool] || "Укажите разные точки.", true);
      return;
    }
    entity.id = newId();
    pending = [];
    resetDirectDistance();
    snap = cursor = null;
    commit([...current.entities, entity], { ids: [entity.id] });
  }

  function operate(point) {
    if (running || offsetPreview || notchPreview || !point.every(Number.isFinite)) return;
    log();
    if (point.some(value => Math.abs(value) > MAX_COORDINATE)) {
      log("Координаты должны быть в пределах ±1 000 000 000 мм.", true);
      return;
    }
    resetDirectDistance();
    cursor = null;
    if (tool === "measure") {
      if (pending.length) {
        if (distance(pending[0], point) <= CAD_POINT_EPSILON) {
          log("Укажите две разные точки.", true);
          return;
        }
        measurements.push([pending[0], point]);
        pending = [];
      } else pending = [point];
      update();
      return;
    }
    if (tool in DRAW) {
      if (pending.length && distance(point, pending.at(-1)) <= CAD_POINT_EPSILON) {
        log("Эта точка совпадает с предыдущей.", true);
        return;
      }
      const multiPoint = tool === "polyline" || tool === "spline";
      if (multiPoint && pending.length >= 3 && distance(point, pending[0]) < CAD_POINT_EPSILON) {
        build(pending, true);
        return;
      }
      const next = [...pending, point];
      if (!multiPoint && next.length === DRAW[tool].points.length) build(next);
      else {
        pending = next;
        update();
      }
      return;
    }
    if (tool === "notch-edit") {
      const mark = hit(point, editableNotch)?.entity;
      if (mark && mark.id !== notchTarget) {
        notchTarget = mark.id;
        placingNotch = false;
        selected = new Set([mark.id]);
        const path = geometry(mark).path;
        fields.option.value = format(distance(path[0], path.at(-1)));
        update();
        openParameters();
      } else if (notchTarget && placingNotch) {
        const parentId = current.entities.find(entity => entity.id === notchTarget)?.parentId;
        const target = hit(point, entity => parentId ? entity.id === parentId : cuttingEdge(entity));
        if (target) updateNotch(target.point, parentId ? null : target.entity.id);
        else log(parentId ? "Укажите новое место на том же контуре." : "Укажите кромку для привязки надсечки.", true);
      } else log("Выберите надсечку.", true);
      return;
    }
    if (tool === "trim" || tool === "notch" || tool === "notch-series") {
      const target = hit(point, entity => !entity.notch && entity.type !== "point" && (tool !== "trim" || selected.has(entity.id)));
      if (!target) {
        log(tool === "trim" ? "Укажите участок на выбранной геометрии." : "Укажите точку на кромке детали.", true);
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
        const options = { id: target.entity.id, point: target.point, depth };
        if (tool === "notch-series") {
          const count = fields["notch-count"].valueAsNumber;
          if (!Number.isInteger(count) || count < 1 || count > 100) {
            log("Введите целое количество надсечек от 1 до 100.", true);
            return;
          }
          Object.assign(options, { count, scope: fields["notch-scope"].value });
        }
        void editOnServer(tool, options);
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
      const { entities, warnings = [], selectedIds = [], changed } = result;
      if (operation === "offset") {
        offsetPreview = { entities, original: options.id, ids: selectedIds, warnings };
        fields.cut.value = "new";
      } else if (operation === "notch-series") {
        notchPreview = { entities, ids: selectedIds, warnings };
      } else if (operation === "trim-quick") {
        if (changed) commit(entities, { warnings: [...current.warnings, ...warnings], ids: [] });
      } else if (operation === "repair-part") {
        if (changed) commit(entities, { ids: selectedIds });
        log(warnings[0]?.message || warnings[0] || "Контур проверен.");
      } else {
        choose("select");
        commit(entities, { warnings: [...current.warnings, ...warnings], ids: selectedIds });
        if (operation === "join") log(warnings[0]?.message || warnings[0] || "Контур соединён.");
      }
    });
  }

  function updateNotch(point, parentId) {
    const depth = numberFrom(fields.option);
    if (notchTarget && depth > 0) void editOnServer("notch-edit", { id: notchTarget, depth, ...(point ? { point } : {}), ...(parentId ? { parentId } : {}) });
    else log("Введите положительную глубину надсечки.", true);
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
    commit(current.entities.map(entity => ids.has(entity.id) && entity.type !== "point" ? { ...entity, reference } : entity));
  }

  function removeSelected() {
    const ids = familyIds(selected);
    if (!ids.size || running || offsetPreview || notchPreview) return;
    choose(tool === "erase" ? "erase" : "select");
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

  function updateGrip(raw, bypass = false, shift = false) {
    const origin = controls(current.entities.find(entity => entity.id === gesture.id))[gesture.index];
    gesture.entities = changeGrip(gesture, position(raw, gesture.id, bypass, shift, origin));
  }

  function onPointerDown(event) {
    if (!event.isPrimary && event.pointerType !== "mouse") return;
    if (gesture || ![0, 1, 2].includes(event.button)) return;
    flushPointerMove();
    canvas.focus({ preventScroll: true });
    const start = screenEvent(event);
    if (event.button === 1 || spacePressed) {
      event.preventDefault();
      gesture = { type: "pan", start, center, pointerId: event.pointerId };
    } else if (event.button === 0 && !running && !offsetPreview && !notchPreview) {
      const point = world(start);
      if (tool === "scale") {
        const handle = scaleBox && scaleHandleAt(start);
        if (!handle) return;
        scaleSide = handle;
        fields.option.value = format(Math.abs(handle.point[handle.axis] - handle.origin[handle.axis]));
        gesture = { type: "scale", start, pointerId: event.pointerId };
        update();
      } else if (tool !== "select" && tool !== "erase") {
        const pickedPoint = tool === "trim" || tool.startsWith("notch") ? point : position(point, null, event.altKey, event.shiftKey);
        if (tool === "rectangle" && pending.length && rectangleEditing) {
          cursor = pickedPoint;
          updateDistanceDirection(point, event.shiftKey);
          const exact = typedDistancePoint();
          if (exact) operate(exact);
          else if (!rectangleSizes.some(Boolean)) operate(pickedPoint);
          else log("Задайте положительные размеры сторон A и B.", true);
        } else if (tool === "move" && pending.length && distanceInput) {
          cursor = pickedPoint;
          updateDistanceDirection(point, event.shiftKey);
          operate(typedDistancePoint() || pickedPoint);
        } else operate(pickedPoint);
        if (directDistance()) directionStart = point;
        return;
      } else if (tool === "erase" && !event.shiftKey) {
        gesture = { type: "erase", start, points: [point], pointerId: event.pointerId };
        cursor = point;
        log();
        draw();
      } else {
        const selection = gripOwner();
        const grip = selection && Array.from(selectedGrips(selection)).find(([, p]) => distance(screen(p), start) <= 7);
        if (!event.shiftKey && !event.altKey && grip) {
          gesture = { type: "grip", id: selection.id, index: grip[0], start, pointerId: event.pointerId };
        } else {
          gesture = { type: "marquee", start, end: start, shift: event.shiftKey, pointerId: event.pointerId };
        }
      }
    }
    if (gesture) {
      canvas.setPointerCapture(event.pointerId);
      canvas.dataset.gesture = gesture.type;
    }
  }

  function addErasePoint(points, point) {
    if (distance(points.at(-1), point) < 3 / scale) return;
    if (points.length === 256) points.splice(0, points.length, ...points.filter((_, index) => index % 2 === 0));
    points.push(point);
  }

  function onPointerMove(event) {
    if (gesture && gesture.pointerId !== event.pointerId) return;
    if (gesture?.type === "erase") {
      movePointer(event);
      return;
    }
    pendingPointer = event;
    if (pointerFrame === null) pointerFrame = window.requestAnimationFrame(flushPointerMove);
  }

  function clearPointerMove() {
    if (pointerFrame !== null) window.cancelAnimationFrame(pointerFrame);
    pointerFrame = pendingPointer = null;
  }

  function flushPointerMove() {
    const event = pendingPointer;
    clearPointerMove();
    if (event) movePointer(event);
  }

  function movePointer(event) {
    if (gesture && gesture.pointerId !== event.pointerId) return;
    const at = screenEvent(event);
    const point = world(at);
    lastPointer = { at, alt: event.altKey };
    if (gesture?.type === "pan") {
      const [x, y] = sub(at, gesture.start);
      center = add(gesture.center, [-x / scale, y / scale]);
    } else if (gesture?.type === "marquee") {
      gesture.end = at;
    } else if (gesture?.type === "erase") {
      addErasePoint(gesture.points, point);
    } else if (gesture?.type === "scale") {
      if (distance(gesture.start, at) > 3) updateScaleGesture(point, event.shiftKey);
      else gesture.parameters = null;
    } else if (gesture?.type === "grip") {
      updateGrip(point, event.altKey, event.shiftKey);
    }
    const location = world(at);
    const picking = tool === "select" || tool === "trim" || tool.startsWith("notch") || tool === "erase" && event.shiftKey;
    hover = !gesture && !offsetPreview && !notchPreview && picking ? pickAt(location)?.entity : null;
    if ((picking || tool === "erase" || tool === "scale") && gesture?.type !== "grip") {
      cursor = location;
      snap = null;
      snapType = "";
      guideAxes = [];
    } else if (gesture?.type !== "grip") cursor = position(location, null, event.altKey, event.shiftKey);
    else cursor = snap || location;
    updateDistanceDirection(location, event.shiftKey);
    draw();
  }

  function updateDistanceDirection(raw, shift) {
    const entry = directDistance();
    if (!entry || !cursor) return;
    const base = directionStart || entry.anchor;
    if (distance(raw, base) * scale <= 2) return;
    const { anchor } = entry;
    directionPoint = cursor;
    if (tool === "rectangle") {
      rectangleDirection = sub(raw, anchor);
      return;
    }
    if (distance(cursor, anchor) <= EPSILON && distance(raw, anchor) > EPSILON) {
      const [dx, dy] = sub(raw, anchor);
      if (shift || fields.ortho.checked) directionPoint = Math.abs(dx) >= Math.abs(dy)
        ? [raw[0], anchor[1]] : [anchor[0], raw[1]];
      else directionPoint = raw;
    }
  }

  function cancelGesture() {
    if (!gesture) return;
    const { pointerId } = gesture;
    gesture = null;
    delete canvas.dataset.gesture;
    if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
  }

  function selectHit(item, shift, segment) {
    const id = item?.entity.id;
    const wasWhole = selected.has(id);
    const edges = selectedEdges.get(id);
    const repeated = edges?.has(item.edge);
    if (!shift) {
      selected.clear();
      selectedEdges.clear();
    }
    if (!item) return;
    if (shift && wasWhole && !segment) {
      selected.delete(id);
    } else if (tool !== "select" || item.edge == null || item.entity.notch
        || !segment && (item.entity.joined || wasWhole || repeated)) {
      selectedEdges.delete(id);
      selected.add(id);
    } else {
      selected.delete(id);
      const next = shift && edges || new Set();
      next.add(item.edge);
      selectedEdges.set(id, next);
    }
  }

  function completeGesture(event, cancelled = false) {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    if (cancelled) clearPointerMove();
    else flushPointerMove();
    const completed = gesture;
    const end = screenEvent(event);
    const dragged = distance(completed.start, end) > 3;
    if (!cancelled && completed.type === "grip" && dragged) updateGrip(world(end), event.altKey, event.shiftKey);
    if (!cancelled && completed.type === "scale" && dragged) updateScaleGesture(world(end), event.shiftKey);
    cancelGesture();
    if (completed.type === "scale") {
      if (cancelled || dragged) scaleSide = null;
      update();
      if (cancelled) return;
      if (dragged) applyScale(completed.parameters);
      else openParameters();
      return;
    }
    if (!cancelled && completed.type === "erase") {
      addErasePoint(completed.points, world(end));
      void editOnServer("trim-quick", { stroke: completed.points, tolerance: 8 / scale });
      return;
    }
    if (!cancelled && completed.type === "grip" && dragged && completed.entities) {
      commit(completed.entities);
      return;
    }
    if (!cancelled && completed.type === "marquee") {
      if (dragged) {
        if (!completed.shift) {
          selected.clear();
          selectedEdges.clear();
        }
        const box = geometryBounds([world(completed.start), world(end)]);
        const crossing = end[0] < completed.start[0];
        for (const entity of current.entities) {
          if (!enclosed(geometry(entity).path, box, crossing)) continue;
          selectedEdges.delete(entity.id);
          selected.add(entity.id);
        }
      } else {
        selectHit(hit(world(end)), completed.shift, event.altKey);
      }
    }
    update();
  }

  const actionHandlers = {
    back: () => { if (!running) onBack(); },
    tools: () => showTools(toolsPanel.hidden),
    "cancel-parameters": cancelParameters,
    "relocate-notch": () => submitParameters(true),
    import: () => fields.file.click(),
    save: () => perform(async () => {
      const name = fields.name.value.trim();
      if (!name) throw new Error("Введите название чертежа.");
      const result = await onSaveDocument({ name, ...current });
      if (lifecycle.signal.aborted) return;
      saved = current;
      savedName = name;
      fields.name.value = name;
      log(layoutPart ? "Деталь обновлена в раскладке." : "Чертёж сохранён.");
      if (result?.closeEditor) await onBack();
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
    "erase-selection": removeSelected,
    "repair-part": () => editOnServer("repair-part", { ids: [...new Set([...selected, ...selectedEdges.keys()])] }),
    "remove-measurement": () => { measurements.pop(); update(); },
    "clear-measurements": () => { measurements.length = 0; pending = []; update(); },
    "apply-notches": () => {
      if (!notchPreview) return;
      const { entities, warnings, ids } = notchPreview;
      notchPreview = null;
      choose("select");
      commit(entities, { warnings: [...current.warnings, ...warnings], ids });
    },
    "cancel-notches": () => { notchPreview = null; update(); },
    undo: () => undo(previous, future),
    redo: () => undo(future, previous),
    join: () => editOnServer("join", { ids: joinIds() }),
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
    if (actionHandlers[key]) button.addEventListener("click", actionHandlers[key], listener);
  }
  for (const [key, button] of Object.entries(buttons)) {
    button.addEventListener("click", () => {
      choose(key);
      if (Object.hasOwn(TOOL_PARAMETERS, key) && TOOL_PARAMETERS[key][3] !== undefined) {
        fields.option.value = TOOL_PARAMETERS[key][3];
        openParameters();
      }
    }, listener);
  }
  root.addEventListener("pointerdown", event => {
    if (!toolsPanel.hidden && !toolsPanel.contains(event.target) && !actions.tools.contains(event.target)) showTools(false);
  }, listener);
  const shortcutControls = { ...buttons, ...Object.fromEntries(["import", "save", "part", "join", "undo", "redo", "fit", "reference"]
    .filter(key => !actions[key].hidden).map(key => [key, actions[key]])) };
  const defaultKeys = { select: "V", line: "L", polyline: "P", circle: "C", arc: "A", spline: "S", point: "Q",
    rectangle: "R", square: "Shift+R", triangle: "T", polygon: "G", ellipse: "O", move: "M", copy: "Shift+C",
    rotate: "Shift+T", scale: "K", mirror: "Shift+M", trim: "X", notch: "N", "notch-series": "Shift+N", "notch-edit": "Alt+N",
    join: "J", offset: "F", erase: "E", fit: "Z", measure: "D" };
  mountCadShortcuts(container, {
    signal: lifecycle.signal,
    commands: Object.entries(shortcutControls).map(([id, control]) => ({
      id, label: control.getAttribute("aria-label") || control.textContent.trim() || "Режущий / вспомогательный контур",
      defaultKey: defaultKeys[id],
      builtInKeys: { undo: ["Ctrl+Z"], redo: ["Ctrl+Y", "Ctrl+Shift+Z"] }[id],
    })),
    activate(id) {
      const control = shortcutControls[id];
      if (control.disabled || control.hidden || offsetPreview || notchPreview || running) return false;
      control.click();
      return true;
    },
  });
  fields.file.addEventListener("change", async () => {
    const file = fields.file.files[0];
    fields.file.value = "";
    if (!file) return;
    if (current.entities.length && !await confirmAction({
      title: "Заменить чертёж?", message: "Импорт заменит геометрию текущего чертежа.",
      confirmLabel: "Заменить", cancelLabel: "Отмена", signal: lifecycle.signal,
    })) return;
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
      measurements.length = 0;
      choose("select");
      commit(entities, { warnings, ids: [] });
      fit();
      views.warnings.open = warnings.length > 0;
      log("Импортировано объектов: " + entities.length);
    });
  }, listener);
  fields.cut.addEventListener("change", draw, listener);
  function submitParameters(relocate = false) {
    const value = numberFrom(fields.option);
    if (tool === "scale" && !(value > CAD_POINT_EPSILON && value <= MAX_COORDINATE)) {
      parameterError("Введите положительный размер в миллиметрах.");
    } else if (tool === "offset" && !(Math.abs(value) > CAD_POINT_EPSILON && Math.abs(value) <= MAX_COORDINATE)) {
      parameterError("Введите ненулевое смещение в миллиметрах.");
    } else if (tool === "rotate" && !Number.isFinite(value)) {
      parameterError("Введите угол поворота в градусах.");
    } else if (tool === "polygon" && !(Number.isInteger(value) && value >= 3 && value <= 100)) {
      parameterError("Введите целое число сторон от 3 до 100.");
    } else if (tool.startsWith("notch") && !(value > 0 && value <= MAX_COORDINATE)) {
      parameterError("Введите положительную глубину надсечки.");
    } else if (tool === "notch-series" && !fields["notch-count"].checkValidity()) {
      parameterError("Введите целое количество надсечек от 1 до 100.");
    } else {
      closeParameters();
      if (tool === "scale" && scaleSide) {
        applyScale(scaleParameters(scaleSide, value, fields.proportional.checked));
        scaleSide = null;
        update();
      } else if (tool === "offset" && selected.size === 1) {
        void editOnServer("offset", { id: selected.values().next().value, distance: value, keepOriginal: true });
      } else if (tool === "notch-edit" && !relocate) updateNotch();
      else {
        placingNotch = tool === "notch-edit" && relocate;
        if (tool === "rotate") log("Укажите центр поворота на чертеже.");
        if (tool.startsWith("notch")) log(placingNotch ? "Щёлкните по новой точке на кромке." : "Щёлкните по кромке детали.");
      }
    }
  }
  parameterDialog.addEventListener("keydown", event => event.stopPropagation(), listener);
  parameterDialog.addEventListener("cancel", event => {
    event.preventDefault();
    cancelParameters();
  }, listener);
  optionForm.addEventListener("submit", event => {
    event.preventDefault();
    submitParameters();
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
    flushPointerMove();
    cursor = snap = null;
    lastPointer = null;
    hover = null;
    guideAxes = [];
    draw();
  }, listener);
  canvas.addEventListener("contextmenu", event => {
    event.preventDefault();
    if (!running && !actions.finish.disabled && !views.finish.hidden) build(pending);
  }, listener);
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    if (gesture) return;
    flushPointerMove();
    const at = screenEvent(event);
    if (tool === "scale") lastPointer = { at, alt: event.altKey };
    const anchor = world(at);
    const delta = event.deltaY * (event.deltaMode === 1 ? 15 : 1);
    scale = Math.min(250, Math.max(.00001, scale * Math.exp(-delta * .0015)));
    center = add(center, sub(anchor, world(at)));
    draw();
  }, { ...listener, passive: false });
  function redrawOrthogonal(shift) {
    flushPointerMove();
    if (!lastPointer) return;
    const point = world(lastPointer.at);
    if (gesture?.type === "grip") {
      updateGrip(point, lastPointer.alt, shift);
      cursor = snap || point;
    } else if (gesture?.type === "scale" && gesture.parameters) {
      updateScaleGesture(point, shift);
    } else if (!gesture && pending.length && (tool in DRAW || tool in EDIT || tool === "measure")) {
      cursor = position(point, null, lastPointer.alt, shift);
      updateDistanceDirection(point, shift);
    } else return;
    draw();
  }
  canvas.addEventListener("blur", () => { spacePressed = false; lastPointer = null; }, listener);
  canvas.addEventListener("keydown", event => {
    if (event.key === "Shift" && !event.repeat) redrawOrthogonal(true);
  }, listener);
  canvas.addEventListener("keyup", event => {
    if (event.code === "Space") spacePressed = false;
    if (event.key === "Shift") redrawOrthogonal(event.shiftKey);
  }, listener);
  root.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.altKey || event.isComposing || !(event.ctrlKey || event.metaKey)
        || root.querySelector("dialog[open]") || !container.getClientRects().length) return;
    const target = event.target;
    if (target !== root.body && !container.contains(target)) return;
    if (target.closest?.("input, textarea, select, [role=textbox]") || target.isContentEditable) return;
    const key = event.code === "KeyZ" ? "z" : event.code === "KeyY" ? "y" : event.key.toLowerCase();
    if (key !== "z" && (key !== "y" || event.shiftKey)) return;
    event.preventDefault();
    event.stopPropagation();
    if (key === "y" || event.shiftKey) undo(future, previous);
    else undo(previous, future);
  }, { ...listener, capture: true });
  container.addEventListener("keydown", event => {
    if (event.target !== canvas || event.defaultPrevented) return;
    if (event.key !== "Shift") flushPointerMove();
    if (handleDirectDistance(event, event.key.toLowerCase())) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, { ...listener, capture: true });
  container.addEventListener("keydown", event => {
    const key = event.key.toLowerCase();
    if (key === "escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!toolsPanel.hidden) {
        showTools(false, true);
        return;
      }
      if (running) return;
      offsetPreview = null;
      notchPreview = null;
      choose("select");
      return;
    }
    if (event.target.matches("input, select")) return;
    if (event.target === canvas && event.code === "Space") {
      event.preventDefault();
      spacePressed = true;
    } else if (event.target === canvas && key === "delete") {
      event.preventDefault();
      removeSelected();
    } else if (event.target === canvas && key === "enter" && !actions.finish.disabled && !views.finish.hidden) {
      event.preventDefault();
      if (!event.repeat) build(pending);
    } else if (event.target === canvas && key === "enter" && tool === "erase") {
      event.preventDefault();
      removeSelected();
    }
  }, listener);

  const observer = new ResizeObserver(([entry]) => {
    const box = entry.contentRect;
    if (!box.width || !box.height) return;
    width = box.width;
    height = box.height;
    pixelRatio = root.defaultView.devicePixelRatio || 1;
    const backingWidth = Math.round(width * pixelRatio);
    const backingHeight = Math.round(height * pixelRatio);
    if (canvas.width !== backingWidth) canvas.width = backingWidth;
    if (canvas.height !== backingHeight) canvas.height = backingHeight;
    if (fitOnSize) {
      fitOnSize = false;
      fit();
    } else draw();
  });
  observer.observe(canvas);
  updateWarnings(current.warnings);
  update();
  canvas.focus({ preventScroll: true });
  return {
    hasUnsavedChanges: () => saved !== current || savedName !== fields.name.value.trim(),
    isBusy: () => running,
    destroy() {
      clearPointerMove();
      lifecycle.abort();
      observer.disconnect();
      container.classList.remove("cad-editor");
      container.replaceChildren();
    },
  };
}
