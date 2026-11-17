import { mountCadShortcuts } from "/designer-shortcuts.js";
import { confirmAction } from "/confirm-action.js";

const EPSILON = 1e-8;
const CAD_POINT_EPSILON = 0.001;
const CONTOUR_ENDPOINT_TOLERANCE_MM = 0.02;
const CONTOUR_EXACT_ENDPOINT_TOLERANCE_MM = 0.000001;
const MAX_COORDINATE = 1e9;
const MIN_VIEW_SCALE = 0.00001;
const MAX_VIEW_SCALE = 10_000_000;
const VIEW_ROUNDING_PX = .125;
const NATIVE_CANVAS_LIMIT_PX = 2 ** 18;
const CURVE_STROKE_TOLERANCE_PX = .2;
const SPLINE_TOLERANCE_MM = .01;
const SPLINE_TOLERANCE_PX = .35;
const MAX_SPLINE_REFINEMENT_POINTS = 16_384;
const MAX_GROUP_DEPTH = 32;
const CAD_SELECTED_COLOR = "#1688de";
const CAD_TRANSFORM_COLOR = "#6941b5";
const CAD_SNAP_COLOR = "#0869a7";
const CAD_NOTCH_COLOR = "#e45a08";
const REPAIR_DIAGNOSTICS = {
  branch: ["Развилка", "Снимите выделение с лишней ветви через Shift + щелчок и повторите восстановление."],
  gap: ["Разрыв контура", "Проверьте отмеченные концы и исправьте контур вручную."],
  intersection: ["Самопересечение", "Поправьте вершины у пересечения. Малую петлю можно убрать щелчком ластика по выступу."],
  overlap: ["Наложенные рёбра", "Проверьте рёбра через Alt + щелчок. Оставьте одно из совпадающих рёбер и повторите восстановление."],
};
const LEGACY_DEFAULT_UNITS_WARNING = "В чертеже не заданы единицы. Одна единица принята за 1 мм.";
const LEGACY_IMPORT_ISSUE = "Повреждение отмечено при импорте. Точная причина в этом чертеже не сохранена.";
const IMPORT_DOUBLED_LINE = "Замкнутый контур из двух совпадающих рёбер сохранён как открытый отрезок.";
const IMPORT_ANNOTATIONS = { DIMENSION: "Размеры", SOLID: "Заливки", TEXT: "Надписи", MTEXT: "Многострочные надписи",
  HATCH: "Штриховки", LEADER: "Выноски", MLEADER: "Мультивыноски", IMAGE: "Изображения" };
const DEG = Math.PI / 180;
const ROTATION_SNAP_STEP = 45;
const ARC_CONTOUR_SNAP_PX = 13;
const ARC_CUES = {
  symmetry: { color: "#a12679", label: "Симметрия от угла" },
  double: { color: "#a12679", label: "Касание двух сторон" },
  tangent: { color: "#087e83", label: "Касание контура" },
  semicircle: { color: "#6145a5", label: "Полукруг" },
};
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
const formatLength = value => value === 0 || value >= .01 ? `${format(value)} мм`
  : value >= .0001 ? `${format(value * 1e3)} мкм` : `${format(value * 1e6)} нм`;
const numberFrom = input => input.value.trim() ? Number(input.value.trim().replace(",", ".")) : NaN;
const maxViewScaleAt = ([x, y]) => Math.min(MAX_VIEW_SCALE,
  VIEW_ROUNDING_PX / (Math.max(1, Math.abs(x), Math.abs(y)) * Number.EPSILON));
const clampViewScale = (value, point) => Math.min(maxViewScaleAt(point), Math.max(MIN_VIEW_SCALE, value));

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
  if (entity.start && entity.end) {
    if (fraction === 0) return entity.start;
    if (fraction === 1) return entity.end;
    const reverse = fraction > .5;
    const start = reverse ? entity.end : entity.start;
    const end = reverse ? entity.start : entity.end;
    const portion = reverse ? 1 - fraction : fraction;
    const half = entity.sweep * DEG / (reverse ? -2 : 2);
    // Evaluate from the chord to avoid subtracting the distant center of a shallow arc.
    const factor = Math.sin(portion * half) / Math.sin(half);
    const turn = (portion - 1) * half;
    const [x, y] = sub(end, start);
    const cosine = factor * Math.cos(turn);
    const sine = factor * Math.sin(turn);
    return add(start, [x * cosine - y * sine, x * sine + y * cosine]);
  }
  return add(entity.center, radial(entity.startAngle + fraction * entity.sweep, entity.radius));
}

function ellipsePoint(entity, fraction) {
  const parameter = entity.startAngle + fraction * entity.sweep;
  const axis = entity.axis;
  const coefficients = radial(parameter, 1);
  return add(entity.center, add(mul(axis, coefficients[0]), mul([-axis[1], axis[0]], coefficients[1] * entity.ratio)));
}

function splinePoints(entity, tolerance = SPLINE_TOLERANCE_MM, limit = Infinity) {
  const depth = tolerance < SPLINE_TOLERANCE_MM ? 16 : 12;
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
      if (!sampleSplineSpan(result, evaluate, 0, 1, b, c, tolerance, limit, depth)) return null;
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
    if (!sampleSplineSpan(result, at, left, right, start, at(right), tolerance, limit, depth)) return null;
  }
  if (entity.closed && result.length && distance(result[0], result.at(-1)) > EPSILON) {
    if (result.length >= limit) return null;
    result.push(result[0]);
  }
  return result;
}

function sampleSplineSpan(result, evaluate, left, right, start, end, tolerance, limit, depth) {
  if (result.length >= limit) return false;
  const step = (right - left) / 4;
  const samples = [evaluate(left + step), evaluate(left + 2 * step), evaluate(right - step)];
  if (depth > 0 && samples.some(point => distance(point, projectToSegment(point, start, end)) > tolerance)) {
    const middle = (left + right) / 2;
    return sampleSplineSpan(result, evaluate, left, middle, start, samples[1], tolerance, limit, depth - 1)
      && sampleSplineSpan(result, evaluate, middle, right, samples[1], end, tolerance, limit, depth - 1);
  }
  result.push(end);
  return true;
}

function bulgeArc(start, end, bulge) {
  const chord = sub(end, start);
  if (norm(chord) < EPSILON || Math.abs(bulge) < EPSILON) return null;
  const center = add(mix(start, end, .5), mul([-chord[1], chord[0]], (1 - bulge ** 2) / (4 * bulge)));
  return { start, end, center, radius: distance(start, center), startAngle: angle(sub(start, center)), sweep: 4 * Math.atan(bulge) / DEG };
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
    const span = Math.abs(entity.sweep);
    const extremes = [0, 90, 180, 270]
      .map(degrees => positiveAngle(Math.sign(entity.sweep) * (degrees - entity.startAngle)))
      .filter(progress => progress <= span)
      .map(progress => arcPoint(entity, progress / span));
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

function contactEdge(contact) {
  if (!contact) return null;
  const { entity, edge } = contact;
  if (entity.type === "line") return entity;
  if (entity.type === "polyline" && Number.isInteger(edge) && edge >= 0 && edge < entity.points.length - Number(!entity.closed)) {
    return polylineEdge(entity, edge);
  }
  return null;
}

function symmetricArcEndpoint(startContact, candidateContact, tolerance) {
  const source = contactEdge(startContact);
  const target = contactEdge(candidateContact);
  if (!source || !target || source.arc || target.arc) return null;
  if (startContact.entity === candidateContact.entity) {
    if (startContact.entity.type !== "polyline") return null;
    const separation = Math.abs(startContact.edge - candidateContact.edge);
    if (separation !== 1 && !(startContact.entity.closed && separation === startContact.entity.points.length - 1)) return null;
  }
  const a = sub(source.end, source.start);
  const b = sub(target.end, target.start);
  const sourceLength = norm(a);
  const targetLength = norm(b);
  if (sourceLength <= EPSILON || targetLength <= EPSILON || Math.abs(cross(a, b)) <= EPSILON * sourceLength * targetLength) return null;
  for (const corner of [source.start, source.end]) {
    const fromStart = distance(corner, target.start) <= tolerance;
    if (!fromStart && distance(corner, target.end) > tolerance) continue;
    const along = distance(corner, startContact.point);
    if (along <= CAD_POINT_EPSILON || along > targetLength + EPSILON) continue;
    const from = fromStart ? target.start : target.end;
    const to = fromStart ? target.end : target.start;
    return { point: mix(from, to, Math.min(1, along / targetLength)), corner };
  }
  return null;
}

function arcFromBulge(start, end, height) {
  const chord = sub(end, start);
  const half = norm(chord) / 2;
  if (half <= EPSILON || !Number.isFinite(height) || Math.abs(height) <= EPSILON) return null;
  const offset = (height - half * (half / height)) / 2;
  const normal = mul([-chord[1], chord[0]], 1 / (2 * half));
  const center = add(mix(start, end, .5), mul(normal, offset));
  const radius = Math.hypot(half, offset);
  const startAngle = angle(sub(mul(chord, -.5), mul(normal, offset)));
  const sweep = -4 * Math.atan2(height, half) / DEG;
  if (!(radius <= MAX_COORDINATE) || !center.every(value => Math.abs(value) <= MAX_COORDINATE)
      || Math.abs(sweep) < 1e-9 || Math.abs(sweep) >= 360) return null;
  return { center, radius, startAngle, sweep };
}

function arcContactTangent(contact, point) {
  if (!contact || distance(contact.point, point) > CAD_POINT_EPSILON) return null;
  const { entity } = contact;
  const edge = contactEdge(contact);
  if (edge && !edge.arc) return sub(edge.end, edge.start);
  if (entity.type === "ellipse") {
    const [cosine, sine] = snapConic(entity).local(point);
    const [a, b] = entity.axis;
    return add(mul(entity.axis, -sine), mul([-b, a], cosine * entity.ratio));
  }
  if (edge?.arc || entity.type === "circle" || entity.type === "arc") {
    const [x, y] = sub(point, (edge?.arc || entity).center);
    return [-y, x];
  }
  return null;
}

function arcBulgeInferences(start, end, startContact, endContact) {
  const chord = sub(end, start);
  const length = norm(chord);
  if (length <= CAD_POINT_EPSILON) return [];
  const half = length / 2;
  const direction = mul(chord, 1 / length);
  // The center is on the chord bisector. Its radius to either contact is normal to the contour there.
  const centerOffset = (contact, point, side) => {
    const tangent = arcContactTangent(contact, point);
    if (!tangent) return null;
    const normal = cross(direction, tangent);
    return Math.abs(normal) > EPSILON * norm(tangent) ? side * half * dot(direction, tangent) / normal : null;
  };
  const a = centerOffset(startContact, start, -1);
  const b = centerOffset(endContact, end, 1);
  const candidates = [];
  const include = (height, kind) => {
    const tolerance = Math.min(CAD_POINT_EPSILON, EPSILON * Math.max(1, Math.abs(height)));
    if (!arcFromBulge(start, end, height) || candidates.some(candidate => Math.abs(candidate.height - height) <= tolerance)) return;
    candidates.push({ height, kind });
  };
  const includeTangents = (offset, kind) => {
    // Use the larger root first, and derive the other without subtracting nearly equal numbers.
    const major = offset + (offset < 0 ? -1 : 1) * Math.hypot(half, offset);
    include(major, kind);
    include(-half * (half / major), kind);
  };
  const centerTolerance = Math.min(CAD_POINT_EPSILON, EPSILON * Math.max(1, length));
  if (a != null && b != null && Math.abs(a - b) <= centerTolerance) {
    includeTangents((a + b) / 2, "double");
  } else {
    if (a != null) includeTangents(a, "tangent");
    if (b != null) includeTangents(b, "tangent");
  }
  include(half, "semicircle");
  include(-half, "semicircle");
  return candidates;
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
    case "arc": return [arcPoint(entity, 0), arcPoint(entity, .5), arcPoint(entity, 1)];
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
      const points = controls(entity);
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
  if (difference < 0 && (norm(closest) - 1) * distance(mix(a, b, fraction), curve.entity.center) > CAD_POINT_EPSILON) return [];
  const spread = Math.sqrt(Math.max(0, difference) / squared);
  return (spread ? [fraction - spread, fraction + spread] : [fraction]).filter(t => t >= -EPSILON && t <= 1 + EPSILON)
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
  arc: { label: "Дуга", points: ["Начало дуги", "Конец дуги", "Изгиб"] },
  spline: { label: "Сплайн по точкам", points: ["Начальная точка", "Следующая точка сплайна"] },
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
  triangle: "Расстояние до вершины", polygon: "Радиус",
  move: "Смещение", copy: "Смещение", measure: "Расстояние",
};
const RECTANGLE_SIDES = ["A", "B"];
const TOOL_PARAMETERS = {
  polygon: ["Правильный многоугольник", "Количество сторон", "Начать построение", "6"],
  notch: ["Надсечка", "Глубина, мм", "Выбрать кромку", "3"],
  "notch-series": ["Серия надсечек", "Глубина, мм", "Выбрать кромку", "3"],
  "notch-edit": ["Изменить надсечку", "Глубина, мм", "Изменить глубину"],
  scale: ["Масштабирование", "", "Применить"],
  "insert-points": ["Добавить несколько точек", "Число точек на выбранном участке", "Добавить точки"],
};
const MAX_INSERT_POINTS = 100;
const EDIT = {
  move: { label: "Перенести", points: ["Базовая точка", "Новая точка"] },
  copy: { label: "Копировать", points: ["Базовая точка", "Точка вставки"] },
  rotate: { label: "Повернуть", points: ["Центр поворота", "Исходное направление", "Новое направление"] },
  scale: { label: "Масштабировать" },
  mirror: { label: "Отразить", points: ["Начало оси", "Конец оси"] },
  notch: { label: "Надсечка", points: ["Точка на контуре"] },
  "notch-series": { label: "Серия надсечек", points: ["Точка на контуре"] },
  "notch-edit": { label: "Изменить надсечку", points: ["Точка на контуре"] },
};
const OBJECT_TRANSFORMS = new Set(["move", "copy", "rotate", "scale", "mirror"]);
const MIRROR_MODES = { points: "По двум точкам", edge: "По ребру", vertical: "Вертикаль", horizontal: "Горизонталь" };
const MIRROR_AXIS_WARNING = { points: "Укажите две разные точки оси.", edge: "Наведите на прямое ребро для отражения." };
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
  join: '<path d="M12 9V5H5a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h7m0-2h7a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-7v-4M8 12h8"/>',
  "close-outline": '<path d="M8 5h9l3 4v10H4V9l4-4"/><circle cx="4" cy="9" r="1.5"/><circle cx="8" cy="5" r="1.5"/>',
  ungroup: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="m10 14 4-4M12 7h3v3m-6 4v3h3"/>',
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
        ${iconButton("offset", "Припуск", "tool")}
        ${iconButton("erase", "Ластик", "tool")}
        <div class="cad-group-actions">
          <button type="button" class="button cad-icon-button cad-group-button" data-action="join" aria-label="Соединить в группу" title="Соединить в группу. При выборе ребра в группу войдёт весь его объект.">${cadIcon("join")}</button>
          <button type="button" class="button cad-icon-button cad-group-button" data-action="ungroup" aria-label="Разъединить" title="Разъединить выбранные группы на текущем уровне" hidden>${cadIcon("ungroup")}</button>
        </div>
        ${iconButton("close-outline", "Замкнуть контур", "action", "Объединить выделенные объекты в один замкнутый контур. При выборе ребра используется весь его объект.")}
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
      <div class="cad-context-menu" data-view="context-menu" role="menu" aria-label="Команды чертежа" hidden></div>
      <div class="cad-group-status" data-view="group-status" hidden>
        <span data-view="group-label"></span>
        <button type="button" class="button" data-action="exit-group">Выйти из группы</button>
      </div>
      <div class="cad-grid-status" data-view="scale"></div>
      <output class="cad-distance" data-view="distance-entry" hidden><span data-view="distance-value"></span><kbd data-view="distance-key">Enter</kbd></output>
      <details class="cad-warnings" data-view="warnings" hidden>
        <summary></summary><ul></ul>
      </details>
      <div class="cad-message" data-view="message" role="status" hidden></div>
      <aside class="cad-message cad-repair-diagnostics" data-view="repair-diagnostics" aria-label="Проверка контура" hidden>
        <div role="status">
          <strong data-view="repair-diagnostic-title"></strong>
          <p data-view="repair-diagnostic-help"></p>
          <p data-view="repair-diagnostic-result" hidden></p>
        </div>
        <div class="cad-warning-actions">
          <button class="button cad-warning-action" type="button" data-action="show-repair-location">Показать место</button>
          <button class="button cad-warning-action" type="button" data-action="fix-repair-location" disabled>Исправить</button>
          <button class="button cad-warning-action" type="button" data-action="fix-all-repair-locations" disabled>Исправить всё</button>
          <button class="button cad-warning-action" type="button" data-action="close-repair-diagnostics">Закрыть</button>
        </div>
      </aside>
      <div class="cad-preview-actions" data-view="notch-preview" hidden>
        <button class="button primary" type="button" data-action="apply-notches">Добавить надсечки</button>
        <button class="button" type="button" data-action="cancel-notches">Отмена</button>
      </div>
      <div class="cad-preview-actions" data-view="offset-preview" hidden>
        <label>Смещение, мм<input data-field="offset-distance" inputmode="decimal" maxlength="16"></label>
        <button class="button" type="button" data-action="offset-side" title="Сменить сторону смещения"></button>
        <span>Тип линии · Рез</span>
        <p data-view="preview-warning" role="status" hidden></p>
        <button class="button primary" type="button" data-action="apply">Применить</button>
        <button class="button" type="button" data-action="cancel-preview">Отмена</button>
      </div>
    </div>
  </div>
  <div class="cad-footer">
      <div class="cad-command" data-view="context" hidden>
        <div class="cad-command-fields cad-mirror-controls" data-view="mirror-controls" role="group" aria-label="Ось отражения" hidden>
          ${Object.entries(MIRROR_MODES).map(([mode, label]) => `<button type="button" class="button" data-action="mirror-${mode}" aria-pressed="false">${label}</button>`).join("")}
          <button type="button" class="button" data-action="mirror-cancel">Отмена</button>
        </div>
        <span class="cad-tool-step" data-view="tool-step" role="status" hidden></span>
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
        <button class="button" type="button" data-action="simplify" title="Убрать лишние вершины с отклонением до 0,02 мм" hidden>${cadIcon("polyline")}<span>Упростить контур</span></button>
        <button class="button cad-action-reference" type="button" data-action="reference" hidden>${cadIcon("reference")}<span></span></button>
      </div>
      <div class="cad-settings">
        <button type="button" class="button cad-action-tools" data-action="tools" aria-expanded="false">${cadIcon("tools")}<span>Привязки</span></button>
        <div class="cad-snaps" data-view="tools-panel" role="group" aria-label="Привязки" hidden>
          <strong>Привязки</strong>
          <label class="cad-checkbox" title="Привязка к узлам и серединам рёбер"><input type="checkbox" data-field="objects" checked> Узлы</label>
          <label class="cad-checkbox" title="Привязка к пересечениям"><input type="checkbox" data-field="crossings"> Пересечения</label>
          <label class="cad-checkbox" title="Привязка к ближайшей точке ребра. При удержании Shift ребро также может задать точку на оси."><input type="checkbox" data-field="edges"> Рёбра</label>
          <label class="cad-checkbox" title="Перпендикуляр к прямому ребру от последней точки"><input type="checkbox" data-field="perpendicular"> Перпендикуляр</label>
          <label class="cad-checkbox" title="Наведите на узел, ребро или кривую, чтобы продолжить построение по направляющей. От последней точки направляющая появляется автоматически."><input type="checkbox" data-field="tracking"> Направляющие</label>
          <label class="cad-checkbox" title="Округлять координаты до шага видимой сетки"><input type="checkbox" data-field="grid"> Сетка</label>
          <label class="cad-checkbox" title="Ограничить следующую точку горизонталью или вертикалью. Shift удерживает текущую ось до отпускания."><input type="checkbox" data-field="ortho"> Орто</label>
          <small>Метки при указании точки</small>
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
  const contextMenu = views["context-menu"];
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
  let byId = new Map(current.entities.map(entity => [entity.id, entity]));
  let activeGroup = [];
  let saved = initial;
  let savedName = draft?.name || "Новый чертёж";
  let selected = new Set();
  const selectedEdges = new Map();
  let gripLayout = null;
  let repairLocations = [];
  let repairSourceIds = [];
  let repairBulkChecked = false;
  let repairLocationIndex = 0;
  let repairLocationFocused = false;
  let insertPointsTarget = null;
  let tool = "select";
  let pending = [];
  let arcStartContact = null;
  let arcSymmetry = null;
  let arcBulges = [];
  let mirrorMode = "points";
  let mirrorEdge = null;
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
  let trackingSource = null;
  let anchorSource = null;
  let guideOrigin = null;
  let visibleGuide = null;
  let guideReference = null;
  let shiftGuide = null;
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

  function viewportGeometry(entity, cached) {
    if (entity.type !== "spline" || cached.path.length >= MAX_SPLINE_REFINEMENT_POINTS) return cached;
    const requested = Math.ceil(Math.log2(scale * SPLINE_TOLERANCE_MM / SPLINE_TOLERANCE_PX));
    const tier = Math.min(requested, cached.splineLimitTier ?? requested);
    if (tier <= 0) return cached;
    if (cached.splineTier !== tier) {
      const sample = detail => splinePoints(entity, SPLINE_TOLERANCE_MM / 2 ** detail, MAX_SPLINE_REFINEMENT_POINTS);
      let detail = tier;
      let path = sample(detail);
      if (!path) {
        // Keep the finest usable path when deep zoom exceeds the sampling budget.
        let lower = 0;
        let upper = tier;
        while (upper - lower > 1) {
          const middle = Math.floor((lower + upper) / 2);
          const candidate = sample(middle);
          if (candidate) { path = candidate; lower = middle; }
          else upper = middle;
        }
        detail = cached.splineLimitTier = lower;
      }
      cached.splineViewport = path ? { path, bounds: geometryBounds(path) } : cached;
      cached.splineTier = detail;
    }
    return cached.splineViewport;
  }

  function viewportPath(entity, cached) {
    return viewportGeometry(entity, cached).path;
  }

  function nearbyCrossings(raw, tolerance, ignoredId, consider) {
    const [x, y] = raw;
    const visible = (bounds, margin = 0) => bounds[0] <= x + tolerance + margin && bounds[2] >= x - tolerance - margin
      && bounds[1] <= y + tolerance + margin && bounds[3] >= y - tolerance - margin;
    const nearby = [];
    for (const entity of current.entities) {
      if (entity.id === ignoredId || entity.type === "point") continue;
      const source = geometry(entity);
      if (!visible(source.bounds, entity.type === "spline" ? SPLINE_TOLERANCE_MM : 0)) continue;
      const cached = viewportGeometry(entity, source);
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

  function snapAlongGuide(point, { direction }, tolerance, ignoredId, consider, type) {
    const span = mul(direction, tolerance);
    const guide = { a: sub(point, span), b: add(point, span) };
    const [left, bottom, right, top] = geometryBounds([guide.a, guide.b]);
    const overlaps = (bounds, margin = EPSILON) => bounds[0] <= right + margin && bounds[2] >= left - margin
      && bounds[1] <= top + margin && bounds[3] >= bottom - margin;
    for (const entity of current.entities) {
      if (entity.id === ignoredId || entity.notch || entity.type === "point") continue;
      const source = geometry(entity);
      if (!overlaps(source.bounds, entity.type === "spline" ? SPLINE_TOLERANCE_MM : EPSILON)) continue;
      const cached = viewportGeometry(entity, source);
      if (!overlaps(cached.bounds)) continue;
      cached.crossings ??= crossingSpans(entity, cached.path, cached.bounds);
      for (const edge of cached.crossings) {
        if (!overlaps(edge.bounds)) continue;
        if (!edge.curve && Math.abs(cross(sub(edge.a, point), direction)) <= EPSILON
            && Math.abs(cross(sub(edge.b, point), direction)) <= EPSILON) {
          consider(projectToSegment(point, edge.a, edge.b), type);
        } else {
          for (const target of snapCrossings(guide, edge)) consider(target, type);
        }
      }
    }
  }

  function newId() {
    return root.defaultView.crypto.randomUUID?.() || "cad-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  function selectedEntities() {
    return current.entities.filter(entity => selected.has(entity.id));
  }

  function selectedOutlines() {
    return [...new Set([...selected, ...selectedEdges.keys()])].map(id => byId.get(id))
      .filter(entity => entity && !entity.notch && entity.type !== "point");
  }

  function groupPath(entity) {
    return (byId.get(entity.parentId) || entity).groupPath || [];
  }

  function groupAt(entity) {
    return groupPath(entity)[activeGroup.length];
  }

  function inActiveGroup(entity) {
    if (!activeGroup.length) return true;
    const path = groupPath(entity);
    return activeGroup.every((group, index) => group === path[index]);
  }

  function editableInGroup(entity) {
    return inActiveGroup(entity) && !groupAt(entity);
  }

  function selectionUnits(ids, includeParents = false) {
    const groups = new Set();
    const objects = new Set();
    for (const id of ids) {
      const item = byId.get(id);
      if (!item) continue;
      const group = groupAt(item);
      if (group) groups.add(group);
      else objects.add(includeParents && item.parentId || id);
    }
    return { groups, objects, count: groups.size + objects.size };
  }

  function groupMembers(group) {
    return current.entities.filter(entity => !entity.parentId && inActiveGroup(entity) && groupAt(entity) === group);
  }

  function groupSelected() {
    const units = selectionUnits([...selected, ...selectedEdges.keys()], true);
    if (units.count < 2) return;
    const members = current.entities.filter(entity => !entity.parentId && (units.objects.has(entity.id)
      || inActiveGroup(entity) && units.groups.has(groupAt(entity))));
    if (members.some(entity => groupPath(entity).length >= MAX_GROUP_DEPTH)) {
      log("Достигнута предельная глубина вложенности групп.", true);
      return;
    }
    const group = newId();
    const ids = new Set(members.map(entity => entity.id));
    commit(current.entities.map(entity => ids.has(entity.id) ? {
      ...entity, groupPath: [...activeGroup, group, ...groupPath(entity).slice(activeGroup.length)],
    } : entity), { ids });
    log("Группа создана. Дважды щёлкните по ней, чтобы изменить её содержимое.");
  }

  function ungroupSelected() {
    const { groups } = selectionUnits(selected);
    if (!groups.size) return;
    const level = activeGroup.length;
    const entities = current.entities.map(entity => {
      if (entity.parentId || !inActiveGroup(entity) || !groups.has(groupAt(entity))) return entity;
      const { groupPath: path, ...member } = entity;
      const remaining = [...path.slice(0, level), ...path.slice(level + 1)];
      return remaining.length ? { ...member, groupPath: remaining } : member;
    });
    commit(entities);
    log(groups.size === 1 ? "Группа разъединена." : `Разъединено групп: ${groups.size}.`);
  }

  function enterGroup(group) {
    activeGroup = [...activeGroup, group];
    selected.clear();
    selectedEdges.clear();
    choose("select");
  }

  function exitGroup() {
    if (!activeGroup.length || running || offsetPreview || notchPreview) return;
    const group = activeGroup.at(-1);
    activeGroup = activeGroup.slice(0, -1);
    selected = new Set(groupMembers(group).map(entity => entity.id));
    selectedEdges.clear();
    choose("select");
  }

  function setCurrent(state) {
    clearRepairDiagnostics();
    current = state;
    byId = new Map(current.entities.map(entity => [entity.id, entity]));
    while (activeGroup.length && !current.entities.some(inActiveGroup)) activeGroup = activeGroup.slice(0, -1);
  }

  function selectedGrips(entity) {
    const points = controls(entity);
    const edges = selectedEdges.get(entity.id);
    if (!edges && entity.type === "polyline" && points.length > 32) {
      if (gripLayout?.entity === entity && gripLayout.center === center && gripLayout.scale === scale
          && gripLayout.width === width && gripLayout.height === height) return gripLayout.visible;
      const spacing = 14;
      const visible = [];
      const cells = new Map();
      const addGrip = index => {
        const point = points[index];
        const [x, y] = screen(point);
        if (x < -7 || x > width + 7 || y < -7 || y > height + 7) return;
        const column = Math.floor(x / spacing);
        const row = Math.floor(y / spacing);
        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            const near = cells.get(`${column + dx},${row + dy}`);
            if (near?.some(([nx, ny]) => Math.hypot(x - nx, y - ny) < spacing)) return;
          }
        }
        const cell = `${column},${row}`;
        if (!cells.has(cell)) cells.set(cell, []);
        cells.get(cell).push([x, y]);
        visible.push([index, point]);
      };
      if (!entity.closed) {
        addGrip(0);
        addGrip(points.length - 1);
      }
      for (const [index, point] of points.entries()) {
        const incoming = sub(point, points[(index + points.length - 1) % points.length]);
        const outgoing = sub(points[(index + 1) % points.length], point);
        if (dot(incoming, outgoing) < .866 * norm(incoming) * norm(outgoing)) addGrip(index);
      }
      for (let index = 0; index < points.length; index++) addGrip(index);
      gripLayout = { entity, center, scale, width, height, visible };
      return visible;
    }
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
    return entity && !groupAt(entity) && !(entity.notch && entity.parentId) ? entity : null;
  }

  function familyIds(ids) {
    const bases = new Set(ids);
    return new Set(current.entities.filter(entity => bases.has(entity.id) || bases.has(entity.parentId)).map(entity => entity.id));
  }

  function log(message = "", error = false) {
    if (message) clearRepairDiagnostics();
    views.message.textContent = message;
    views.message.hidden = !message;
    views.message.classList.toggle("cad-error", error);
  }

  function clearRepairDiagnostics() {
    repairLocations = [];
    repairSourceIds = [];
    views["repair-diagnostics"].hidden = true;
  }

  function updateRepairDiagnostic() {
    const location = repairLocations[repairLocationIndex];
    const [name, help] = REPAIR_DIAGNOSTICS[location.kind];
    const gap = location.kind === "gap";
    const title = gap ? `${name}: ${formatLength(location.distance)}` : name;
    const fixHelp = gap ? "Команда соединит отмеченные концы и замкнёт контур."
      : "Исправление уберёт лишние рёбра и сохранит замкнутый внешний контур.";
    views["repair-diagnostic-title"].textContent = repairLocations.length > 1
      ? `${title} · ${repairLocationIndex + 1} из ${repairLocations.length}` : title;
    views["repair-diagnostic-help"].textContent = location.feedback || (location.fixable === true ? fixHelp : help);
    actions["show-repair-location"].textContent = repairLocationFocused && repairLocations.length > 1
      ? "Следующее место" : "Показать место";
    actions["fix-repair-location"].textContent = gap ? "Замкнуть разрыв" : "Исправить";
    actions["fix-all-repair-locations"].hidden = gap || location.sourceOperation === "close-outline";
  }

  function showRepairDiagnostics(locations, ids, bulkResult = null) {
    log();
    repairLocations = locations;
    repairSourceIds = [...ids];
    repairBulkChecked = bulkResult !== null;
    repairLocationIndex = Math.max(0, locations.findIndex(location => location.fixable === true));
    repairLocationFocused = false;
    updateRepairDiagnostic();
    views["repair-diagnostic-result"].textContent = bulkResult || "";
    views["repair-diagnostic-result"].hidden = !bulkResult;
    views["repair-diagnostics"].hidden = false;
  }

  function focusRepairLocation() {
    if (running || gesture || !repairLocations.length) return;
    if (repairLocationFocused) repairLocationIndex = (repairLocationIndex + 1) % repairLocations.length;
    repairLocationFocused = true;
    const location = repairLocations[repairLocationIndex];
    center = location.point;
    const targetScale = location.kind === "gap" && location.distance > 0
      ? Math.min(Math.max(scale, 48 / location.distance), Math.min(width, height) * .6 / location.distance)
      : Math.max(scale, 200);
    scale = clampViewScale(targetScale, center);
    updateRepairDiagnostic();
    update();
  }

  function updateWarnings(warnings) {
    const issues = new Map();
    for (const entity of current.entities) {
      if (!entity.importDamaged) continue;
      for (const issue of entity.importIssues?.length ? entity.importIssues : [LEGACY_IMPORT_ISSUE]) {
        if (!issues.has(issue)) issues.set(issue, []);
        issues.get(issue).push(entity);
      }
    }
    const entries = Array.from(issues, ([issue, entities]) => ({
      issue,
      title: issue,
      description: `Затронуто объектов: ${entities.length}. ${issue === IMPORT_DOUBLED_LINE
        ? "Нужный отрезок можно оставить на чертеже как метку, лишний — удалить."
        : "Красный контур отмечен для проверки."}`,
      entities,
    }));
    const messages = new Set(warnings.map(warning => warning?.message || String(warning)));
    for (const message of messages) {
      // Per-object issues are rebuilt from the drawing so resolved warnings disappear after repair.
      if (message.startsWith("Восстановлены повреждённые объекты:")) continue;
      if (message.startsWith("Особенности импорта: ")) {
        const annotations = [];
        const changes = [];
        for (const entry of message.slice("Особенности импорта: ".length).replace(/\.$/, "").split(", ")) {
          const match = /^([A-Z]+) (\(\d+\))$/.exec(entry);
          const label = match && IMPORT_ANNOTATIONS[match[1]];
          if (label) annotations.push(`${label} ${match[2]}`);
          else changes.push(entry);
        }
        if (annotations.length) entries.push({ title: "Пропущенные аннотации DXF",
          description: `${annotations.join(", ")}. Эти объекты отсутствуют на полотне.` });
        if (changes.length) entries.push({ title: "Обработка файла DXF", description: changes.join(", ") + "." });
      } else if (message === "Обрезанная кривая представлена полилинией.") {
        entries.push({ title: "Обрезанная кривая стала полилинией",
          description: "Её можно редактировать по вершинам. Для удаления лишних вершин выберите «Упростить контур»." });
      } else {
        entries.push({ title: message });
      }
    }
    views.warnings.hidden = !entries.length;
    const summary = views.warnings.querySelector("summary");
    summary.textContent = "Предупреждения · " + entries.length;
    const list = views.warnings.querySelector("ul");
    list.replaceChildren(...entries.map(({ issue, title, description, entities }) => {
      const item = root.createElement("li");
      const main = root.createElement("div");
      main.className = "cad-warning-main";
      const heading = root.createElement("strong");
      heading.textContent = title;
      main.append(heading);
      if (description) {
        const detail = root.createElement("p");
        detail.className = "cad-warning-description";
        detail.textContent = description;
        main.append(detail);
      }
      item.append(main);
      if (entities) {
        const commands = root.createElement("div");
        commands.className = "cad-warning-actions";
        const button = () => {
          const element = root.createElement("button");
          element.type = "button";
          element.className = "button cad-warning-action";
          commands.append(element);
          return element;
        };
        let index = -1;
        const show = button();
        const repair = button();
        const remove = issue === IMPORT_DOUBLED_LINE ? button() : null;
        if (remove) remove.textContent = "Удалить отрезок";
        function refreshActions() {
          const next = (index + 1) % entities.length;
          show.textContent = entities.length === 1 ? "Показать на чертеже" : `Показать ${next + 1} из ${entities.length}`;
          const target = entities[Math.max(0, index)];
          const segment = remove && isRecoveredSegment(target);
          repair.textContent = segment ? "Оставить отрезок" : "Исправить";
          repair.title = segment ? "Сохранить один отрезок и снять это предупреждение" : "Проверить и восстановить замыкание контура";
          repair.hidden = target.notch || target.type === "point";
          if (remove) remove.hidden = !segment;
        }
        show.addEventListener("click", () => {
          const next = (index + 1) % entities.length;
          if (!focusWarning(entities[next])) return;
          index = next;
          refreshActions();
          show.focus({ preventScroll: true });
        });
        repair.addEventListener("click", () => {
          const target = entities[Math.max(0, index)];
          if (!focusWarning(target)) return;
          summary.focus({ preventScroll: true });
          const ids = isRecoveredSegment(target) ? [target.id] : [...contourIds(target)];
          void editOnServer("repair-part", { ids });
        });
        remove?.addEventListener("click", () => {
          const target = entities[Math.max(0, index)];
          if (!isRecoveredSegment(target) || !focusWarning(target)) return;
          removeSelected();
          log("Отрезок удалён.");
        });
        refreshActions();
        item.append(commands);
      }
      return item;
    }));
  }

  function focusWarning(entity) {
    if (running || gesture || offsetPreview || notchPreview || parameterDialog.open) return false;
    if (tool !== "select") choose("select");
    activeGroup = [...groupPath(entity)];
    selectContour(new Set([entity.id]));
    fit([entity]);
    return true;
  }

  function isRecoveredSegment(entity) {
    return !entity.notch && entity.importIssues?.includes(IMPORT_DOUBLED_LINE)
      && (entity.type === "line" || entity.type === "polyline" && !entity.closed && entity.points.length === 2
        && !entity.bulges?.some(Boolean) && distance(...entity.points) > CONTOUR_EXACT_ENDPOINT_TOLERANCE_MM);
  }

  function simplifiable(entity) {
    if (entity?.type !== "polyline" || !editableInGroup(entity) || entity.notch) return false;
    const { points, bulges, closed } = entity;
    if (points.length <= (closed ? 3 : 2)) return false;
    if (!bulges?.some(Boolean)) return true;
    return points.some((_, index) => (closed || index > 0 && index < points.length - 1)
      && !bulges[(index + points.length - 1) % points.length] && !bulges[index]);
  }

  function showTools(open, restoreFocus = false) {
    toolsPanel.hidden = !open;
    actions.tools.setAttribute("aria-expanded", String(open));
    if (restoreFocus) actions.tools.focus({ preventScroll: true });
  }

  function updateSnapButton() {
    const enabled = [...toolsPanel.querySelectorAll("input:checked")].map(input => input.parentElement.textContent.trim());
    actions.tools.title = enabled.length ? `Включено · ${enabled.join(", ")}` : "Привязки выключены";
  }

  function toolInstruction() {
    if (running || notchPreview) return "";
    if (tool === "offset") return offsetPreview
      ? "Ведите курсор в нужную сторону. Щёлкните для применения или введите миллиметры и нажмите Enter."
      : "Щёлкните по контуру, от которого нужно построить припуск.";
    if (tool === "mirror") {
      if (mirrorMode === "edge") return "Отразить · Наведите на прямое ребро и щёлкните.";
      if (mirrorMode !== "points") return "Отразить · Наведите ось и щёлкните. Enter применяет показанное отражение.";
      return pending.length ? "Отразить · Укажите вторую точку оси. Shift фиксирует горизонталь или вертикаль."
        : "Отразить · Укажите первую точку оси или выберите готовое направление.";
    }
    if (tool === "scale") return "Масштаб · Потяните ручку рамки или щёлкните по ней для точного размера.";
    if (tool === "erase") return "Ластик · Щёлкните по участку или проведите по нему курсором.";
    if (tool === "notch-edit") return placingNotch ? "Надсечка · Укажите новую точку на контуре." : "Надсечка · Выберите надсечку.";
    if (tool === "arc" && !pending.length) return "Дуга · Укажите первый конец. Привязка к контуру включена.";
    const definition = DRAW[tool] || EDIT[tool] || (tool === "measure"
      ? { label: "Измерить", points: ["Начало измерения", "Конец измерения"] } : null);
    if (!definition?.points) return "";
    const step = definition.points[Math.min(pending.length, definition.points.length - 1)];
    const exact = tool === "rotate" && pending.length ? "Введите угол и нажмите Enter. Shift задаёт шаг 45°."
      : directDistance() ? tool === "rectangle" ? "Размеры можно ввести с клавиатуры. Tab переключает сторону, Enter применяет."
        : "Размер можно ввести с клавиатуры и подтвердить Enter." : "";
    return [definition.label, step, exact].filter(Boolean).join(" · ");
  }

  function update() {
    const items = selectedEntities();
    const units = selectionUnits(selected);
    const count = units.count;
    const groupable = selectionUnits([...selected, ...selectedEdges.keys()], true);
    const settings = count > 0;
    const transformSelection = settings || selectedEdges.size > 0;
    const first = items[0];
    const active = Boolean(gesture?.type === "grip" || gesture?.type === "scale" || offsetPreview || notchPreview);
    actions["fix-repair-location"].disabled = running || active || repairLocations[repairLocationIndex]?.fixable !== true;
    actions["fix-all-repair-locations"].disabled = running || active || repairBulkChecked || !repairSourceIds.length;
    for (const [key, button] of Object.entries(buttons)) {
      button.setAttribute("aria-pressed", String(key === tool));
      button.disabled = running || active || (key in EDIT && !key.startsWith("notch") && !(OBJECT_TRANSFORMS.has(key) ? transformSelection : settings))
        || (key === "scale" && !selectedEdges.size && !items.some(entity => entity.type !== "point"));
    }
    actions["erase-selection"].disabled = running || active || !settings;
    actions.join.disabled = tool !== "select" || running || active || groupable.count < 2;
    const outlines = selectedOutlines();
    const oneOpenPolyline = outlines.length === 1 && outlines[0].type === "polyline" && !outlines[0].closed;
    actions["close-outline"].disabled = tool !== "select" || running || active || !outlines.every(editableInGroup)
      || (outlines.length < 2 && !oneOpenPolyline);
    const canUngroup = tool === "select" && units.groups.size > 0;
    actions.join.hidden = canUngroup && groupable.count < 2;
    actions.ungroup.hidden = !canUngroup;
    actions.ungroup.disabled = running || active || !canUngroup;
    views["group-status"].hidden = !activeGroup.length;
    views["group-label"].textContent = activeGroup.length > 1 ? `Редактирование группы · уровень ${activeGroup.length}` : "Редактирование группы";
    actions["exit-group"].disabled = running || active;
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
    renderOffsetControls();
    views["mirror-controls"].hidden = tool !== "mirror";
    if (tool === "mirror") for (const mode of Object.keys(MIRROR_MODES)) {
      const button = actions[`mirror-${mode}`];
      button.setAttribute("aria-pressed", String(mode === mirrorMode));
      button.disabled = running;
    }
    actions["mirror-cancel"].disabled = running;
    const step = toolInstruction();
    views["tool-step"].hidden = !step;
    if (views["tool-step"].textContent !== step) views["tool-step"].textContent = step;
    views.context.hidden = [...views.context.children].every(child => child.hidden);
    const edgeCount = Array.from(selectedEdges.values()).reduce((total, edges) => total + edges.size, 0);
    const selection = [...new Map([...items, ...current.entities.filter(entity => selectedEdges.has(entity.id))]
      .map(entity => [entity.id, entity])).values()];
    const needsReview = selection.filter(entity => entity.importDamaged).length;
    const onlyGroup = count === 1 && !edgeCount && units.groups.size;
    if (count + edgeCount === 1) {
      const [entity] = selection;
      const polyline = entity.type === "polyline" && !edgeCount;
      views.selection.textContent = onlyGroup ? "" : [edgeCount ? "Ребро полилинии" : ENTITY_NAMES[entity.type] || entity.type, entity.layer,
        polyline ? `Сегменты: ${entity.points.length - Number(!entity.closed)}` : "",
        polyline && (entity.joined || entity.points.length > 32) ? "Alt+щелчок — выбрать ребро" : "",
        entity.reference ? "вспомогательный" : "", entity.importDamaged ? "Проверьте после импорта" : ""].filter(Boolean).join(" · ");
    } else {
      views.selection.textContent = [
        count || !edgeCount ? `Выбрано объектов: ${count}` : "",
        edgeCount ? `Выбрано рёбер: ${edgeCount}` : "",
        needsReview ? `Требуют проверки после импорта: ${needsReview}` : "",
      ].filter(Boolean).join(" · ");
    }
    views["selection-summary"].hidden = Boolean(onlyGroup || !count && !edgeCount || tool !== "select");
    actions["repair-part"].hidden = tool !== "select" || units.groups.size > 0 || !selection.length
      || !selection.every(entity => !entity.notch && entity.type !== "point");
    actions["repair-part"].disabled = running || active;
    actions["repair-part"].querySelector("span").textContent = selection.length === 1 && isRecoveredSegment(selection[0])
      ? "Оставить отрезок" : "Восстановить деталь";
    actions.simplify.hidden = tool !== "select" || !selection.some(simplifiable);
    actions.simplify.disabled = running || active;
    actions.reference.hidden = !first || tool !== "select" || items.every(entity => entity.type === "point");
    actions.reference.disabled = running || active;
    actions.reference.querySelector("span").textContent = items.every(entity => entity.reference) ? "Назначить для реза" : "Сделать вспомогательным";
    views["selection-tools"].hidden = actions["repair-part"].hidden && actions.simplify.hidden && actions.reference.hidden;
    draw();
  }

  function commit(entities, { warnings = current.warnings, ids = selected } = {}) {
    previous.push(current);
    if (previous.length > 50) previous.shift();
    future.length = 0;
    setCurrent({ entities, warnings });
    if (ids !== selected) selectedEdges.clear();
    selected = new Set(ids);
    updateWarnings(warnings);
    update();
  }

  function choose(next) {
    closeContextMenu();
    clearPointerMove();
    cancelGesture();
    clearOffset();
    if (tool === "measure") {
      measurements.length = 0;
      if (next === "measure") next = "select";
    }
    tool = next;
    if (OBJECT_TRANSFORMS.has(next)) for (const id of selectedEdges.keys()) selected.add(id);
    mirrorMode = "points";
    mirrorEdge = null;
    scaleSide = null;
    placingNotch = false;
    scaleIds = tool === "scale" ? familyIds(selected) : new Set();
    scaleBox = tool === "scale" ? selectionBounds() : null;
    if (next !== "select") selectedEdges.clear();
    pending = [];
    resetArcPlacement();
    resetDirectDistance();
    cursor = shiftGuide = null;
    clearSnapGuides();
    hover = notchTarget = trackingSource = null;
    lastPointer = null;
    log();
    update();
    canvas.focus({ preventScroll: true });
  }

  function clearSnapGuides() {
    snap = guideOrigin = guideReference = null;
    snapType = "";
    visibleGuide = null;
  }

  function openParameters() {
    if (running || root.querySelector("dialog[open]")) return;
    showTools(false);
    const [title, label, submit] = TOOL_PARAMETERS[insertPointsTarget ? "insert-points" : tool];
    views["parameter-title"].textContent = title;
    views["option-label"].textContent = tool === "scale" ? `${scaleSide.axis === 0 ? "Ширина" : "Высота"}, мм` : label;
    views["option-button"].textContent = submit;
    views["scale-mode"].hidden = tool !== "scale";
    views["notch-series"].hidden = tool !== "notch-series";
    actions["relocate-notch"].hidden = tool !== "notch-edit";
    fields.option.inputMode = insertPointsTarget ? "numeric" : "decimal";
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
    if (insertPointsTarget) {
      insertPointsTarget = null;
      return;
    }
    if (tool === "scale") {
      scaleSide = null;
      update();
    } else choose("select");
  }

  function undo(from, to) {
    if (!from.length || running || gesture?.type === "grip" || offsetPreview || notchPreview) return;
    cancelGesture();
    to.push(current);
    setCurrent(from.pop());
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
    traceEntityPath({ type: "line" }, points);
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

  function currentScalePreview() {
    if (gesture?.type === "scale") return gesture.parameters;
    if (!parameterDialog.open || !scaleSide) return null;
    const size = validSize(fields.option.value);
    if (!Number.isFinite(size)) return null;
    const parameters = scaleParameters(scaleSide, size, fields.proportional.checked);
    return parameters.sx !== 1 || parameters.sy !== 1 ? parameters : null;
  }

  function drawScaleFrame(preview) {
    const scaling = gesture?.type === "scale";
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
    context.strokeStyle = CAD_SELECTED_COLOR;
    context.lineWidth = 1;
    context.setLineDash([5, 4]);
    context.strokeRect(leftX, topY, rightX - leftX, bottomY - topY);
    context.setLineDash([]);
    if (preview) {
      const { origin, sx, sy } = preview;
      const pointTransform = ([x, y]) => [origin[0] + (x - origin[0]) * sx, origin[1] + (y - origin[1]) * sy];
      drawSelectionPreview(pointTransform, scaleIds);
      const target = pointTransform(scaleSide.point);
      drawTransformGuide(origin, target);
      const [x, y] = screen(origin);
      context.fillStyle = CAD_TRANSFORM_COLOR;
      context.fillRect(x - 3, y - 3, 6, 6);
      drawTransformLabel(target, `${format((right - left) * sx)} × ${format((top - bottom) * sy)} мм`);
    } else {
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

  function guideTarget(point, accepted = () => true) {
    return hit(point, entity => !entity.notch && accepted(entity), 10 / scale, false);
  }

  function projectToGuide(point, origin, direction) {
    return add(origin, mul(direction, dot(sub(point, origin), direction)));
  }

  function projectedSnap(point, reference, label) {
    snap = point;
    guideReference = reference;
    snapType = label;
    return snap;
  }

  function updateScaleGesture(point, shift, bypass = false) {
    const { axis, point: handle, origin } = scaleSide;
    const direction = Math.sign(handle[axis] - origin[axis]);
    const hovered = shift && !bypass ? guideTarget(point, entity => !scaleIds.has(entity.id)) : null;
    const contact = hovered && direction * (hovered.point[axis] - origin[axis]) > CAD_POINT_EPSILON ? hovered : null;
    const moved = contact ? contact.point[axis] : handle[axis] + point[axis] - world(gesture.start)[axis];
    const size = Math.max(CAD_POINT_EPSILON, direction * (moved - origin[axis]));
    clearSnapGuides();
    if (!bypass) {
      visibleGuide = axisGuide(origin, 1 - axis);
      if (contact) projectedSnap(handle.map((value, index) => index === axis ? moved : value), contact.point, "По оси масштаба");
    }
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
    context.strokeStyle = fields.grid.checked ? "#d4e4ee" : "#e6e6e6";
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

  function traceArc({ center, radius, startAngle = 0, sweep = 360 }) {
    context.arc(...screen(center), radius * scale, -startAngle * DEG,
      -(startAngle + sweep) * DEG, sweep > 0);
  }

  function traceNativePath(entity, path) {
    if (entity.type === "circle" || entity.type === "arc") {
      traceArc(entity);
    } else if (entity.type === "ellipse") {
      const { center, axis, ratio, startAngle, sweep } = entity;
      const radius = norm(axis) * scale;
      context.ellipse(...screen(center), radius, radius * ratio, -angle(axis) * DEG,
        -startAngle * DEG, -(startAngle + sweep) * DEG, sweep > 0);
    } else if (entity.type === "polyline" && entity.bulges?.some(Boolean)) {
      const { points, closed } = entity;
      context.moveTo(...screen(points[0]));
      for (let index = 0; index < points.length - Number(!closed); index++) {
        const { end, arc } = polylineEdge(entity, index);
        if (arc) traceArc(arc);
        else context.lineTo(...screen(end));
      }
    } else if (path.length) {
      context.moveTo(...screen(path[0]));
      for (let index = 1; index < path.length; index++) context.lineTo(...screen(path[index]));
    }
  }

  function nativePathExtent(entity, path) {
    const extent = (point, radius = 0) => Math.max(...screen(point).map(Math.abs)) + radius * scale;
    if (entity.type === "circle" || entity.type === "arc") return extent(entity.center, entity.radius);
    if (entity.type === "ellipse") return extent(entity.center, norm(entity.axis));
    const limit = NATIVE_CANVAS_LIMIT_PX / pixelRatio;
    let maximum = 0;
    for (const point of path) {
      maximum = Math.max(maximum, extent(point));
      if (maximum > limit) return maximum;
    }
    if (entity.type === "polyline" && entity.bulges?.some(Boolean)) {
      for (let index = 0; index < entity.points.length - Number(!entity.closed); index++) {
        const { arc } = polylineEdge(entity, index);
        if (arc) maximum = Math.max(maximum, extent(arc.center, arc.radius));
        if (maximum > limit) break;
      }
    }
    return maximum;
  }

  function clippedStroke(bounds) {
    let previous = null;
    return (start, end) => {
      let first = 0;
      let last = 1;
      if (!within(start, bounds) || !within(end, bounds)) {
        const delta = sub(end, start);
        for (let axis = 0; axis < 2; axis++) {
          if (!delta[axis]) {
            if (start[axis] < bounds[axis] || start[axis] > bounds[axis + 2]) { previous = null; return; }
            continue;
          }
          const a = (bounds[axis] - start[axis]) / delta[axis];
          const b = (bounds[axis + 2] - start[axis]) / delta[axis];
          first = Math.max(first, Math.min(a, b));
          last = Math.min(last, Math.max(a, b));
        }
        if (first > last) { previous = null; return; }
      }
      const from = first === 0 ? start : mix(start, end, first);
      const to = last === 1 ? end : mix(start, end, last);
      if (!previous || previous[0] !== from[0] || previous[1] !== from[1]) context.moveTo(...from);
      context.lineTo(...to);
      previous = to;
    };
  }

  function tracePrecisePath(entity, path, project, stretch = 1) {
    const bounds = [-12, -12, width + 12, height + 12];
    const segment = clippedStroke(bounds);
    const tolerance = CURVE_STROKE_TOLERANCE_PX / pixelRatio;
    function curve(shape, pointAt, radius, sweep) {
      const sample = t => project(pointAt(shape, t));
      const radians = Math.abs(sweep) * DEG;
      const error = radius * scale * stretch * radians ** 2 / 8;
      // Chord error bounds both the subdivision tolerance and the offscreen rejection.
      function span(first, last, a, b, deviation) {
        if (Math.max(a[0], b[0]) + deviation < bounds[0] || Math.min(a[0], b[0]) - deviation > bounds[2]
            || Math.max(a[1], b[1]) + deviation < bounds[1] || Math.min(a[1], b[1]) - deviation > bounds[3]) return;
        if (deviation <= tolerance) {
          segment(a, b);
          return;
        }
        const middle = (first + last) / 2;
        const point = sample(middle);
        span(first, middle, a, point, deviation / 4);
        span(middle, last, point, b, deviation / 4);
      }
      span(0, 1, sample(0), sample(1), error);
    }
    if (entity.type === "arc" || entity.type === "circle") {
      const arc = entity.type === "circle" ? { ...entity, startAngle: 0, sweep: 360 } : entity;
      curve(arc, arcPoint, arc.radius, arc.sweep);
    } else if (entity.type === "ellipse") {
      curve(entity, ellipsePoint, norm(entity.axis), entity.sweep);
    } else if (entity.type === "polyline" && entity.bulges?.some(Boolean)) {
      for (let index = 0; index < entity.points.length - Number(!entity.closed); index++) {
        const { start, end, arc } = polylineEdge(entity, index);
        if (arc) curve(arc, arcPoint, arc.radius, arc.sweep);
        else segment(project(start), project(end));
      }
    } else if (path.length) {
      let start = project(path[0]);
      for (let index = 1; index < path.length; index++) {
        const end = project(path[index]);
        segment(start, end);
        start = end;
      }
    }
  }

  function traceEntityPath(entity, path) {
    if (nativePathExtent(entity, path) * pixelRatio <= NATIVE_CANVAS_LIMIT_PX) traceNativePath(entity, path);
    else tracePrecisePath(entity, path, screen);
  }

  function drawEntity(entity, highlighted = false, preview = false) {
    const cached = geometry(entity);
    const { bounds } = cached;
    const [minX, maxY] = world([-10, -10]);
    const [maxX, minY] = world([width + 10, height + 10]);
    const margin = entity.type === "spline" ? SPLINE_TOLERANCE_MM : 0;
    if (bounds[2] + margin < minX || bounds[0] - margin > maxX || bounds[3] + margin < minY || bounds[1] - margin > maxY) return;
    const baseColor = entity.importDamaged ? "#b00020" : entity.notch ? CAD_NOTCH_COLOR : entity.reference ? "#777" : "#171717";
    const overlay = highlighted && (entity.importDamaged || entity.notch);
    context.strokeStyle = highlighted ? CAD_SELECTED_COLOR : baseColor;
    context.lineWidth = overlay ? 5 : highlighted ? 3 : preview || entity.notch || entity.importDamaged ? 2.25 : 1.5;
    context.setLineDash(!highlighted && entity.reference ? [6, 4] : []);
    context.beginPath();
    if (entity.type === "point") {
      const [x, y] = screen(entity.point);
      context.setLineDash([]);
      context.moveTo(x - 5, y);
      context.lineTo(x + 5, y);
      context.moveTo(x, y - 5);
      context.lineTo(x, y + 5);
    } else {
      traceEntityPath(entity, viewportPath(entity, cached));
    }
    context.stroke();
    if (overlay) {
      context.strokeStyle = baseColor;
      context.lineWidth = 2;
      context.stroke();
    }
    context.setLineDash([]);
  }

  function drawSelectionPreview(pointTransform, ids = familyIds(selected)) {
    const [minX, maxY] = world([-10, -10]);
    const [maxX, minY] = world([width + 10, height + 10]);
    const transformedCenter = pointTransform(center);
    // Measure the basis over a longer span to retain precision far from the drawing origin.
    const basisLength = 1024;
    const [xx, xy] = mul(sub(pointTransform(add(center, [basisLength, 0])), transformedCenter), 1 / basisLength);
    const [yx, yy] = mul(sub(pointTransform(add(center, [0, basisLength])), transformedCenter), 1 / basisLength);
    const [centerX, centerY] = screen(transformedCenter);
    const offsetX = centerX - (xx * width - yx * height) / 2;
    const offsetY = centerY - (-xy * width + yy * height) / 2;
    const stretch = Math.hypot(xx, xy, yx, yy);
    const project = point => screen(pointTransform(point));
    context.save();
    context.lineJoin = "round";
    for (const id of ids) {
      const entity = byId.get(id);
      if (!entity) continue;
      const cached = geometry(entity);
      const [left, bottom, right, top] = cached.bounds;
      const margin = entity.type === "spline" ? SPLINE_TOLERANCE_MM : 0;
      const corners = [[left - margin, bottom - margin], [left - margin, top + margin],
        [right + margin, bottom - margin], [right + margin, top + margin]];
      const [x1, y1, x2, y2] = geometryBounds(corners.map(pointTransform));
      if (x2 < minX || x1 > maxX || y2 < minY || y1 > maxY) continue;
      context.strokeStyle = CAD_TRANSFORM_COLOR;
      context.lineWidth = entity.notch ? 4 : 2.5;
      if (entity.type === "point") {
        const [x, y] = screen(pointTransform(entity.point));
        context.setLineDash([]);
        context.beginPath();
        context.moveTo(x - 5, y);
        context.lineTo(x + 5, y);
        context.moveTo(x, y - 5);
        context.lineTo(x, y + 5);
        context.stroke();
      } else {
        context.setLineDash(entity.reference ? [6, 4] : []);
        context.beginPath();
        const path = viewportPath(entity, cached);
        const extent = nativePathExtent(entity, path) * Math.max(1, stretch) + Math.max(Math.abs(offsetX), Math.abs(offsetY));
        if (extent * pixelRatio <= NATIVE_CANVAS_LIMIT_PX) {
          context.save();
          context.transform(xx, -xy, -yx, yy, offsetX, offsetY);
          traceNativePath(entity, path);
          context.restore();
        } else tracePrecisePath(entity, path, project, stretch);
        context.stroke();
        if (entity.notch) {
          context.strokeStyle = CAD_NOTCH_COLOR;
          context.lineWidth = 1.75;
          context.stroke();
        }
      }
    }
    context.restore();
  }

  function drawTransformGuide(...points) {
    context.save();
    context.strokeStyle = CAD_TRANSFORM_COLOR;
    context.lineWidth = 1.25;
    context.setLineDash([6, 4]);
    pathLine(points);
    context.restore();
  }

  function drawTransformLabel(point, text, color = CAD_TRANSFORM_COLOR, verticalOffset = -26) {
    const [px, py] = screen(point);
    context.save();
    context.font = "600 12px system-ui, sans-serif";
    const labelWidth = context.measureText(text).width + 12;
    const x = Math.max(4, Math.min(width - labelWidth - 4, px + 10));
    const y = Math.max(4, Math.min(height - 25, py + verticalOffset));
    context.fillStyle = "#f8f4ff";
    context.fillRect(x, y, labelWidth, 21);
    context.fillStyle = color;
    context.textBaseline = "middle";
    context.fillText(text, x + 6, y + 10.5);
    context.restore();
  }

  function placementTransform(base, target) {
    const delta = sub(target, base);
    return norm(delta) < EPSILON ? null : point => add(point, delta);
  }

  function reflectionTransform(base, target) {
    const delta = sub(target, base);
    const axis = mul(delta, 1 / norm(delta));
    return point => {
      const vector = sub(point, base);
      return add(base, sub(mul(axis, 2 * dot(vector, axis)), vector));
    };
  }

  function drawPlacementPreview(base, target) {
    const pointTransform = placementTransform(base, target);
    if (!pointTransform) return;
    drawSelectionPreview(pointTransform);
    const delta = sub(target, base);
    if (Math.abs(delta[0] * scale) > 2 && Math.abs(delta[1] * scale) > 2) drawTransformGuide(base, [target[0], base[1]], target);
    drawTransformGuide(base, target);
    drawTransformLabel(mix(base, target, .5), `ΔX ${format(delta[0])} · ΔY ${format(delta[1])} мм`);
    const [x, y] = screen(base);
    context.fillStyle = CAD_TRANSFORM_COLOR;
    context.fillRect(x - 3, y - 3, 6, 6);
  }

  function currentMirrorAxis(point) {
    if (mirrorMode === "edge") return mirrorEdge && [mirrorEdge.start, mirrorEdge.end];
    if (mirrorMode === "points") {
      return pending.length && point && distance(pending[0], point) * scale > 2 ? [pending[0], point] : null;
    }
    const bounds = !point && selectionBounds();
    const base = point || (bounds && mix(bounds.slice(0, 2), bounds.slice(2), .5));
    if (!base) return null;
    const direction = mirrorMode === "vertical" ? [0, 1] : [1, 0];
    return [base, add(base, direction)];
  }

  function drawMirrorPreview(point) {
    const axis = currentMirrorAxis(point);
    if (axis) {
      if (views.message.textContent === MIRROR_AXIS_WARNING[mirrorMode]) log();
      const [base, target] = axis;
      drawSelectionPreview(reflectionTransform(base, target));
      const delta = sub(target, base);
      const direction = mul(delta, 1 / norm(delta));
      const nearCenter = add(base, mul(direction, dot(sub(center, base), direction)));
      const extent = mul(direction, Math.hypot(width, height) / scale);
      drawTransformGuide(sub(nearCenter, extent), add(nearCenter, extent));
      if (mirrorMode === "edge") {
        context.save();
        context.strokeStyle = CAD_SNAP_COLOR;
        context.lineWidth = 2.25;
        context.setLineDash([]);
        pathLine(axis);
        context.restore();
      }
      drawTransformLabel(point || base, "Ось отражения");
    }
    if (pending.length) {
      const [x, y] = screen(pending[0]);
      context.fillStyle = CAD_TRANSFORM_COLOR;
      context.fillRect(x - 3, y - 3, 6, 6);
    }
  }

  function drawArcPreview(point) {
    const [start, end] = pending;
    if (!start || (!end && !point)) return;
    const symmetric = arcSymmetry && snapType === ARC_CUES.symmetry.label;
    context.save();
    context.strokeStyle = symmetric ? ARC_CUES.symmetry.color : CAD_SNAP_COLOR;
    context.lineWidth = 1.25;
    context.setLineDash([5, 4]);
    pathLine([start, end || point]);
    if (!end && arcSymmetry) {
      context.strokeStyle = ARC_CUES.symmetry.color;
      pathLine([start, arcSymmetry.corner, arcSymmetry.point]);
      const [x, y] = screen(arcSymmetry.point);
      context.beginPath();
      context.arc(x, y, 4, 0, Math.PI * 2);
      context.stroke();
    }
    if (end) {
      const { middle, normal } = arcFrame();
      const extent = mul(normal, Math.hypot(width, height) / scale);
      const bulge = point ? dot(sub(point, middle), normal) : 0;
      const cue = nearestArcBulge(bulge, .25 / scale);
      const color = cue ? ARC_CUES[cue.kind].color : CAD_SNAP_COLOR;
      context.strokeStyle = color;
      pathLine([sub(middle, extent), add(middle, extent)]);
      const arc = point && creation([start, end, point]);
      if (arc) {
        context.setLineDash([]);
        context.lineWidth = 2.5;
        context.beginPath();
        traceEntityPath(arc);
        context.stroke();
        if (cue && !distanceInput) drawTransformLabel(point, `R ${format(arc.radius)} мм`, color, 11);
        else drawTransformLabel(mix(middle, point, .45), `R ${format(arc.radius)} мм`, color);
      }
    }
    context.restore();
  }

  function draw() {
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.miterLimit = 2;
    drawGrid();
    const scalePreview = tool === "scale" && scaleBox && currentScalePreview();
    const shown = offsetPreview ? previewEntities() : notchPreview?.entities || gesture?.entities || current.entities;
    const offsetResult = shown !== current.entities ? offsetPreview?.result : null;
    const isSelected = entity => selected.has(entity.id) || (groupAt(entity) && selected.has(entity.parentId));
    const hoveredGroup = hover && groupAt(hover);
    for (const entity of shown) {
      if (scalePreview && scaleIds.has(entity.id)) continue;
      if (!offsetPreview && !notchPreview && isSelected(entity)) continue;
      if (!inActiveGroup(entity)) context.globalAlpha = .25;
      drawEntity(entity, offsetResult?.ids.has(entity.parentId || entity.id), Boolean(hover?.id === entity.id || hoveredGroup && inActiveGroup(entity) && groupAt(entity) === hoveredGroup
        || offsetPreview?.originals.has(entity.parentId || entity.id) || notchPreview?.ids.includes(entity.id)));
      context.globalAlpha = 1;
    }
    for (const entity of shown) {
      if (offsetPreview || notchPreview) continue;
      if (isSelected(entity) && !scalePreview) drawEntity(entity, true);
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
    if (tool === "select" && !gesture && !offsetPreview && !notchPreview && !selectedEdges.size) {
      const { groups, count } = selectionUnits(selected);
      const box = count === 1 && groups.size && selectionBounds();
      if (box) {
        const [left, top] = screen([box[0], box[3]]);
        const [right, bottom] = screen([box[2], box[1]]);
        context.strokeStyle = CAD_SELECTED_COLOR;
        context.lineWidth = 1;
        context.setLineDash([5, 4]);
        context.strokeRect(left - 4, top - 4, right - left + 8, bottom - top + 8);
        context.setLineDash([]);
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
    if (tool === "offset") drawOffsetGuide();
    if (tool === "arc") drawArcPreview(previewPoint);
    else if (pending.length && previewPoint && DRAW[tool]) {
      const points = [...pending, previewPoint];
      const provisional = creation(points, false, true);
      context.strokeStyle = "#555";
      context.lineWidth = 1.5;
      context.setLineDash([5, 4]);
      if (provisional) {
        context.beginPath();
        traceEntityPath(provisional, samples(provisional));
        context.stroke();
      } else pathLine(points);
      context.setLineDash([]);
    } else if (tool === "rotate") {
      drawRotationPreview(cursor);
    } else if (tool === "mirror") {
      drawMirrorPreview(cursor);
    } else if (pending.length && previewPoint && (tool === "move" || tool === "copy")) {
      drawPlacementPreview(pending[0], previewPoint);
    }
    if (tool === "scale" && scaleBox) drawScaleFrame(scalePreview);
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
    if (cursor && tool === "ellipse" && pending.length === 2) {
      const { normal } = arcFrame();
      const nearView = projectToGuide(center, pending[0], normal);
      const extent = mul(normal, Math.hypot(width, height) / scale);
      context.strokeStyle = CAD_SNAP_COLOR;
      context.lineWidth = 1;
      context.setLineDash([6, 4]);
      pathLine([sub(nearView, extent), add(nearView, extent)]);
      context.setLineDash([]);
    }
    if (cursor && visibleGuide) {
      const { origin, direction, axis } = visibleGuide;
      const [x, y] = screen(origin);
      context.strokeStyle = CAD_SNAP_COLOR;
      context.lineWidth = 1;
      context.setLineDash([6, 4]);
      if (axis == null) {
        const middle = projectToGuide(center, origin, direction);
        const extent = mul(direction, Math.hypot(width, height) / scale);
        pathLine([sub(middle, extent), add(middle, extent)]);
      } else {
        context.beginPath();
        if (axis === 0) { context.moveTo(x, 0); context.lineTo(x, height); }
        else { context.moveTo(0, y); context.lineTo(width, y); }
        context.stroke();
      }
      context.setLineDash([]);
    }
    if (cursor && snap && guideReference && distance(snap, guideReference) * scale > 1) {
      context.strokeStyle = "#76869a";
      context.lineWidth = 1;
      context.setLineDash([3, 4]);
      pathLine([guideReference, snap]);
      context.setLineDash([]);
      context.beginPath();
      context.arc(...screen(guideReference), 4, 0, Math.PI * 2);
      context.stroke();
    }
    if (cursor && (tool !== "select" || gesture?.type === "grip")) {
      if (snap) {
        const cue = tool === "arc" && Object.values(ARC_CUES).find(item => item.label === snapType);
        drawSnapMarker(snap, snapType, cue?.color);
      }
      else if (guideOrigin && !visibleGuide) drawSnapMarker(guideOrigin, "Опорная точка");
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
    for (const [index, location] of repairLocations.entries()) {
      if (location.kind === "gap") {
        context.save();
        context.strokeStyle = "#fff";
        context.lineWidth = 5;
        pathLine(location.endpoints);
        context.strokeStyle = "#b00020";
        context.lineWidth = 2;
        context.setLineDash([4, 3]);
        pathLine(location.endpoints);
        context.setLineDash([]);
        context.beginPath();
        const radius = index === repairLocationIndex ? 6 : 4;
        for (const point of location.endpoints) {
          const [x, y] = screen(point);
          context.moveTo(x + radius, y);
          context.arc(x, y, radius, 0, Math.PI * 2);
        }
        context.fillStyle = "#fff";
        context.fill();
        context.stroke();
        context.restore();
        continue;
      }
      const [x, y] = screen(location.point);
      if (x < -10 || y < -10 || x > width + 10 || y > height + 10) continue;
      context.beginPath();
      context.arc(x, y, index === repairLocationIndex ? 8 : 5, 0, Math.PI * 2);
      context.strokeStyle = "#fff";
      context.lineWidth = 5;
      context.stroke();
      context.strokeStyle = "#b00020";
      context.lineWidth = 2;
      context.stroke();
    }
    const gridLabel = `${fields.grid.checked ? "Привязка к сетке" : "Сетка"} ${formatLength(gridStep())}`;
    if (views.scale.textContent !== gridLabel) views.scale.textContent = gridLabel;
  }

  function drawSnapMarker(point, label, color = CAD_SNAP_COLOR) {
    const [x, y] = screen(point);
    context.beginPath();
    if (label === "Середина") {
      context.moveTo(x, y - 7); context.lineTo(x + 6, y + 5); context.lineTo(x - 6, y + 5); context.closePath();
    } else if (label === "Пересечение" || label === "Сетка") {
      context.moveTo(x - 6, y); context.lineTo(x + 6, y); context.moveTo(x, y - 6); context.lineTo(x, y + 6);
    } else if (label.startsWith("Ребро") || label.startsWith("Контур") || label === "Перпендикуляр" || label === "Квадрант") {
      context.moveTo(x, y - 6); context.lineTo(x + 6, y); context.lineTo(x, y + 6); context.lineTo(x - 6, y); context.closePath();
    } else if (label === "Опорная точка") {
      context.arc(x, y, 6, 0, 2 * Math.PI);
    } else context.rect(x - 5, y - 5, 10, 10);
    context.strokeStyle = "#fff";
    context.lineWidth = 4;
    context.stroke();
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.stroke();
    context.font = "600 13px Inter, Segoe UI, sans-serif";
    const length = context.measureText(label).width + 12;
    const left = Math.max(3, Math.min(width - length - 3, x + 11));
    const top = Math.max(3, Math.min(height - 23, y - 24));
    context.fillStyle = "#fff";
    context.fillRect(left, top, length, 20);
    context.fillStyle = color;
    context.fillText(label, left + 6, top + 14);
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
      const label = formatLength(distance(...line));
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
    traceEntityPath({ type: "arc", ...arc });
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
      const availableWidth = Math.max(1, width - 56);
      const availableHeight = Math.max(1, height - 56);
      const contentWidth = box[2] - box[0];
      const contentHeight = box[3] - box[1];
      const fitted = contentWidth || contentHeight
        ? Math.min(availableWidth / contentWidth, availableHeight / contentHeight)
        : Math.min(availableWidth, availableHeight);
      scale = clampViewScale(fitted, center);
    }
    draw();
  }

  function closestOnArc(point, arc) {
    if (arc.start && arc.end) {
      const startRay = sub(arc.start, arc.center);
      const turn = Math.atan2(cross(startRay, sub(point, arc.start)), dot(startRay, sub(point, arc.center))) / DEG;
      const progress = positiveAngle(Math.sign(arc.sweep) * turn);
      if (progress <= Math.abs(arc.sweep)) return arcPoint(arc, progress / Math.abs(arc.sweep));
      return distance(point, arc.start) <= distance(point, arc.end) ? arc.start : arc.end;
    }
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
    const tolerance = .05 / (norm(entity.axis) * span * DEG * scale);
    for (let i = 0; i < 80 && right - left > tolerance; i++) {
      const a = (2 * left + right) / 3;
      const b = (left + 2 * right) / 3;
      if (distance(point, ellipsePoint(entity, a)) <= distance(point, ellipsePoint(entity, b))) right = b;
      else left = a;
    }
    const candidates = [ellipsePoint(entity, (left + right) / 2), path[0], path.at(-1)];
    return candidates.map(pointOnEdge => ({ point: pointOnEdge, miss: distance(point, pointOnEdge) }))
      .reduce((first, second) => first.miss <= second.miss ? first : second);
  }

  function closestPolylineEdge(entity, point, maximum, straightOnly = false) {
    let found = null;
    let miss = maximum;
    const count = entity.points.length - Number(!entity.closed);
    for (let index = 0; index < count; index++) {
      const { start, end, arc } = polylineEdge(entity, index);
      if (straightOnly && (arc || distance(start, end) <= EPSILON)) continue;
      const nearest = arc ? closestOnArc(point, arc) : projectToSegment(point, start, end);
      const gap = distance(point, nearest);
      if (gap <= miss && (!found || gap < miss)) {
        found = { index, point: nearest, miss: gap };
        miss = gap;
      }
    }
    return found;
  }

  function hit(point, accepted = () => true, tolerance = 8 / scale, preferNotch = true, straightOnly = false) {
    let miss = tolerance;
    let found = null;
    for (const entity of current.entities) {
      if (!accepted(entity)) continue;
      const cached = geometry(entity);
      const { path, bounds } = cached;
      const [x, y] = point;
      const margin = miss + (entity.type === "spline" ? SPLINE_TOLERANCE_MM : 0);
      if (x < bounds[0] - margin || x > bounds[2] + margin || y < bounds[1] - margin || y > bounds[3] + margin) continue;
      let candidate;
      if (entity.type === "point") {
        candidate = { miss: distance(point, entity.point), point: entity.point };
      } else if (entity.type === "circle") {
        const nearest = add(entity.center, radial(angle(sub(point, entity.center)), entity.radius));
        candidate = { miss: distance(point, nearest), point: nearest };
      } else if (entity.type === "arc") {
        const nearest = closestOnArc(point, entity);
        candidate = { miss: distance(point, nearest), point: nearest };
      } else if (entity.type === "polyline") {
        candidate = closestPolylineEdge(entity, point, miss, straightOnly);
      } else if (entity.type === "ellipse") {
        candidate = closestOnEllipse(point, entity, path);
      } else {
        candidate = nearestSegment(point, viewportPath(entity, cached));
      }
      if (candidate && candidate.miss <= miss && !(preferNotch && found?.entity.notch && !entity.notch && candidate.miss === miss)) {
        miss = candidate.miss;
        found = { entity, point: candidate.point, edge: candidate.index, direction: candidate.direction };
      }
    }
    return found;
  }

  function pickAt(point) {
    if (tool === "notch-edit") {
      const parentId = current.entities.find(entity => entity.id === notchTarget)?.parentId;
      return hit(point, entity => editableInGroup(entity) && (editableNotch(entity) || notchTarget && (parentId ? entity.id === parentId : cuttingEdge(entity))));
    }
    if (tool.startsWith("notch")) return hit(point, entity => editableInGroup(entity) && cuttingEdge(entity));
    return hit(point, inActiveGroup);
  }

  const editableNotch = entity => entity.notch && ["line", "polyline"].includes(entity.type) && !entity.closed;
  const cuttingEdge = entity => !entity.notch && entity.type !== "point";

  function axisGuide(origin, axis) {
    return { origin, axis, direction: axis ? [1, 0] : [0, 1] };
  }

  function pointOnGuide(point, { origin, direction }) {
    return projectToGuide(point, origin, direction);
  }

  function guideGap(point, { origin, direction }) {
    return Math.abs(cross(sub(point, origin), direction));
  }

  function guideSource(origin, ignoredId, contact = hit(origin, entity => entity.id !== ignoredId && cuttingEdge(entity),
    Math.min(CAD_POINT_EPSILON, .25 / scale), false)) {
    const options = [axisGuide(origin, 0), axisGuide(origin, 1)];
    const contacts = contact ? [contact] : [];
    if (contact?.entity.type === "polyline" && contact.edge != null) {
      const { entity, edge: index, point } = contact;
      const { start, end } = polylineEdge(entity, index);
      const count = entity.points.length - Number(!entity.closed);
      const atStart = distance(point, start) <= CAD_POINT_EPSILON;
      const atEnd = distance(point, end) <= CAD_POINT_EPSILON;
      const adjacent = atStart ? index - 1 : atEnd ? index + 1 : index;
      if (adjacent !== index && (entity.closed || adjacent >= 0 && adjacent < count)) {
        contacts.push({ ...contact, point: atStart ? start : end, edge: (adjacent + count) % count });
      }
    }
    function addOption(direction, label) {
      if (!options.some(guide => Math.abs(cross(direction, guide.direction)) <= EPSILON)) options.push({ origin, direction, label });
    }
    for (const match of contacts) {
      const edge = contactEdge(match);
      const tangent = arcContactTangent(match, match.point) || match.direction;
      const length = tangent ? norm(tangent) : 0;
      if (length <= EPSILON) continue;
      const [x, y] = mul(tangent, 1 / length);
      const straight = edge && !edge.arc;
      const round = edge?.arc || ["arc", "circle"].includes(match.entity.type);
      addOption([x, y], straight ? "Вдоль ребра" : "Касательная");
      addOption([-y, x], straight ? "Перпендикуляр к ребру" : round ? "По радиусу" : "Нормаль к кривой");
    }
    return { origin, options };
  }

  function guidesAtAnchor(anchor, ignoredId) {
    if (!anchor) return null;
    if (anchorSource?.entities !== current.entities || anchorSource.ignoredId !== ignoredId
        || anchorSource.scale !== scale || distance(anchor, anchorSource.origin) > EPSILON) {
      anchorSource = { ...guideSource(anchor, ignoredId), entities: current.entities, ignoredId, scale };
    }
    return anchorSource;
  }

  function nearbyGuide(raw, source, tolerance, axesOnly = false) {
    if (!source) return null;
    const delta = sub(raw, source.origin);
    let best = null;
    for (const guide of source.options) {
      if (axesOnly && guide.axis == null) continue;
      const gap = Math.abs(cross(delta, guide.direction));
      if (gap < (best?.gap ?? tolerance) && Math.abs(dot(delta, guide.direction)) >= tolerance) best = { ...guide, gap };
    }
    return best;
  }

  function position(raw, ignoredId, bypass = false, shift = false, anchor = tool === "rotate" ? null : pending.at(-1)) {
    const ortho = fields.ortho.checked;
    const previousGuide = cursor && visibleGuide && (!ortho || visibleGuide.axis != null) ? visibleGuide : null;
    clearSnapGuides();
    if (!shift || !anchor || shiftGuide && (distance(anchor, shiftGuide.anchor) > EPSILON || ortho && shiftGuide.axis == null)) shiftGuide = null;
    if (bypass) return raw;
    const contourSnapping = tool === "arc";
    const rotationReference = tool === "rotate" && shift && pending.length;
    const snapToEdges = contourSnapping || rotationReference || fields.edges.checked;
    const edgeLabel = contourSnapping || rotationReference ? "Контур" : "Ребро";
    const tolerance = (contourSnapping ? ARC_CONTOUR_SNAP_PX : 10) / scale;
    const guideContactTolerance = Math.min(CAD_POINT_EPSILON, .25 / scale);
    const twoAxis = Boolean(anchor && (tool === "rectangle" || tool === "square"));
    const axisFromAnchor = anchor && axisGuide(anchor, Number(Math.abs(raw[0] - anchor[0]) > Math.abs(raw[1] - anchor[1])));
    const anchorGuide = !ortho && nearbyGuide(raw, guidesAtAnchor(anchor, ignoredId), tolerance);
    if (shift && anchor && !shiftGuide && (previousGuide || distance(raw, anchor) * scale > 2)) {
      shiftGuide = { ...(previousGuide || anchorGuide || axisFromAnchor), anchor };
    }
    const pinned = shiftGuide || (ortho && !twoAxis ? axisFromAnchor : null);
    const usablePoint = point => !twoAxis || point.every((value, index) => Math.abs(value - anchor[index]) > CAD_POINT_EPSILON);
    const usableGuide = guide => usablePoint(pointOnGuide(raw, guide));
    const locked = pinned && usableGuide(pinned) ? pinned : null;
    if (pinned) visibleGuide = pinned;
    if (!fields.tracking.checked) trackingSource = null;
    let best = tolerance;
    let reference = null;
    let guideCandidate = null;
    let guideDistance = tolerance;
    const consider = (candidate, type) => {
      if (!usablePoint(candidate)) return;
      const gap = distance(raw, candidate);
      if (locked && guideGap(candidate, locked) > guideContactTolerance) {
        if (shiftGuide && gap < (reference?.gap ?? tolerance)) reference = { point: candidate, type, gap };
        return;
      }
      if (gap < best || snap && Math.abs(gap - best) <= EPSILON && type === "Середина" && snapType !== type) {
        best = gap;
        snap = locked ? pointOnGuide(candidate, locked) : candidate;
        snapType = type;
      }
    };
    if (fields.objects.checked || fields.tracking.checked) {
      const considerNode = (point, type = "Узел") => {
        if (fields.objects.checked) consider(point, type);
        if (fields.tracking.checked) {
          const miss = distance(raw, point);
          if (miss < guideDistance) {
            guideDistance = miss;
            guideCandidate = point;
          }
        }
      };
      for (const entity of current.entities) {
        if (entity.id === ignoredId) continue;
        const source = geometry(entity);
        if (entity.type === "spline") {
          const [left, bottom, right, top] = source.bounds;
          const margin = tolerance + SPLINE_TOLERANCE_MM;
          if (!within(raw, [left - margin, bottom - margin, right + margin, top + margin])) continue;
        }
        const cached = viewportGeometry(entity, source);
        cached.snaps ??= snapPoints(entity, cached.path);
        for (const { point, type } of cached.snaps) considerNode(point, type);
      }
      for (const candidate of [...pending, [0, 0]]) considerNode(candidate);
    }
    if (fields.crossings.checked) nearbyCrossings(raw, tolerance, ignoredId, consider);
    const acceptedEdge = entity => entity.id !== ignoredId && cuttingEdge(entity);
    const trackingGuide = ortho && anchor ? null : nearbyGuide(raw, trackingSource, tolerance, ortho);
    if (fields.tracking.checked && !trackingGuide && !locked) {
      if (guideCandidate) trackingSource = guideSource(guideCandidate, ignoredId);
      else {
        const contact = hit(raw, acceptedEdge, tolerance, false);
        if (contact) trackingSource = guideSource(contact.point, ignoredId, contact);
      }
    }
    const automatic = pinned || (trackingGuide && (!anchorGuide || trackingGuide.gap < anchorGuide.gap) ? trackingGuide : anchorGuide);
    const returnSnap = () => {
      if (!pinned && automatic && guideGap(snap, automatic) <= guideContactTolerance) visibleGuide = automatic;
      return snap;
    };
    if (snap) {
      if (fields.tracking.checked && !trackingGuide && (!trackingSource || distance(trackingSource.origin, snap) > EPSILON)) {
        trackingSource = guideSource(snap, ignoredId);
      }
      return returnSnap();
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
          if (fraction >= 0 && fraction <= 1) consider(mix(a, b, fraction), "Перпендикуляр");
        }
      }
      if (snap) return returnSnap();
    }
    if (locked && (shiftGuide || snapToEdges)) {
      if (shiftGuide) {
        const edge = hit(raw, acceptedEdge, tolerance, false);
        if (edge && (!reference || guideGap(edge.point, locked) <= guideContactTolerance)) {
          consider(edge.point, snapToEdges ? edgeLabel : "Контур");
        }
        if (snap) return snap;
      }
      const projected = pointOnGuide(raw, locked);
      const referenceProjection = reference && pointOnGuide(reference.point, locked);
      const projectedReference = referenceProjection && usablePoint(referenceProjection) ? referenceProjection : null;
      let intersection = null;
      let matchingReference = null;
      let nearest = tolerance;
      snapAlongGuide(projected, locked, tolerance, ignoredId, candidate => {
        if (!usablePoint(candidate)) return;
        if (projectedReference && distance(candidate, projectedReference) <= guideContactTolerance) matchingReference = candidate;
        const gap = Math.abs(dot(sub(candidate, projected), locked.direction));
        if (gap < nearest) {
          intersection = candidate;
          nearest = gap;
        }
      });
      const directIntersection = intersection && distance(raw, intersection) < tolerance;
      if (projectedReference && !matchingReference && !directIntersection) {
        guideReference = reference.point;
        snap = projectedReference;
        snapType = `${reference.type} · проекция`;
        return snap;
      }
      if (matchingReference || intersection) {
        snap = pointOnGuide(matchingReference || intersection, locked);
        snapType = `${snapToEdges ? edgeLabel : "Контур"} · направляющая`;
        return snap;
      }
    }
    const guidePoint = !locked && automatic && usableGuide(automatic) ? pointOnGuide(raw, automatic) : null;
    if (!locked && snapToEdges) {
      if (guidePoint) {
        snapAlongGuide(guidePoint, automatic, tolerance, ignoredId, consider, `${edgeLabel} · направляющая`);
        if (snap) return returnSnap();
      }
      const edge = hit(raw, acceptedEdge, tolerance, false);
      if (edge) consider(edge.point, edgeLabel);
      if (snap) return returnSnap();
    }
    if (automatic) visibleGuide = automatic;
    else if (!trackingGuide && fields.tracking.checked && trackingSource && distance(raw, trackingSource.origin) < tolerance) {
      guideOrigin = trackingSource.origin;
    }
    if (guidePoint) {
      snap = guidePoint;
      snapType = automatic.label || "Направляющая";
      return snap;
    }
    let point = raw;
    if (fields.grid.checked) {
      const step = gridStep();
      point = point.map((value, index) => {
        const rounded = Math.round(value / step) * step;
        return twoAxis && Math.abs(rounded - anchor[index]) <= CAD_POINT_EPSILON ? value : rounded;
      });
    }
    if (locked) {
      snap = pointOnGuide(point, locked);
      const label = locked.label || `Орто · ${locked.axis ? "горизонталь" : "вертикаль"}`;
      snapType = label + (fields.grid.checked ? " · сетка" : "");
      return snap;
    }
    if (fields.grid.checked) {
      snap = point;
      snapType = "Сетка";
    }
    return point;
  }

  function resetArcPlacement() {
    arcStartContact = arcSymmetry = null;
    arcBulges = [];
  }

  function arcFrame(start = pending[0], end = pending[1]) {
    const chord = sub(end, start);
    return { middle: mix(start, end, .5), normal: mul([-chord[1], chord[0]], 1 / norm(chord)) };
  }

  function nearestArcBulge(height, tolerance) {
    let nearest = null;
    let gap = tolerance;
    for (const candidate of arcBulges) {
      const miss = Math.abs(candidate.height - height);
      if (candidate.height * height > 0 && miss < Math.min(gap, Math.abs(candidate.height) * .35)) {
        nearest = candidate;
        gap = miss;
      }
    }
    return nearest;
  }

  function toolPosition(raw, bypass, shift) {
    if (tool === "mirror" && mirrorMode === "edge") {
      clearSnapGuides();
      const contact = hit(raw, entity => !entity.notch && (entity.type === "line" || entity.type === "polyline"), 10 / scale, false, true);
      mirrorEdge = contactEdge(contact);
      return raw;
    }
    if (tool === "ellipse" && pending.length === 2) {
      const picked = position(raw, null, bypass, false, null);
      const contact = shift && !bypass && guideTarget(raw);
      const reference = contact ? contact.point : picked;
      const label = contact ? "По оси эллипса" : snapType;
      const projected = projectToGuide(reference, pending[0], arcFrame().normal);
      clearSnapGuides();
      return label ? projectedSnap(projected, reference, label) : projected;
    }
    if (tool !== "arc") return position(raw, null, bypass, shift);
    arcSymmetry = null;
    if (pending.length < 2) {
      const point = position(raw, null, bypass, shift);
      if (bypass || shift || fields.ortho.checked || !arcStartContact) return point;
      const contact = hit(raw, cuttingEdge, ARC_CONTOUR_SNAP_PX / scale, false);
      arcSymmetry = symmetricArcEndpoint(arcStartContact, contact, Math.min(CAD_POINT_EPSILON, .25 / scale));
      if (!arcSymmetry || distance(raw, arcSymmetry.point) > ARC_CONTOUR_SNAP_PX / scale) return point;
      clearSnapGuides();
      snap = arcSymmetry.point;
      snapType = ARC_CUES.symmetry.label;
      return snap;
    }
    clearSnapGuides();
    const { middle, normal } = arcFrame();
    const contact = shift && !bypass && guideTarget(raw);
    const picked = contact ? contact.point : raw;
    const height = dot(sub(picked, middle), normal);
    const inference = !bypass && nearestArcBulge(height, (contact ? .25 : ARC_CONTOUR_SNAP_PX) / scale);
    const point = add(middle, mul(normal, inference ? inference.height : height));
    const label = inference ? ARC_CUES[inference.kind].label : contact ? "По оси дуги" : "";
    return label ? projectedSnap(point, contact ? contact.point : null, label) : point;
  }

  function directDistance() {
    if (!pending.length || gesture || running || offsetPreview || notchPreview) return null;
    const anchor = pending.at(-1);
    if (tool === "rectangle") return { anchor };
    if (tool === "arc") return pending.length === 1 ? { anchor, label: "Хорда" } : { anchor: arcFrame().middle, label: "Изгиб" };
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
    if (tool === "arc" && pending.length === 2) {
      const { normal } = arcFrame();
      return add(anchor, mul(normal, directionPoint && dot(vector, normal) < 0 ? -size : size));
    }
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
        if (!end || !middle) return null;
        const { middle: chordMiddle, normal } = arcFrame(start, middle);
        const arc = arcFromBulge(start, middle, dot(sub(end, chordMiddle), normal));
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
      log(({ arc: "Задайте изгиб дуги на расстоянии от хорды.", triangle: "Вершины треугольника должны лежать вне одной прямой.",
        polygon: "Укажите разные точки и целое число сторон от 3 до 100.", ellipse: "Укажите две ненулевые полуоси эллипса.",
      })[tool] || "Укажите разные точки.", true);
      return;
    }
    entity.id = newId();
    if (activeGroup.length) entity.groupPath = activeGroup;
    pending = [];
    shiftGuide = null;
    resetArcPlacement();
    resetDirectDistance();
    snap = cursor = null;
    commit([...current.entities, entity], { ids: [entity.id] });
  }

  function rotationAngle(point, shift = false, free = false) {
    const [pivot, reference] = pending;
    if (distance(point, pivot) * scale <= 2) return null;
    const from = sub(reference, pivot);
    const to = sub(point, pivot);
    const degrees = Math.atan2(cross(from, to), dot(from, to)) / DEG;
    const nearest = Math.round(degrees / ROTATION_SNAP_STEP) * ROTATION_SNAP_STEP;
    return !free && (shift || Math.abs(degrees - nearest) <= 4) ? nearest : degrees;
  }

  function typedRotationAngle() {
    return distanceInput ? Number(distanceInput.replace(",", ".")) : NaN;
  }

  function applyRotation(degrees) {
    if (degrees % 360 === 0) choose("select");
    else applySelectionTransform(angleTransform(pending[0], degrees % 360));
  }

  function advanceRotation(point, event) {
    const [pivot, reference] = pending;
    if (pivot && distanceInput) {
      const typedAngle = typedRotationAngle();
      if (Number.isFinite(typedAngle)) applyRotation(typedAngle);
      else log("Введите угол поворота в градусах.", true);
      return;
    }
    if (pivot && distance(point, pivot) * scale <= 2) {
      log("Укажите направление от центра поворота.", true);
      return;
    }
    if (reference) {
      applyRotation(rotationAngle(point, event?.shiftKey, event?.altKey));
      return;
    }
    pending = [...pending, point];
    cursor = snap = null;
    update();
  }

  function handleRotationInput(event, key) {
    if (tool !== "rotate" || !pending.length || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return false;
    if (/^\d$/.test(key) && distanceInput.length < 24) distanceInput += key;
    else if ((key === "-" || key === "+") && !distanceInput) distanceInput = key;
    else if ((key === "." || key === ",") && !/[.,]/.test(distanceInput)) distanceInput += key;
    else if (key === "backspace" && distanceInput) distanceInput = distanceInput.slice(0, -1);
    else if (key === "enter" && distanceInput) {
      if (!event.repeat) {
        const degrees = typedRotationAngle();
        if (Number.isFinite(degrees)) applyRotation(degrees);
        else log("Введите угол поворота в градусах.", true);
      }
      return true;
    } else return false;
    log();
    draw();
    return true;
  }

  function drawRotationPreview(point) {
    const [pivot, reference] = pending;
    if (!pivot) return;
    const typedAngle = typedRotationAngle();
    const pointerAngle = point && reference ? rotationAngle(point, lastPointer?.shift, lastPointer?.alt) : null;
    const degrees = Number.isFinite(typedAngle) ? typedAngle : pointerAngle;
    const sweep = (degrees ?? 0) % 360;
    if (sweep) drawSelectionPreview(angleTransform(pivot, sweep));

    const [x, y] = screen(pivot);
    const radius = Math.min(94, width / 5, height / 5);
    if (radius < 16) return;
    const start = reference ? angle(sub(reference, pivot)) : 0;
    const ray = (degrees, length) => radial(-degrees, length);
    context.save();
    context.translate(x, y);
    context.strokeStyle = CAD_TRANSFORM_COLOR;
    context.fillStyle = CAD_TRANSFORM_COLOR;
    context.lineWidth = 1.4;
    context.beginPath();
    context.arc(0, 0, radius, 0, 2 * Math.PI);
    context.stroke();

    context.font = "600 11px Inter, Segoe UI, sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.beginPath();
    for (let mark = 0; mark < 360; mark += 15) {
      const major = mark % ROTATION_SNAP_STEP === 0;
      context.moveTo(...ray(start + mark, radius - (major ? 11 : 5)));
      context.lineTo(...ray(start + mark, radius));
      if (major && (radius >= 75 || mark % 90 === 0)) {
        context.fillText(`${mark > 180 ? mark - 360 : mark}°`, ...ray(start + mark, radius - 24));
      }
    }
    context.stroke();

    context.setLineDash([6, 4]);
    context.beginPath();
    if (reference || degrees != null) {
      context.moveTo(0, 0);
      context.lineTo(...ray(start, Math.hypot(width, height)));
    } else if (point) {
      context.moveTo(0, 0);
      context.lineTo(...ray(angle(sub(point, pivot)), Math.max(radius, distance(point, pivot) * scale)));
    }
    if (sweep) {
      context.moveTo(0, 0);
      context.lineTo(...ray(start + sweep, Math.hypot(width, height)));
    }
    context.stroke();
    context.setLineDash([]);
    if (sweep) {
      context.lineWidth = 2;
      context.beginPath();
      context.arc(0, 0, radius / 2, -start * DEG, -(start + sweep) * DEG, sweep > 0);
      context.stroke();
    }
    context.beginPath();
    context.arc(0, 0, 4, 0, 2 * Math.PI);
    context.fill();
    if (degrees != null) {
      context.beginPath();
      context.arc(...ray(start + sweep, radius), 4, 0, 2 * Math.PI);
      context.fill();
    }
    context.restore();

    if (distanceInput || degrees != null) {
      const label = `${distanceInput || format(degrees)}°`;
      const labelPoint = add(pivot, radial(start + sweep, (radius + 18) / scale));
      drawTransformLabel(labelPoint, label);
    }
  }

  function operate(point, event) {
    if (running || offsetPreview || notchPreview || !point.every(Number.isFinite)) return;
    log();
    if (point.some(value => Math.abs(value) > MAX_COORDINATE)) {
      log("Координаты должны быть в пределах ±1 000 000 000 мм.", true);
      return;
    }
    if (tool === "rotate") {
      advanceRotation(point, event);
      return;
    }
    if (tool === "arc") {
      advanceArc(point, event?.altKey);
      return;
    }
    if (tool === "mirror") {
      if (mirrorMode === "points" && !pending.length) {
        pending = [point];
        cursor = snap = null;
        update();
      } else applyMirror(point);
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
      const mark = hit(point, entity => editableInGroup(entity) && editableNotch(entity))?.entity;
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
        const target = hit(point, entity => editableInGroup(entity) && (parentId ? entity.id === parentId : cuttingEdge(entity)));
        if (target) updateNotch(target.point, parentId ? null : target.entity.id);
        else log(parentId ? "Укажите новое место на том же контуре." : "Укажите кромку для привязки надсечки.", true);
      } else log("Выберите надсечку.", true);
      return;
    }
    if (tool === "notch" || tool === "notch-series") {
      const target = hit(point, entity => editableInGroup(entity) && cuttingEdge(entity));
      if (!target) {
        log("Укажите точку на кромке детали.", true);
        return;
      }
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
      return;
    }
    if (!(tool in EDIT)) return;
    if (!pending.length) {
      pending = [point];
      update();
      return;
    }
    const pointTransform = placementTransform(pending[0], point);
    if (pointTransform) applySelectionTransform(pointTransform);
    else log("Точки должны различаться.", true);
  }

  function advanceArc(point, bypass) {
    const [start, end] = pending;
    if (end) {
      build([start, end, point]);
      return;
    }
    if (start && distance(start, point) <= CAD_POINT_EPSILON) {
      log("Укажите второй конец дуги на расстоянии от первого.", true);
      return;
    }
    const contact = bypass ? null : hit(point, cuttingEdge, .25 / scale, false);
    if (start) arcBulges = arcBulgeInferences(start, point, arcStartContact, contact);
    else arcStartContact = contact;
    pending = [...pending, point];
    arcSymmetry = cursor = snap = null;
    resetDirectDistance();
    update();
  }

  function chooseMirrorMode(mode) {
    if (tool !== "mirror" || running) return;
    clearPointerMove();
    mirrorMode = mode;
    mirrorEdge = null;
    pending = [];
    cursor = trackingSource = shiftGuide = null;
    clearSnapGuides();
    lastPointer = null;
    log();
    update();
    canvas.focus({ preventScroll: true });
  }

  function applyMirror(point) {
    if (running) return;
    const axis = currentMirrorAxis(point);
    if (!axis) {
      log(MIRROR_AXIS_WARNING[mirrorMode] || "Укажите положение оси.", true);
      return;
    }
    applySelectionTransform(reflectionTransform(...axis));
  }

  function applySelectionTransform(pointTransform) {
    const ids = familyIds(selected);
    const newIds = tool === "copy" ? new Map(Array.from(ids, id => [id, newId()])) : null;
    const copiedGroups = new Map();
    const changed = Array.from(ids, id => {
      const entity = byId.get(id);
      const result = transform(entity, pointTransform, tool === "mirror");
      if (newIds) {
        result.id = newIds.get(id);
        if (result.groupPath) result.groupPath = result.groupPath.map((group, index) => {
          if (index < activeGroup.length) return group;
          if (!copiedGroups.has(group)) copiedGroups.set(group, newId());
          return copiedGroups.get(group);
        });
        if (result.parentId && newIds.has(result.parentId)) result.parentId = newIds.get(result.parentId);
        else if (result.parentId) {
          delete result.parentId;
          if (activeGroup.length) result.groupPath = activeGroup;
        }
      } else if (result.parentId && !ids.has(result.parentId)) {
        delete result.parentId;
        if (activeGroup.length) result.groupPath = activeGroup;
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
      const { entities, warnings = [], selectedIds = [], changed, diagnostics = [], diagnosticIds } = result;
      if (operation === "notch-series") {
        notchPreview = { entities, ids: selectedIds, warnings };
      } else if (operation === "trim-quick") {
        if (changed) commit(entities, { warnings: [...current.warnings, ...warnings], ids: [] });
        else if (warnings.length) log(warnings[0]?.message || String(warnings[0]));
      } else if (operation === "repair-conflicts") {
        if (changed) commit(entities, { ids: selectedIds });
        const feedback = warnings.map(warning => warning?.message || String(warning)).join(" ");
        if (diagnostics.length) showRepairDiagnostics(diagnostics, diagnosticIds ?? selectedIds, feedback);
        else if (!changed && repairLocations.length) showRepairDiagnostics(repairLocations, repairSourceIds, feedback);
        else log(feedback || "Проверка завершена.");
        canvas.focus({ preventScroll: true });
      } else if (["repair-part", "repair-conflict", "close-outline", "simplify"].includes(operation)) {
        if (changed) commit(entities, { ids: selectedIds });
        const feedback = warnings.map(warning => warning?.message || String(warning)).join(" ");
        if (!changed && diagnostics.length) showRepairDiagnostics(diagnostics, options.ids);
        else if (operation === "repair-conflict" && !changed && repairLocations.length) {
          repairLocations[repairLocationIndex] = { ...repairLocations[repairLocationIndex], fixable: false,
            feedback: feedback || "Здесь требуется ручная правка." };
          updateRepairDiagnostic();
        } else {
          log(feedback || (operation === "simplify" ? "Контур упрощён." : "Контур проверен."));
          if (operation === "repair-conflict") canvas.focus({ preventScroll: true });
        }
      } else {
        choose("select");
        commit(entities, { warnings: [...current.warnings, ...warnings], ids: selectedIds });
      }
    });
  }

  function updateNotch(point, parentId) {
    const depth = numberFrom(fields.option);
    if (notchTarget && depth > 0) void editOnServer("notch-edit", { id: notchTarget, depth, ...(point ? { point } : {}), ...(parentId ? { parentId } : {}) });
    else log("Введите положительную глубину надсечки.", true);
  }

  function offsetContact(point) {
    const contours = selectedOutlines().filter(editableInGroup);
    const ids = new Set(contours.map(entity => entity.id));
    return hit(point, entity => editableInGroup(entity) && cuttingEdge(entity) && (!ids.size || ids.has(entity.id)));
  }

  function startOffset(point) {
    const contact = offsetContact(point);
    if (!contact) {
      log("Щёлкните по линии контура.", true);
      return;
    }
    const { entity, point: anchor } = contact;
    const path = geometry(entity).path;
    const edge = contactEdge(contact);
    const tangent = arcContactTangent(contact, anchor);
    const direction = tangent ? mul(tangent, Math.sign(edge?.arc?.sweep ?? entity.sweep ?? 1))
      : contact.direction || nearestSegment(anchor, path)?.direction;
    const length = direction ? norm(direction) : 0;
    if (length <= EPSILON) return;
    const contours = selectedOutlines().filter(editableInGroup);
    const originals = new Set(contours.length ? contours.map(item => item.id) : [entity.id]);
    const family = familyIds(originals);
    const source = current.entities.filter(item => family.has(item.id));
    const closed = entity.type === "circle" || entity.closed === true || entity.type === "ellipse" && Math.abs(entity.sweep) === 360;
    const winding = closed ? path.reduce((sum, item, index) =>
      sum + cross(sub(item, anchor), sub(path[(index + 1) % path.length], anchor)), 0) : 0;
    const normalSign = closed && winding >= 0 ? -1 : 1;
    const state = {
      originals, family, source, anchor, left: [-direction[1] / length, direction[0] / length],
      options: originals.size === 1 ? { id: entity.id } : { ids: [...originals] },
      normalSign, closed, side: 1, pointerDistance: 0, target: 0, result: null, requested: null,
      timer: null, controller: new AbortController(), error: "", applying: false, findingAnchor: originals.size > 1,
    };
    offsetPreview = state;
    fields["offset-distance"].value = "";
    cursor = point;
    hover = null;
    log();
    update();
    if (state.findingAnchor) void readOffsetAnchor(state, entity.id);
  }

  function offsetRequest(state, operation, options) {
    return request("/api/cad/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entities: state.source, operation, options }),
      signal: AbortSignal.any([state.controller.signal, AbortSignal.timeout(15_000)]),
    });
  }

  async function readOffsetAnchor(state, anchorId) {
    try {
      const { normalSign, closed } = await offsetRequest(state, "offset-anchor", { ...state.options, anchorId });
      if (offsetPreview !== state) return;
      state.normalSign = normalSign;
      state.closed = closed;
      state.findingAnchor = false;
      updateOffsetDistance(cursor);
    } catch (error) {
      if (offsetPreview !== state) return;
      clearOffset();
      log(errorMessage(error), true);
      update();
    }
  }

  function clearOffset() {
    if (!offsetPreview) return;
    clearTimeout(offsetPreview.timer);
    offsetPreview.controller.abort();
    offsetPreview = null;
  }

  function offsetSide(state) {
    return state.closed ? state.side > 0 ? "Наружу" : "Внутрь" : state.side > 0 ? "Слева" : "Справа";
  }

  function renderOffsetControls() {
    const state = offsetPreview;
    views["offset-preview"].hidden = !state;
    if (!state) return;
    const input = fields["offset-distance"];
    input.disabled = state.findingAnchor || state.applying;
    input.placeholder = state.pointerDistance ? format(state.pointerDistance) : "0";
    actions["offset-side"].textContent = offsetSide(state);
    actions["offset-side"].disabled = input.disabled;
    actions.apply.disabled = input.disabled || !state.target;
    const warnings = state.result?.distance === state.target && state.result.warnings
      .map(warning => warning?.message || String(warning)).join(" ");
    const message = state.findingAnchor ? "Проверяем стыки контура…" : state.applying ? "Строим припуск…"
      : state.error || warnings || "";
    if (views["preview-warning"].textContent !== message) views["preview-warning"].textContent = message;
    views["preview-warning"].hidden = !message;
  }

  function drawOffsetGuide() {
    const state = offsetPreview;
    const anchor = state?.anchor || cursor;
    if (!anchor) return;
    const [x, y] = screen(anchor);
    context.save();
    context.strokeStyle = CAD_SNAP_COLOR;
    context.lineWidth = 1.5;
    context.strokeRect(x - 4, y - 4, 8, 8);
    context.restore();
    if (!state?.target) return;
    const end = add(anchor, mul(state.left, state.target * state.normalSign));
    drawTransformGuide(anchor, end);
    drawTransformLabel(end, `${offsetSide(state)} · ${formatLength(Math.abs(state.target))}`);
  }

  function setOffsetTarget(apply = false) {
    const state = offsetPreview;
    const input = fields["offset-distance"];
    if (!state || state.findingAnchor || state.applying) return;
    const distance = input.value.trim() ? validSize(input.value.trim()) : state.pointerDistance;
    const target = distance > CAD_POINT_EPSILON && distance <= MAX_COORDINATE ? state.side * distance : 0;
    if (target !== state.target) state.error = "";
    state.target = target;
    if (apply && !target) {
      state.error = "Укажите ненулевое смещение в миллиметрах.";
    } else if (target) {
      state.applying = apply;
      if (apply && state.result?.distance === target) {
        commitOffset(state.result);
        return;
      }
      if (state.requested === null && state.result?.distance !== target && (apply || !state.error && state.timer === null)) {
        if (apply) clearTimeout(state.timer);
        state.timer = setTimeout(() => void calculateOffset(state), apply ? 0 : 45);
      }
    }
    renderOffsetControls();
    draw();
  }

  function updateOffsetDistance(point) {
    const state = offsetPreview;
    if (!state || state.findingAnchor || state.applying) return;
    if (point) {
      const signed = dot(sub(point, state.anchor), state.left) * state.normalSign;
      if (Math.abs(signed) * scale > 2) state.side = Math.sign(signed);
      state.pointerDistance = Math.abs(signed) * scale > 2 ? Number(Math.abs(signed).toFixed(3)) : 0;
    }
    setOffsetTarget();
  }

  async function calculateOffset(state) {
    state.timer = null;
    const distance = state.target;
    if (offsetPreview !== state || !distance || state.result?.distance === distance) return;
    state.requested = distance;
    try {
      const { entities, selectedIds, warnings = [] } = await offsetRequest(state, "offset",
        { ...state.options, distance, keepOriginal: true });
      if (offsetPreview !== state) return;
      const updated = new Map(entities.map(entity => [entity.id, entity]));
      state.result = { distance, ids: new Set(selectedIds), warnings,
        entities: [...current.entities.map(entity => updated.get(entity.id) || entity),
          ...entities.filter(entity => !state.family.has(entity.id))] };
      if (state.applying && state.target === distance) {
        commitOffset(state.result);
        return;
      }
    } catch (error) {
      if (offsetPreview !== state) return;
      if (state.target === distance) {
        state.applying = false;
        state.error = errorMessage(error);
        state.result = null;
      }
    } finally {
      if (offsetPreview === state) {
        state.requested = null;
        if (state.target && state.target !== distance) void calculateOffset(state);
        renderOffsetControls();
        draw();
      }
    }
  }

  function previewEntities() {
    const { result, target } = offsetPreview;
    return !offsetPreview.error && Math.sign(result?.distance ?? 0) === Math.sign(target) && result
      ? result.entities : current.entities;
  }

  function commitOffset({ entities, warnings, ids }) {
    choose("select");
    commit(entities, { warnings: [...current.warnings, ...warnings], ids });
  }

  function setReference() {
    const items = selectedEntities();
    if (!items.length) return;
    const reference = !items.every(entity => entity.reference);
    const ids = familyIds(selected);
    commit(current.entities.map(entity => ids.has(entity.id) && entity.type !== "point" ? { ...entity, reference } : entity));
  }

  function removeSelected() {
    if (running || gesture || offsetPreview || notchPreview) return;
    const ids = familyIds(selected);
    if (!ids.size) {
      if (selectedEdges.size) {
        log("Выбрано ребро. Для правки стыка откройте «Восстановить деталь». Чтобы удалить весь контур, выделите его двойным щелчком.");
      }
      return;
    }
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
    } else if (event.button === 0 && !running && !notchPreview) {
      const point = world(start);
      if (tool === "offset") {
        if (!offsetPreview) startOffset(point);
        else {
          updateOffsetDistance(point);
          setOffsetTarget(true);
        }
        return;
      } else if (tool === "scale") {
        const handle = scaleBox && scaleHandleAt(start);
        if (!handle) return;
        scaleSide = handle;
        fields.option.value = format(Math.abs(handle.point[handle.axis] - handle.origin[handle.axis]));
        gesture = { type: "scale", start, pointerId: event.pointerId };
        update();
      } else if (tool !== "select" && tool !== "erase") {
        const pickedPoint = tool.startsWith("notch") ? point : toolPosition(point, event.altKey, event.shiftKey);
        if (tool === "rectangle" && pending.length && rectangleEditing) {
          cursor = pickedPoint;
          updateDistanceDirection(point, event.shiftKey);
          const exact = typedDistancePoint();
          if (exact) operate(exact);
          else if (!rectangleSizes.some(Boolean)) operate(pickedPoint);
          else log("Задайте положительные размеры сторон A и B.", true);
        } else if ((tool in EDIT || tool === "arc") && pending.length && distanceInput) {
          cursor = pickedPoint;
          updateDistanceDirection(point, event.shiftKey);
          const exact = typedDistancePoint();
          if (exact) operate(exact, event);
          else log("Введите положительный размер в миллиметрах.", true);
        } else operate(pickedPoint, event);
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
    lastPointer = { at, alt: event.altKey, shift: event.shiftKey };
    if (gesture?.type === "pan") {
      const [x, y] = sub(at, gesture.start);
      center = add(gesture.center, [-x / scale, y / scale]);
    } else if (gesture?.type === "marquee") {
      gesture.end = at;
    } else if (gesture?.type === "erase") {
      addErasePoint(gesture.points, point);
    } else if (gesture?.type === "scale") {
      if (distance(gesture.start, at) > 3) updateScaleGesture(point, event.shiftKey, event.altKey);
      else gesture.parameters = null;
    } else if (gesture?.type === "grip") {
      updateGrip(point, event.altKey, event.shiftKey);
    }
    const location = world(at);
    if (tool === "offset") {
      if (gesture) draw();
      else if (offsetPreview) {
        cursor = location;
        updateOffsetDistance(location);
      } else {
        const contact = offsetContact(location);
        hover = contact?.entity || null;
        cursor = contact?.point || null;
        draw();
      }
      return;
    }
    const picking = tool === "select" || tool.startsWith("notch") || tool === "erase" && event.shiftKey;
    hover = !gesture && !offsetPreview && !notchPreview && picking ? pickAt(location)?.entity : null;
    if (gesture?.type === "scale" && gesture.parameters) {
      cursor = snap || location;
    } else if ((picking || tool === "erase" || tool === "scale") && gesture?.type !== "grip") {
      cursor = location;
      clearSnapGuides();
    } else if (gesture?.type !== "grip") cursor = toolPosition(location, event.altKey, event.shiftKey);
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
      if (shift && shiftGuide) {
        const { direction } = shiftGuide;
        directionPoint = add(anchor, mul(direction, Math.sign(dot(sub(raw, anchor), direction)) || 1));
      } else if (shift || fields.ortho.checked) directionPoint = Math.abs(dx) >= Math.abs(dy)
        ? [raw[0], anchor[1]] : [anchor[0], raw[1]];
      else directionPoint = raw;
    }
  }

  function cancelGesture() {
    if (!gesture) return;
    const { pointerId, type } = gesture;
    gesture = null;
    if (type === "grip" || type === "scale") {
      shiftGuide = null;
      clearSnapGuides();
    }
    delete canvas.dataset.gesture;
    if (canvas.hasPointerCapture(pointerId)) canvas.releasePointerCapture(pointerId);
  }

  function selectHit(item, shift, segment) {
    const id = item?.entity.id;
    const group = item && groupAt(item.entity);
    const wasWhole = selected.has(id);
    const edges = selectedEdges.get(id);
    const repeated = edges?.has(item.edge);
    const members = group ? groupMembers(group) : [];
    const wasGroupSelected = shift && members.some(entity => selected.has(entity.id));
    if (!shift) {
      selected.clear();
      selectedEdges.clear();
    }
    if (!item) return;
    if (group) {
      for (const entity of members) {
        if (wasGroupSelected) selected.delete(entity.id);
        else selected.add(entity.id);
      }
    } else if (shift && wasWhole && !segment) {
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

  function contourIds(seed) {
    const ids = new Set([seed.id]);
    const endpoints = entity => {
      if (entity.closed || entity.type === "point" || entity.type === "circle" || Math.abs(entity.sweep) >= 360 - EPSILON) return null;
      const path = geometry(entity).path;
      return path.length >= 2 && distance(path[0], path.at(-1)) > EPSILON ? [path[0], path.at(-1)] : null;
    };
    if (!editableInGroup(seed) || seed.notch || !endpoints(seed)) return ids;

    const tolerance = CONTOUR_ENDPOINT_TOLERANCE_MM;
    const buckets = new Map();
    const ends = new Map();
    for (const entity of current.entities) {
      if (!editableInGroup(entity) || entity.notch || Boolean(entity.reference) !== Boolean(seed.reference)
          || (entity.layer || "") !== (seed.layer || "")) continue;
      const pair = endpoints(entity);
      if (!pair) continue;
      ends.set(entity.id, pair.map(point => {
        const [x, y] = point.map(value => Math.floor(value / tolerance));
        const end = { id: entity.id, point, x, y };
        const key = `${x},${y}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(end);
        return end;
      }));
    }
    function nextEnd(end) {
      let exact = null;
      let nearby = null;
      let ambiguous = false;
      for (let x = end.x - 1; x <= end.x + 1; x++) {
        for (let y = end.y - 1; y <= end.y + 1; y++) {
          for (const candidate of buckets.get(`${x},${y}`) || []) {
            if (candidate.id === end.id) continue;
            const gap = distance(end.point, candidate.point);
            if (gap <= CONTOUR_EXACT_ENDPOINT_TOLERANCE_MM) {
              if (exact) return null;
              exact = candidate;
            } else if (gap <= tolerance) {
              ambiguous ||= Boolean(nearby);
              nearby = candidate;
            }
          }
        }
      }
      return exact || (ambiguous ? null : nearby);
    }
    for (const id of ids) {
      for (const end of ends.get(id)) {
        const match = nextEnd(end);
        // Both ends must agree so a near-coincident branch cannot pull in another contour.
        if (match && !ids.has(match.id) && nextEnd(match) === end) ids.add(match.id);
      }
    }
    return ids;
  }

  function selectContour(ids) {
    selected = ids;
    selectedEdges.clear();
    update();
  }

  function closeContextMenu(restoreFocus = false) {
    if (contextMenu.hidden) return;
    contextMenu.hidden = true;
    contextMenu.replaceChildren();
    if (restoreFocus) canvas.focus({ preventScroll: true });
  }

  function menuControl(control, icon, label = control.getAttribute("aria-label")) {
    return { label, icon, disabled: control.disabled, run: () => control.click() };
  }

  function selectionMenu(item) {
    const { entity, point, edge } = item;
    const groupCommands = [];
    if (editableInGroup(entity) && !entity.notch && entity.type !== "point") {
      const ids = contourIds(entity);
      if (ids.size > 1 || selectedEdges.has(entity.id)) {
        groupCommands.push({ label: "Выбрать контур", icon: "select", run: () => selectContour(ids) });
      }
      const segment = isRecoveredSegment(entity);
      groupCommands.push({ label: segment ? "Оставить отрезок" : "Восстановить деталь", icon: "repair",
        run: () => editOnServer("repair-part", { ids: segment ? [entity.id] : [...ids] }) });
      const pickedIds = [...new Set([...selected, ...selectedEdges.keys()])];
      if (!actions["repair-part"].hidden && (pickedIds.length > 1 || entity.closed)) {
        groupCommands.push({ label: "Исправить всё", icon: "repair",
          run: () => editOnServer("repair-conflicts", { ids: pickedIds }) });
      }
    }
    if (!actions.join.hidden) groupCommands.push(menuControl(actions.join, "join"));
    if (!actions["close-outline"].disabled) groupCommands.push(menuControl(actions["close-outline"], "close-outline"));
    if (!actions.ungroup.hidden) groupCommands.push(menuControl(actions.ungroup, "ungroup"));
    const pointCommands = [];
    if (editableInGroup(entity) && !entity.notch && ["line", "polyline", "spline"].includes(entity.type)) {
      const target = { id: entity.id, point, ...(entity.type === "polyline" ? { edge } : {}) };
      if (edge != null && !selectedEdges.get(entity.id)?.has(edge)) {
        pointCommands.push({ label: "Выбрать сегмент", icon: "select", run: () => {
          selectHit(item, false, true);
          update();
        } });
      }
      if (simplifiable(entity)) {
        pointCommands.push({ label: "Упростить контур", icon: "polyline", run: () => editOnServer("simplify", { ids: [entity.id] }) });
      }
      pointCommands.push({ label: "Добавить точку", icon: "point", run: () => editOnServer("insert-points", target) });
      pointCommands.push({ label: entity.type === "polyline" ? "Разбить сегмент…" : "Добавить несколько точек", icon: "polyline", run: () => {
        insertPointsTarget = target;
        fields.option.value = "3";
        openParameters();
      } });
    }
    return [groupCommands, pointCommands, [menuControl(buttons.move, "move"), menuControl(buttons.copy, "copy")]];
  }

  function drawingMenu() {
    const currentCommand = [];
    if (!views.finish.hidden) {
      currentCommand.push(menuControl(actions.finish, "polyline", "Завершить"), menuControl(actions.close, "rectangle", "Замкнуть"));
    }
    if (tool !== "select") currentCommand.push({ label: "Отменить команду", icon: "select", run: () => choose("select") });
    const drawCommands = Object.entries(DRAW).map(([key, { label }]) => menuControl(buttons[key], key, label));
    const viewCommands = [menuControl(actions.fit, "fit", "Вписать чертёж"), menuControl(actions.undo, "undo"), menuControl(actions.redo, "redo")];
    return [currentCommand, drawCommands, viewCommands];
  }

  function openContextMenu(at) {
    if (running || gesture || offsetPreview || notchPreview || parameterDialog.open) return;
    closeContextMenu();
    flushPointerMove();
    showTools(false);
    const item = tool === "select" ? hit(world(at), inActiveGroup) : null;
    if (item && !selected.has(item.entity.id) && !selected.has(item.entity.parentId) && !selectedEdges.has(item.entity.id)) {
      selectHit(item, false, false);
      update();
    }
    const groups = item ? selectionMenu(item) : drawingMenu();
    contextMenu.setAttribute("aria-label", item ? "Команды выделения" : "Инструменты чертежа");
    for (const group of groups.filter(commands => commands.length)) {
      if (contextMenu.childElementCount) {
        const separator = root.createElement("hr");
        separator.setAttribute("role", "separator");
        contextMenu.append(separator);
      }
      for (const command of group) {
        const button = root.createElement("button");
        button.type = "button";
        button.setAttribute("role", "menuitem");
        button.tabIndex = -1;
        button.disabled = Boolean(command.disabled);
        button.innerHTML = cadIcon(command.icon);
        button.append(root.createTextNode(command.label));
        button.addEventListener("click", () => {
          closeContextMenu(true);
          command.run();
        }, { once: true });
        contextMenu.append(button);
      }
    }
    contextMenu.hidden = false;
    const bx = Math.max(6, Math.min(at[0], canvas.clientWidth - contextMenu.offsetWidth - 6));
    const by = Math.max(6, Math.min(at[1], canvas.clientHeight - contextMenu.offsetHeight - 6));
    contextMenu.style.left = `${bx}px`;
    contextMenu.style.top = `${by}px`;
    contextMenu.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
  }

  function completeGesture(event, cancelled = false) {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    if (cancelled) clearPointerMove();
    else flushPointerMove();
    const completed = gesture;
    const end = screenEvent(event);
    const dragged = distance(completed.start, end) > 3;
    if (!cancelled && completed.type === "grip" && dragged) updateGrip(world(end), event.altKey, event.shiftKey);
    if (!cancelled && completed.type === "scale" && dragged) updateScaleGesture(world(end), event.shiftKey, event.altKey);
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
      void editOnServer("trim-quick", { stroke: completed.points, tolerance: 8 / scale, groupPath: activeGroup });
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
        const groups = new Map();
        const include = id => { selectedEdges.delete(id); selected.add(id); };
        for (const entity of current.entities) {
          if (!inActiveGroup(entity)) continue;
          const group = groupAt(entity);
          const matches = enclosed(geometry(entity).path, box, crossing);
          if (group) {
            if (!groups.has(group)) groups.set(group, { ids: [], matches: [] });
            const entry = groups.get(group);
            if (!entity.parentId) entry.ids.push(entity.id);
            entry.matches.push(matches);
          } else if (matches) include(entity.id);
        }
        for (const { ids, matches } of groups.values()) {
          if (crossing ? matches.some(Boolean) : matches.every(Boolean)) ids.forEach(include);
        }
      } else {
        selectHit(hit(world(end), inActiveGroup), completed.shift, event.altKey);
      }
    }
    update();
  }

  const actionHandlers = {
    back: () => { if (!running) onBack(); },
    tools: () => showTools(toolsPanel.hidden),
    ...Object.fromEntries(Object.keys(MIRROR_MODES).map(mode => [`mirror-${mode}`, () => chooseMirrorMode(mode)])),
    "mirror-cancel": () => choose("select"),
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
    "close-outline": () => editOnServer("close-outline", { ids: selectedOutlines().map(entity => entity.id) }),
    "show-repair-location": focusRepairLocation,
    "fix-repair-location": () => {
      const location = repairLocations[repairLocationIndex];
      if (gesture || location?.fixable !== true) return;
      const { point, kind, sourceOperation } = location;
      void editOnServer("repair-conflict", { ids: location.ids ?? repairSourceIds, point, kind, sourceOperation });
    },
    "fix-all-repair-locations": () => {
      if (!gesture && !repairBulkChecked && repairSourceIds.length) {
        void editOnServer("repair-conflicts", { ids: repairSourceIds });
      }
    },
    "close-repair-diagnostics": () => {
      clearRepairDiagnostics();
      draw();
      canvas.focus({ preventScroll: true });
    },
    simplify: () => editOnServer("simplify", { ids: [...new Set([...selected, ...selectedEdges.keys()])]
      .filter(id => simplifiable(byId.get(id))) }),
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
    join: groupSelected,
    ungroup: ungroupSelected,
    "exit-group": exitGroup,
    finish: () => build(pending),
    close: () => build(pending, true),
    reference: setReference,
    apply: () => setOffsetTarget(true),
    "offset-side": () => {
      if (!offsetPreview) return;
      offsetPreview.side *= -1;
      setOffsetTarget();
    },
    "cancel-preview": () => choose("select"),
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
    if (!contextMenu.contains(event.target)) closeContextMenu();
  }, listener);
  const shortcutControls = { ...buttons, ...Object.fromEntries(["import", "save", "part", "join", "close-outline", "undo", "redo", "fit", "reference"]
    .filter(key => !actions[key].hidden).map(key => [key, actions[key]])) };
  const defaultKeys = { select: "V", line: "L", polyline: "P", circle: "C", arc: "A", spline: "S", point: "Q",
    rectangle: "R", square: "Shift+R", triangle: "T", polygon: "G", ellipse: "O", move: "M", copy: "Shift+C",
    rotate: "Shift+T", scale: "K", mirror: "Shift+M", notch: "N", "notch-series": "Shift+N", "notch-edit": "Alt+N",
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
      const { entities, warnings = [], diagnostics = [], diagnosticIds = [], repairSummary } = imported;
      fields.name.value = imported.name || file.name.replace(/\.[^.]+$/, "");
      measurements.length = 0;
      choose("select");
      commit(entities, { warnings, ids: [] });
      fit();
      views.warnings.open = !views.warnings.hidden;
      const summary = [`Импортировано объектов: ${entities.length}`, repairSummary].filter(Boolean).join(". ");
      if (diagnostics.length) showRepairDiagnostics(diagnostics, diagnosticIds, summary);
      else log(summary);
    });
  }, listener);
  const offsetInput = fields["offset-distance"];
  offsetInput.addEventListener("input", () => setOffsetTarget(), listener);
  offsetInput.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      setOffsetTarget(true);
    }
  }, listener);
  for (const field of [fields.option, fields.proportional]) {
    field.addEventListener("input", () => { if (tool === "scale" && parameterDialog.open) draw(); }, listener);
  }
  toolsPanel.addEventListener("change", () => {
    updateSnapButton();
    if (!fields.tracking.checked) trackingSource = null;
    clearSnapGuides();
    draw();
  }, listener);
  function submitParameters(relocate = false) {
    const value = numberFrom(fields.option);
    if (insertPointsTarget) {
      if (!Number.isInteger(value) || value < 2 || value > MAX_INSERT_POINTS) {
        parameterError(`Введите целое число от 2 до ${MAX_INSERT_POINTS}.`);
        return;
      }
      const target = insertPointsTarget;
      insertPointsTarget = null;
      closeParameters();
      void editOnServer("insert-points", { ...target, count: value });
      return;
    }
    if (tool === "scale" && !(value > CAD_POINT_EPSILON && value <= MAX_COORDINATE)) {
      parameterError("Введите положительный размер в миллиметрах.");
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
      } else if (tool === "notch-edit" && !relocate) updateNotch();
      else {
        placingNotch = tool === "notch-edit" && relocate;
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
  canvas.addEventListener("dblclick", event => {
    if (tool !== "select" || running || gesture || offsetPreview || notchPreview || event.shiftKey || event.altKey) return;
    const item = hit(world(screenEvent(event)), inActiveGroup);
    const group = item && groupAt(item.entity);
    if (group) enterGroup(group);
    else if (item && !item.entity.notch && item.entity.type !== "point") selectContour(contourIds(item.entity));
    else if (!item) exitGroup();
  }, listener);
  canvas.addEventListener("pointerleave", () => {
    flushPointerMove();
    cursor = null;
    lastPointer = null;
    hover = null;
    mirrorEdge = null;
    clearSnapGuides();
    draw();
  }, listener);
  canvas.addEventListener("contextmenu", event => {
    event.preventDefault();
    openContextMenu(screenEvent(event));
  }, listener);
  contextMenu.addEventListener("contextmenu", event => event.preventDefault(), listener);
  contextMenu.addEventListener("focusout", event => {
    if (!contextMenu.contains(event.relatedTarget)) closeContextMenu();
  }, listener);
  contextMenu.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeContextMenu(true);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = [...contextMenu.querySelectorAll("button:not(:disabled)")];
    if (!items.length) return;
    const currentIndex = items.indexOf(root.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : (currentIndex + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
    event.preventDefault();
    event.stopPropagation();
    items[next].focus();
  }, listener);
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    if (gesture) return;
    closeContextMenu();
    flushPointerMove();
    const at = screenEvent(event);
    const anchor = world(at);
    const delta = event.deltaY * (event.deltaMode === 1 ? 15 : 1);
    const next = clampViewScale(scale * Math.exp(-delta * .0015), anchor);
    if (next !== scale) {
      scale = next;
      center = [anchor[0] - (at[0] - width / 2) / scale, anchor[1] + (at[1] - height / 2) / scale];
    }
    movePointer(event);
  }, { ...listener, passive: false });
  function redrawOrthogonal(shift, alt) {
    flushPointerMove();
    if (!shift) shiftGuide = null;
    if (!lastPointer) return;
    lastPointer.shift = shift;
    if (alt !== undefined) lastPointer.alt = alt;
    const point = world(lastPointer.at);
    if (gesture?.type === "grip") {
      updateGrip(point, lastPointer.alt, shift);
      cursor = snap || point;
    } else if (gesture?.type === "scale" && gesture.parameters) {
      updateScaleGesture(point, shift, lastPointer.alt);
      cursor = snap || point;
    } else if (!gesture && (tool === "arc" || tool === "mirror" || pending.length && (tool in DRAW || tool in EDIT || tool === "measure"))) {
      cursor = toolPosition(point, lastPointer.alt, shift);
      updateDistanceDirection(point, shift);
    } else return;
    draw();
  }
  canvas.addEventListener("blur", () => {
    spacePressed = false;
    lastPointer = shiftGuide = null;
  }, listener);
  canvas.addEventListener("keydown", event => {
    if (event.key === "ContextMenu" || event.key === "F10" && event.shiftKey) {
      event.preventDefault();
      event.stopPropagation();
      openContextMenu(lastPointer?.at || [width / 2, height / 2]);
      return;
    }
    if (event.key === "Shift" && !event.repeat) redrawOrthogonal(true);
    if (event.key === "Alt" && !event.repeat) redrawOrthogonal(event.shiftKey, true);
  }, listener);
  canvas.addEventListener("keyup", event => {
    if (event.code === "Space") spacePressed = false;
    if (event.key === "Shift") redrawOrthogonal(event.shiftKey);
    if (event.key === "Alt") redrawOrthogonal(event.shiftKey, false);
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
    const key = event.key.toLowerCase();
    if (offsetPreview && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing) {
      if (key === "enter") {
        if (!event.repeat) setOffsetTarget(true);
      } else if (key === "backspace") {
        offsetInput.value = offsetInput.value.slice(0, -1);
        setOffsetTarget();
      } else if (/^\d$/.test(key) || ((key === "." || key === ",") && !/[.,]/.test(offsetInput.value))) {
        if (offsetInput.value.length < offsetInput.maxLength) offsetInput.value += key;
        setOffsetTarget();
      } else if (key === "escape") {
        choose("select");
      } else {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    const mirrorEnter = key === "enter" && tool === "mirror" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing;
    if (mirrorEnter && !event.repeat) applyMirror(cursor);
    if (mirrorEnter || handleRotationInput(event, key) || handleDirectDistance(event, key)) {
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
      if (tool === "select" && activeGroup.length && !gesture && !offsetPreview && !notchPreview) {
        exitGroup();
        return;
      }
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
  updateSnapButton();
  updateWarnings(current.warnings);
  update();
  canvas.focus({ preventScroll: true });
  return {
    hasUnsavedChanges: () => saved !== current || savedName !== fields.name.value.trim(),
    isBusy: () => running || Boolean(offsetPreview?.applying),
    destroy() {
      clearPointerMove();
      clearOffset();
      lifecycle.abort();
      observer.disconnect();
      container.classList.remove("cad-editor");
      container.replaceChildren();
    },
  };
}
