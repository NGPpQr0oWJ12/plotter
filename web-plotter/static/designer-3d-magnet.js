import { Vector3 } from "./three-3d.js";

const edgeSlots = [[0, 1], [1, 2], [2, 0]];
const radius = 18;

export function worldEdgeKey(a, b) {
  return [a, b].map(point => point.map(value => Math.round(value * 100)).join(",")).sort().join("|");
}

export function magneticEdges(geometry, authored, plane) {
  const points = geometry.vertices.map(point => new Vector3(...point));
  const planar = plane && points.map(point => Math.abs(plane.normal.dot(point) - plane.offset) <= 0.35);
  const records = new Map();
  for (const [triangle, corners] of geometry.triangles.entries()) {
    if (planar && !corners.every(vertex => planar[vertex])) continue;
    for (const [a, b] of edgeSlots) {
      const first = corners[a], last = corners[b];
      const id = first < last ? first + ":" + last : last + ":" + first;
      const record = records.get(id);
      if (record) record.count++;
      else records.set(id, { triangle, first, last, a, b, count: 1 });
    }
  }
  const edges = [...records.values()].filter(({ count, first, last }) =>
    count === 1 || authored.has(worldEdgeKey(geometry.vertices[first], geometry.vertices[last])));

  let view = "";
  let visible = [];
  const viewPoint = new Vector3(), projected = new Vector3();
  function screen(point, camera, rect) {
    viewPoint.copy(point).applyMatrix4(camera.matrixWorldInverse);
    const depth = -viewPoint.z;
    if (depth < camera.near || depth > camera.far) return null;
    projected.copy(viewPoint).applyMatrix4(camera.projectionMatrix);
    return { x: (1 + projected.x) * rect.width / 2, y: (1 - projected.y) * rect.height / 2, depth };
  }

  function pick(event, camera, rect, hit, previous, boundaryOnly = false) {
    const signature = [rect.width, rect.height, ...camera.matrixWorldInverse.elements, ...camera.projectionMatrix.elements].join(",");
    if (signature !== view) {
      view = signature;
      const projections = new Map();
      const at = vertex => {
        if (!projections.has(vertex)) projections.set(vertex, screen(points[vertex], camera, rect));
        return projections.get(vertex);
      };
      visible = edges.flatMap(edge => {
        const a = at(edge.first), b = at(edge.last);
        return a && b ? [{ edge, a, b }] : [];
      });
    }
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    let best;
    for (const { edge, a, b } of visible) {
      if (boundaryOnly && edge.count !== 1) continue;
      if (x < Math.min(a.x, b.x) - radius || x > Math.max(a.x, b.x) + radius ||
          y < Math.min(a.y, b.y) - radius || y > Math.max(a.y, b.y) + radius) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const length = dx * dx + dy * dy;
      if (length < 1e-5) continue;
      let t = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / length));
      const distance = Math.hypot(x - a.x - t * dx, y - a.y - t * dy);
      if (distance > radius) continue;
      const rank = distance - (edge === previous ? 1.5 : 0);
      if (best && rank > best.rank) continue;
      if (t * Math.sqrt(length) < 3.5) t = 0;
      else if ((1 - t) * Math.sqrt(length) < 3.5) t = 1;
      const fraction = (t / b.depth) / ((1 - t) / a.depth + t / b.depth);
      const point = points[edge.first].clone().lerp(points[edge.last], fraction);
      const depth = point.distanceTo(camera.position);
      const tolerance = depth * Math.tan(camera.fov * Math.PI / 360) * 40 / rect.height;
      if (hit && depth > hit.distance + tolerance || best && rank === best.rank && depth >= best.depth) continue;
      const barycentric = [0, 0, 0];
      barycentric[edge.a] = 1 - fraction;
      barycentric[edge.b] = fraction;
      best = { edge, point, anchor: { triangle: edge.triangle, barycentric }, rank, depth };
    }
    return best;
  }

  return { pick };
}
