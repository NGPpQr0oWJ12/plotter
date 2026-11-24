import { Vector3 } from "./three-3d.js";

const ink = "#173c73";
const millimeters = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });

function clipRange(a, b, minimum, maximum, span) {
  const delta = b - a;
  if (delta === 0) return a >= minimum && a <= maximum;
  const first = (minimum - a) / delta;
  const last = (maximum - a) / delta;
  span[0] = Math.max(span[0], Math.min(first, last));
  span[1] = Math.min(span[1], Math.max(first, last));
  return span[0] <= span[1];
}

function interpolate(a, b, fraction) {
  return a.map((coordinate, index) => coordinate + (b[index] - coordinate) * fraction);
}

export function canvasProjection(camera, width, height) {
  const vector = new Vector3();
  const near = -camera.near;
  const far = -camera.far;

  function screen(point) {
    const result = vector.fromArray(point).applyMatrix4(camera.projectionMatrix);
    const x = (result.x + 1) * width / 2;
    const y = (1 - result.y) * height / 2;
    return Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
  }

  const projection = {
    view(point) {
      if (!point || point.length !== 3 || !point.every(Number.isFinite)) return null;
      const view = vector.fromArray(point).applyMatrix4(camera.matrixWorldInverse).toArray();
      return view.every(Number.isFinite) ? view : null;
    },
    point(view) {
      if (!view || view[2] < far || view[2] > near) return null;
      const point = screen(view);
      if (!point || point[0] < 0 || point[0] > width || point[1] < 0 || point[1] > height) return null;
      return point;
    },
    segment(a, b) {
      if (!a || !b) return null;
      // Clip depth before perspective division, including segments that cross the camera plane.
      const depth = [0, 1];
      if (!clipRange(a[2], b[2], far, near, depth)) return null;
      const start = screen(interpolate(a, b, depth[0]));
      const end = screen(interpolate(a, b, depth[1]));
      const span = [0, 1];
      if (!start || !end || !clipRange(start[0], end[0], 0, width, span)
          || !clipRange(start[1], end[1], 0, height, span)) return null;
      return [interpolate(start, end, span[0]), interpolate(start, end, span[1])];
    },
    guide(a, b) {
      if (!a || !b) return null;
      const along = new Vector3(...b).sub(new Vector3(...a));
      if (along.lengthSq() < 1e-12) return null;
      along.normalize();
      // Extend past the frustum's bounding sphere in view space, in both directions.
      const matrix = camera.projectionMatrix.elements;
      const extent = camera.far * Math.hypot(1, (1 + Math.abs(matrix[8])) / matrix[0],
        (1 + Math.abs(matrix[9])) / matrix[5]);
      const center = new Vector3(...a);
      center.addScaledVector(along, -center.dot(along));
      return projection.segment(center.clone().addScaledVector(along, -extent).toArray(),
        center.addScaledVector(along, extent).toArray());
    },
  };
  return projection;
}

function stroke(context, trace, color, strong, dashed = false, fill = false) {
  context.beginPath();
  trace();
  context.setLineDash(dashed ? [5, 4] : []);
  if (fill) {
    context.fillStyle = "#fff";
    context.fill();
  }
  const width = strong ? 2 : 1.4;
  context.strokeStyle = "rgba(255,255,255,.94)";
  context.lineWidth = width + 2.4;
  context.stroke();
  context.strokeStyle = color;
  context.lineWidth = width;
  context.stroke();
  context.setLineDash([]);
}

function glyph(context, point, shape, color, strong) {
  const [x, y] = point;
  const radius = strong ? 4.5 : 3.5;
  stroke(context, () => {
    if (shape === "square") {
      context.rect(x - radius, y - radius, radius * 2, radius * 2);
    } else if (shape === "diamond") {
      context.moveTo(x, y - radius);
      context.lineTo(x + radius, y);
      context.lineTo(x, y + radius);
      context.lineTo(x - radius, y);
      context.closePath();
    } else {
      context.moveTo(x - radius, y);
      context.lineTo(x + radius, y);
      context.moveTo(x, y - radius);
      context.lineTo(x, y + radius);
    }
  }, color, strong, false, shape === "square" || shape === "diamond");
}

function direction(a, b) {
  const x = b[0] - a[0];
  const y = b[1] - a[1];
  const length = Math.hypot(x, y);
  return length >= .1 ? [x / length, y / length] : [1, 0];
}

function tick(context, point, tangent, color, strong, radius = strong ? 4.5 : 3.5) {
  const [x, y] = point;
  const dx = -tangent[1] * radius;
  const dy = tangent[0] * radius;
  stroke(context, () => {
    context.moveTo(x - dx, y - dy);
    context.lineTo(x + dx, y + dy);
  }, color, strong);
}

function badge(context, text, point, color, width, height) {
  if (!text || width < 24 || height < 24) return;
  context.font = "600 12px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  const boxWidth = Math.min(width - 8, Math.max(17, Math.ceil(context.measureText(text).width) + 12));
  const x = Math.max(4, Math.min(width - boxWidth - 4, point[0] - boxWidth / 2));
  const y = Math.max(4, Math.min(height - 24, point[1] - 10));
  context.fillStyle = "#fff";
  context.fillRect(x, y, boxWidth, 20);
  context.strokeStyle = color;
  context.lineWidth = 1;
  context.strokeRect(x + .5, y + .5, boxWidth - 1, 19);
  context.fillStyle = "#171717";
  context.fillText(text, x + boxWidth / 2, y + 10, boxWidth - 8);
}

function traceSegments(context, segments) {
  let previous;
  for (const [a, b] of segments) {
    if (!previous || Math.hypot(a[0] - previous[0], a[1] - previous[1]) > .05) context.moveTo(...a);
    context.lineTo(...b);
    previous = b;
  }
}

function path(context, segments, color, strong, dashed) {
  stroke(context, () => traceSegments(context, segments), color, strong, dashed);
}

function seamPath(context, segments, color, strong, dashed) {
  const width = strong ? 4.4 : 3.4;
  context.beginPath();
  traceSegments(context, segments);
  context.setLineDash(dashed ? [8, 6] : []);
  for (const [paint, lineWidth] of [["rgba(13,20,29,.76)", width + 4.4], ["#fff", width + 2.6], [color, width]]) {
    context.strokeStyle = paint;
    context.lineWidth = lineWidth;
    context.stroke();
  }
  context.setLineDash([]);
}

function seamCaption(segments) {
  const lengths = segments.map(([a, b]) => Math.hypot(b[0] - a[0], b[1] - a[1]));
  let remaining = lengths.reduce((sum, length) => sum + length, 0) / 2;
  if (remaining < .05) return null;
  for (const [index, [a, b]] of segments.entries()) {
    if (remaining > lengths[index] || !lengths[index]) {
      remaining -= lengths[index];
      continue;
    }
    const [x, y] = interpolate(a, b, remaining / lengths[index]);
    const [tx, ty] = direction(a, b);
    const side = tx < 0 ? 1 : -1;
    return [x - ty * side * 22, y + tx * side * 15];
  }
  return null;
}

function seamEndpoints(context, projection, view, color, strong) {
  const start = view[0];
  const last = view.at(-1);
  if (view.length > 1 && start && last && start.every((coordinate, axis) => Math.abs(coordinate - last[axis]) < 1e-7)) return;
  const a = projection.point(start);
  if (a) glyph(context, a, "square", color, strong);
  if (view.length < 2) return;
  const b = projection.point(last);
  if (b && (!a || Math.hypot(b[0] - a[0], b[1] - a[1]) > 4)) glyph(context, b, "square", color, strong);
}

function edgeAnnotation(context, segments, color, strong) {
  const end = segments.at(-1)[1];
  let earlier = end;
  for (let index = segments.length - 1; index >= 0; index--) {
    const [a, b] = segments[index];
    if (Math.hypot(b[0] - earlier[0], b[1] - earlier[1]) > .05) break;
    earlier = a;
    if (Math.hypot(end[0] - earlier[0], end[1] - earlier[1]) >= 9) break;
  }
  const length = Math.hypot(end[0] - earlier[0], end[1] - earlier[1]);
  if (length >= 4) {
    const [x, y] = direction(earlier, end);
    const size = Math.min(1, length / 9);
    const tip = [end[0] - 2 * x * size, end[1] - 2 * y * size];
    const base = [tip[0] - 7 * x * size, tip[1] - 7 * y * size];
    stroke(context, () => {
      context.moveTo(base[0] - 3 * y * size, base[1] + 3 * x * size);
      context.lineTo(...tip);
      context.lineTo(base[0] + 3 * y * size, base[1] - 3 * x * size);
    }, color, strong);
  }
  return [end[0] + 12, end[1] - 12];
}

export function drawCadOverlay(canvas, camera, graphics) {
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const ratio = Math.min(canvas.ownerDocument.defaultView?.devicePixelRatio || 1, 2);
  const pixelWidth = Math.max(1, Math.round(width * ratio));
  const pixelHeight = Math.max(1, Math.round(height * ratio));
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  const context = canvas.getContext("2d");
  if (!context) return;
  context.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0);
  context.clearRect(0, 0, width, height);
  if (!camera || !graphics?.length) return;
  camera.updateWorldMatrix(true, false);
  const projection = canvasProjection(camera, width, height);
  context.lineCap = "butt";
  context.lineJoin = "round";

  for (const graphic of graphics) {
    const { kind, points, color = kind === "notch" ? "#893f16" : ink, strong, dashed, label } = graphic;
    if (!points?.length) continue;
    const view = points.map(projection.view);
    const first = projection.point(view[0]);
    if (kind === "point" || kind === "tick" || kind === "notch") {
      if (!first) continue;
      if (kind === "point") glyph(context, first, graphic.glyph, color, strong);
      else {
        const segment = projection.segment(view[0], view[1]);
        const tangent = segment ? direction(...segment) : [1, 0];
        if (kind === "notch") tick(context, first, tangent, color, true, 8);
        else tick(context, first, tangent, color, strong);
      }
      if (label != null) badge(context, String(label), [first[0] + 12, first[1] - 12], color, width, height);
    } else if (kind === "line" || kind === "guide") {
      const segment = kind === "guide" ? projection.guide(view[0], view[1]) : projection.segment(view[0], view[1]);
      if (!segment) continue;
      path(context, [segment], color, strong, kind === "guide" || dashed);
      if (graphic.endpoints) {
        if (first) glyph(context, first, "square", color, strong);
        const second = projection.point(view[1]);
        if (second) glyph(context, second, "square", color, strong);
      }
    } else if (kind === "dimension") {
      const segment = projection.segment(view[0], view[1]);
      if (!segment) continue;
      const tangent = direction(...segment);
      path(context, [segment], color, strong, dashed);
      if (first) tick(context, first, tangent, color, strong);
      const second = projection.point(view[1]);
      if (second) tick(context, second, tangent, color, strong);
      const middle = interpolate(segment[0], segment[1], .5);
      const length = Math.hypot(...points[0].map((coordinate, axis) => points[1][axis] - coordinate));
      badge(context, label == null ? `${millimeters.format(length)} мм` : String(label),
        [middle[0], middle[1] - 13], color, width, height);
    } else if (kind === "edge" || kind === "seam") {
      const segments = [];
      for (let index = 1; index < view.length; index++) {
        const segment = projection.segment(view[index - 1], view[index]);
        if (segment) segments.push(segment);
      }
      if (kind === "seam") {
        if (segments.length) seamPath(context, segments, color, strong, dashed);
        if (graphic.endpoints) seamEndpoints(context, projection, view, color, strong);
        if (graphic.directional && segments.length) edgeAnnotation(context, segments, color, strong);
        if (label != null) {
          const position = seamCaption(segments);
          if (position) badge(context, String(label), position, color, width, height);
        }
      } else if (segments.length) {
        path(context, segments, color, strong, dashed);
        const position = edgeAnnotation(context, segments, color, strong);
        if (label != null) badge(context, String(label), position, color, width, height);
      }
    }
  }
}
