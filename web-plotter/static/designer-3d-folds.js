import { Vector3 } from "./three-3d.js";

const creaseAngle = 22;
const transitionAngle = 2.5;
const coplanarAngle = 0.45;
const degrees = 180 / Math.PI;
const reliefCache = new WeakMap();

function addIncidence(edges, index, a, b) {
  const first = Math.min(a, b);
  const second = Math.max(a, b);
  const key = `${first}:${second}`;
  let edge = edges.get(key);
  if (!edge) {
    edge = { first, second, sides: [], count: 0 };
    edges.set(key, edge);
  }
  edge.count += 1;
  if (edge.count <= 2) edge.sides.push({ index, forward: a === first });
}

function neighboringFold(edge, candidates, vertex, points) {
  const outward = candidate => points[candidate.first === vertex ? candidate.second : candidate.first].clone().sub(points[vertex]).normalize();
  const direction = outward(edge);
  return candidates.some(other => other !== edge && other.angle >= edge.angle * 0.5
    && outward(other).dot(direction) < -0.8);
}

export function clothFoldSegments(placement) {
  if (reliefCache.has(placement)) return reliefCache.get(placement);
  const { vertices, triangles } = placement;
  const points = vertices.map(vertex => new Vector3(...vertex));
  const edges = new Map();
  const faces = [];
  for (const [index, [a, b, c]] of triangles.entries()) {
    const normal = points[b].clone().sub(points[a]).cross(points[c].clone().sub(points[a]));
    faces.push({ normal: normal.lengthSq() > 1e-12 ? normal.normalize() : null, planarNeighbor: false, angles: [] });
    addIncidence(edges, index, a, b);
    addIncidence(edges, index, b, c);
    addIncidence(edges, index, c, a);
  }

  const candidates = [];
  for (const edge of edges.values()) {
    if (edge.count !== 2 || points[edge.first].distanceToSquared(points[edge.second]) <= 0.01 ** 2) continue;
    const [a, b] = edge.sides;
    const first = faces[a.index];
    const second = faces[b.index];
    if (!first.normal || !second.normal || a.index === b.index) continue;
    // Adjacent triangles with inconsistent winding need the same geometric angle.
    const dot = first.normal.dot(second.normal) * (a.forward === b.forward ? -1 : 1);
    const angle = Math.acos(Math.max(-1, Math.min(1, dot))) * degrees;
    if (angle <= coplanarAngle) first.planarNeighbor = second.planarNeighbor = true;
    if (angle < transitionAngle) continue;
    edge.angle = angle;
    first.angles.push(edge);
    second.angles.push(edge);
    candidates.push(edge);
  }

  // Keep shoulders between modeled strips. In other meshes require a locally dominant,
  // continuing fold so regular triangulation of smooth cloth doesn't produce a wire grid.
  const dominant = (edge, face) => face.angles.every(other => other === edge || edge.angle >= other.angle * 1.25);
  const incident = new Map();
  for (const edge of candidates) {
    const [first, second] = edge.sides.map(side => faces[side.index]);
    edge.kind = edge.angle >= creaseAngle ? "crease"
      : first.planarNeighbor && second.planarNeighbor ? "transition" : null;
    if (!edge.kind && (edge.angle < 6 || !dominant(edge, first) && !dominant(edge, second))) continue;
    for (const vertex of [edge.first, edge.second]) {
      if (!incident.has(vertex)) incident.set(vertex, []);
      incident.get(vertex).push(edge);
    }
    edge.coherent = true;
  }

  const result = [];
  for (const edge of candidates) {
    const { first, second, angle } = edge;
    const continues = edge.coherent && (neighboringFold(edge, incident.get(first), first, points)
      || neighboringFold(edge, incident.get(second), second, points));
    if (edge.kind || continues) result.push({ kind: edge.kind || "transition", vertices: [first, second],
      points: [vertices[first], vertices[second]], angle });
  }
  result.sort((a, b) => a.vertices[0] - b.vertices[0] || a.vertices[1] - b.vertices[1]);
  reliefCache.set(placement, result);
  return result;
}
