import { Vector3 } from "./three-3d.js";

const topologyCache = new WeakMap();
const edgeCache = new WeakMap();
const zoneCache = new WeakMap();
const sampleBudget = 480_000;
const degrees = Math.PI / 180;
const edgeKey = (a, b) => a < b ? a + ":" + b : b + ":" + a;
const triangleEdges = ([a, b, c]) => [edgeKey(a, b), edgeKey(b, c), edgeKey(c, a)];

function clipDepth(points, plane, direction) {
  if (!points.length) return points;
  const kept = [];
  let previous = points.at(-1);
  for (const point of points) {
    const inside = (point.z - plane) * direction >= 0;
    const wasInside = (previous.z - plane) * direction >= 0;
    if (inside !== wasInside) kept.push(previous.clone().lerp(point, (plane - previous.z) / (point.z - previous.z)));
    if (inside) kept.push(point);
    previous = point;
  }
  return kept;
}

function rasterTriangle(a, b, c, owner, width, height, depth, owners) {
  const ux = b.x - a.x, uy = b.y - a.y;
  const vx = c.x - a.x, vy = c.y - a.y;
  const area = ux * vy - uy * vx;
  if (Math.abs(area) < 1e-12) return;
  const left = Math.max(0, Math.ceil(Math.min(a.x, b.x, c.x) - 0.5));
  const right = Math.min(width - 1, Math.floor(Math.max(a.x, b.x, c.x) - 0.5));
  const top = Math.max(0, Math.ceil(Math.min(a.y, b.y, c.y) - 0.5));
  const bottom = Math.min(height - 1, Math.floor(Math.max(a.y, b.y, c.y) - 0.5));
  if (right < left || bottom < top) return;
  const xStepU = vy / area;
  const xStepV = -uy / area;
  const dzU = b.z - a.z, dzV = c.z - a.z;
  for (let y = top; y <= bottom; y++) {
    const dy = y + 0.5 - a.y;
    const dx = left + 0.5 - a.x;
    let u = (dx * vy - dy * vx) / area;
    let v = (ux * dy - uy * dx) / area;
    for (let x = left, index = y * width + left; x <= right; x++, index++, u += xStepU, v += xStepV) {
      if (u < -1e-9 || v < -1e-9 || u + v > 1 + 1e-9) continue;
      const z = a.z + u * dzU + v * dzV;
      if (z < depth[index]) {
        depth[index] = z;
        owners[index] = owner;
      }
    }
  }
}

export function visibleFacesInBox(items, camera, view, box, { contained = false } = {}) {
  const selected = new Map();
  if (!box.width || !box.height) return selected;
  // Sample frontmost geometry across the box. A bounded buffer keeps a wide drag responsive.
  const step = Math.max(1, Math.sqrt(box.width * box.height / sampleBudget));
  const width = Math.max(1, Math.floor(box.width / step));
  const height = Math.max(1, Math.floor(box.height / step));
  const depth = new Float64Array(width * height).fill(Infinity);
  const owners = new Uint32Array(width * height);
  const faces = [null];
  const outside = new Map();
  camera.updateMatrixWorld();
  const project = point => {
    const p = point.clone().applyMatrix4(camera.projectionMatrix);
    p.x = (view.left + (p.x + 1) * view.width / 2 - box.left) * width / box.width;
    p.y = (view.top + (1 - p.y) * view.height / 2 - box.top) * height / box.height;
    return p;
  };
  for (const { node, mesh, indices } of items) {
    if (!mesh?.visible) continue;
    const outsideFaces = new Set();
    if (contained) outside.set(node.id, outsideFaces);
    const points = node.vertices.map(point => new Vector3(...point).applyMatrix4(camera.matrixWorldInverse));
    const projected = points.map(point => point.z <= -camera.near && point.z >= -camera.far ? project(point) : null);
    for (const index of indices) {
      const triangle = node.triangles[index];
      let screen = triangle.map(vertex => projected[vertex]);
      if (screen.some(point => !point)) {
        const corners = triangle.map(vertex => points[vertex]);
        screen = clipDepth(clipDepth(corners, -camera.near, -1), -camera.far, 1).map(project);
      }
      const face = node.faces[index];
      if (contained && screen.some(p => p.x < -0.01 || p.x > width + 0.01 || p.y < -0.01 || p.y > height + 0.01)) {
        outsideFaces.add(face);
      }
      if (screen.length < 3 || screen.every(p => p.x < 0) || screen.every(p => p.x > width) ||
          screen.every(p => p.y < 0) || screen.every(p => p.y > height)) continue;
      const owner = faces.length;
      faces.push([node.id, face]);
      for (let i = 1; i + 1 < screen.length; i++) {
        rasterTriangle(screen[0], screen[i], screen[i + 1], owner, width, height, depth, owners);
      }
    }
  }
  for (const owner of new Set(owners)) {
    if (!owner) continue;
    const [id, face] = faces[owner];
    if (outside.get(id)?.has(face)) continue;
    if (!selected.has(id)) selected.set(id, new Set());
    selected.get(id).add(face);
  }
  return selected;
}

function meshEdges(item) {
  if (edgeCache.has(item)) return edgeCache.get(item);
  const { node, indices } = item;
  const edges = new Map();
  for (const index of indices) {
    for (const key of triangleEdges(node.triangles[index])) {
      if (!edges.has(key)) edges.set(key, []);
      edges.get(key).push(index);
    }
  }
  edgeCache.set(item, edges);
  return edges;
}

export function connectedFaces(item, start) {
  if (!item.indices.includes(start)) return new Set();
  const edges = meshEdges(item);
  const visitedEdges = new Set();
  const visited = new Set([start]);
  const queue = [start];
  const faces = new Set();
  for (let i = 0; i < queue.length; i++) {
    const triangle = queue[i];
    faces.add(item.node.faces[triangle]);
    for (const key of triangleEdges(item.node.triangles[triangle])) {
      if (visitedEdges.has(key)) continue;
      visitedEdges.add(key);
      for (const neighbor of edges.get(key)) {
        if (visited.has(neighbor)) continue;
        visited.add(neighbor);
        queue.push(neighbor);
      }
    }
  }
  return faces;
}

function topology(item) {
  if (topologyCache.has(item)) return topologyCache.get(item);
  const { node, indices } = item;
  const positions = node.vertices.map(point => new Vector3(...point));
  const edges = meshEdges(item);
  const normals = new Map();
  const neighbors = new Map();
  const boundaries = new Set();
  const authored = new Map(node.edges.map(([a, b], i) => [edgeKey(a, b), node.edgeFlags?.[i]]));
  for (const index of indices) {
    const [a, b, c] = node.triangles[index];
    normals.set(index, positions[b].clone().sub(positions[a]).cross(positions[c].clone().sub(positions[a])).normalize());
    neighbors.set(index, []);
  }
  for (const [key, incident] of edges) {
    const [a, b] = incident;
    const flags = authored.get(key);
    const samePatch = incident.length === 2 && (node.faces[a] === node.faces[b] || flags?.soft || flags?.smooth ||
      (!authored.has(key) && normals.get(a).dot(normals.get(b)) > 1 - 1e-6));
    if (samePatch) {
      neighbors.get(a).push(b);
      neighbors.get(b).push(a);
    } else {
      for (const index of incident) boundaries.add(index);
    }
  }
  const data = { neighbors, normals, boundaries };
  topologyCache.set(item, data);
  return data;
}

export function linkedFaces(item, start) {
  const { neighbors, normals, boundaries } = topology(item);
  if (!neighbors.has(start)) return new Set();
  function flood(sameSide) {
    const picked = new Set([start]);
    const queue = [start];
    for (let i = 0; i < queue.length; i++) {
      for (const neighbor of neighbors.get(queue[i])) {
        if (picked.has(neighbor) || (sameSide && normals.get(start).dot(normals.get(neighbor)) < 0.15)) continue;
        picked.add(neighbor);
        queue.push(neighbor);
      }
    }
    return queue;
  }
  let triangles = flood(false);
  // A wholly smoothed closed shell has no seam to stop at. Take the connected side of the clicked face.
  if (!triangles.some(index => boundaries.has(index))) triangles = flood(true);
  return new Set(triangles.map(index => item.node.faces[index]));
}

function zoneTopology(item) {
  if (zoneCache.has(item)) return zoneCache.get(item);
  const { node, indices } = item;
  const points = node.vertices.map(point => new Vector3(...point));
  const edges = meshEdges(item);
  const authored = new Map((node.edges || []).map(([a, b], i) => [edgeKey(a, b), node.edgeFlags?.[i]]));
  const facets = new Map();
  for (const index of indices) {
    const [a, b, c] = node.triangles[index].map(vertex => points[vertex]);
    const normal = b.clone().sub(a).cross(c.clone().sub(a));
    const twiceArea = normal.length();
    if (twiceArea > 0) facets.set(index, { normal: normal.divideScalar(twiceArea), area: twiceArea / 2 });
  }
  const byFacet = new Map();
  const coplanar = Math.cos(0.45 * degrees);
  for (const [start] of facets) {
    if (byFacet.has(start)) continue;
    const patch = { area: 0, faces: new Set(), neighbors: new Map() };
    const queue = [start];
    const normal = facets.get(start).normal;
    byFacet.set(start, patch);
    for (const index of queue) {
      const face = node.faces[index];
      patch.area += facets.get(index).area;
      patch.faces.add(face);
      for (const key of triangleEdges(node.triangles[index])) {
        const incident = edges.get(key);
        if (incident.length !== 2) continue;
        const next = incident[0] === index ? incident[1] : incident[0];
        const other = facets.get(next);
        if (!other || byFacet.has(next)) continue;
        const flags = authored.get(key);
        if (face !== node.faces[next] && (Math.abs(normal.dot(other.normal)) < coplanar ||
            authored.has(key) && !flags?.smooth && !flags?.soft && flags?.visible !== false)) continue;
        byFacet.set(next, patch);
        queue.push(next);
      }
    }
  }

  for (const [key, incident] of edges) {
    if (incident.length !== 2) continue;
    const [first, second] = incident;
    const from = byFacet.get(first), to = byFacet.get(second);
    if (!from || !to || from === to) continue;
    const flags = authored.get(key);
    const maxTurn = flags?.smooth || flags?.soft ? 50 : 36;
    const alignment = Math.abs(facets.get(first).normal.dot(facets.get(second).normal));
    if (alignment >= coplanar || alignment < Math.cos(maxTurn * degrees)) continue;
    const [a, b] = key.split(":").map(Number);
    const axis = points[b].clone().sub(points[a]);
    const length = axis.length();
    if (!length || length <= (from.neighbors.get(to)?.length || 0)) continue;
    const join = { axis: axis.divideScalar(length), length };
    from.neighbors.set(to, join);
    to.neighbors.set(from, join);
  }
  zoneCache.set(item, byFacet);
  return byFacet;
}

export function surfaceZoneFaces(item, start) {
  const patch = zoneTopology(item).get(start);
  if (!patch) return new Set();
  // Each facet of an extruded rounding meets the next along the same direction.
  // The wide flat shoulders on either end are neighboring patches rather than part of the rounding.
  const parallel = (a, b) => Math.abs(a.dot(b)) >= 0.88;
  const shoulder = (part, other, axis) => part.area > other.area * 2.8 ||
    (part.area > other.area * 1.65 && [...part.neighbors.values()].filter(edge => parallel(edge.axis, axis)).length === 1);
  const connects = (a, b, edge) => !shoulder(a, b, edge.axis) && !shoulder(b, a, edge.axis);
  const links = [...patch.neighbors].filter(([other, edge]) => connects(patch, other, edge));
  const reference = links.sort(([, a], [, b]) => b.length - a.length)[0]?.[1].axis;
  if (!reference) return new Set(patch.faces);
  const faces = new Set();
  const visited = new Set([patch]);
  const queue = [patch];
  for (const current of queue) {
    for (const face of current.faces) faces.add(face);
    for (const [next, edge] of current.neighbors) {
      if (visited.has(next) || !parallel(edge.axis, reference) || !connects(current, next, edge)) continue;
      visited.add(next);
      queue.push(next);
    }
  }
  return faces;
}

export function fabricSurface(parts) {
  const vertices = [];
  const triangles = [];
  const faces = [];
  const vertexIndices = new Map();
  const seenTriangles = new Set();
  const origin = new Vector3();
  const edgeAB = new Vector3();
  const edgeAC = new Vector3();

  for (const [indexInLayer, { node, indices, surfaceIndex = indexInLayer }] of parts.entries()) {
    for (const index of indices) {
      const face = node.faces[index];
      if (node.faceFlags?.[face]?.visible === false) continue;
      const points = node.triangles[index].map(vertex => node.vertices[vertex]);
      // Identical positions at hundredth-millimeter precision share a vertex across components.
      const keys = points.map(point => point.map(coordinate => Math.round(coordinate * 100)).join(","));
      const [a, b, c] = keys;
      if (a === b || b === c || c === a) continue;
      const triangleKey = [...keys].sort().join("|");
      if (seenTriangles.has(triangleKey)) continue;

      const [pointA, pointB, pointC] = points.map((point, corner) => vertices[vertexIndices.get(keys[corner])] ?? point);
      origin.fromArray(pointA);
      edgeAB.fromArray(pointB).sub(origin);
      edgeAC.fromArray(pointC).sub(origin);
      if (edgeAB.cross(edgeAC).lengthSq() === 0) continue;

      triangles.push(keys.map((key, corner) => {
        if (!vertexIndices.has(key)) {
          vertexIndices.set(key, vertices.length);
          vertices.push(points[corner]);
        }
        return vertexIndices.get(key);
      }));
      faces.push(surfaceIndex + ":" + face);
      seenTriangles.add(triangleKey);
    }
  }
  return { vertices, triangles, faces };
}
