import { Vector3 } from "./three-3d.js";
import { canvasProjection } from "./designer-3d-overlay.js";

export const draftingTools = new Set(["measure", "line", "guide"]);

const epsilon = 0.001;
const planeTolerance = 0.01;
const colors = { line: "#135988", guide: "#85602a", measure: "#14715d" };
const axes = [new Vector3(1, 0, 0), new Vector3(0, 1, 0), new Vector3(0, 0, 1)];
const axisNames = ["X", "Y", "Z"];
const millimeters = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });
const edgeKey = (a, b) => a < b ? `${a}:${b}` : `${b}:${a}`;

function segment(from, to, nodeIds, label, infinite = false) {
  const along = to.clone().sub(from);
  const length = along.length();
  return length > epsilon ? { from, to, nodeIds, label, infinite, length, along: along.divideScalar(length) } : null;
}

function markSegment(mark) {
  const guide = mark.kind === "guide";
  return segment(new Vector3(...mark.from), new Vector3(...mark.to), mark.nodeIds, guide ? "Направляющая" : "Отрезок", guide);
}

function faceBoundary(item, faceId, cache) {
  if (!cache.has(item)) {
    const hidden = new Set();
    for (const [index, [a, b]] of (item.node.edges || []).entries()) {
      const flag = item.node.edgeFlags?.[index];
      if (flag?.visible === false || flag?.soft) hidden.add(edgeKey(a, b));
    }
    cache.set(item, { hidden, faces: new Map() });
  }
  const { hidden, faces } = cache.get(item);
  if (faces.has(faceId)) return faces.get(faceId);
  const { node } = item;
  const loops = node.faceLoops?.[faceId];
  const sides = new Map();
  for (const loop of loops || (item.faceTriangles.get(faceId) || []).map(index => node.triangles[item.indices[index]])) {
    for (const [index, a] of loop.entries()) {
      const b = loop[(index + 1) % loop.length];
      const key = edgeKey(a, b);
      sides.set(key, sides.has(key) ? null : [a, b]);
    }
  }
  const edges = [];
  for (const [key, side] of sides) {
    if (!side || hidden.has(key)) continue;
    const [a, b] = side.map(index => new Vector3(...node.vertices[index]));
    const edge = segment(a, b, [node.id], "Ребро");
    if (edge) edges.push(edge);
  }
  faces.set(faceId, edges);
  return edges;
}

function drawnEdges(item, cache) {
  if (!item.node.editorSourceFace) return [];
  const entry = cache.get(item);
  if (entry.drawn) return entry.drawn;
  const { node } = item;
  entry.drawn = [];
  for (const [a, b] of node.edges || []) {
    if (entry.hidden.has(edgeKey(a, b))) continue;
    const edge = segment(new Vector3(...node.vertices[a]), new Vector3(...node.vertices[b]), [node.id], "Ребро");
    if (edge) entry.drawn.push(edge);
  }
  return entry.drawn;
}

function planePoint(ray, origin, normal) {
  const denominator = normal.dot(ray.direction);
  if (Math.abs(denominator) <= 1e-6) return null;
  const distance = origin.clone().sub(ray.origin).dot(normal) / denominator;
  return distance >= 0 ? ray.at(distance, new Vector3()) : null;
}

function cameraPlane(camera, along = null) {
  const facing = camera.getWorldDirection(new Vector3());
  if (along) {
    const normal = facing.clone().addScaledVector(along, -facing.dot(along));
    if (normal.lengthSq() > 0.01) return normal.normalize();
  }
  return axes.reduce((best, axis) => Math.abs(facing.dot(axis)) > Math.abs(facing.dot(best)) ? axis : best);
}

function nearestRayPoint(ray, edge) {
  const parallel = ray.direction.dot(edge.along);
  const denominator = 1 - parallel * parallel;
  if (denominator <= 1e-8) return null;
  const delta = ray.origin.clone().sub(edge.from);
  let amount = (delta.dot(edge.along) - parallel * delta.dot(ray.direction)) / denominator;
  if (!edge.infinite) amount = Math.max(0, Math.min(edge.length, amount));
  return edge.from.clone().addScaledVector(edge.along, amount);
}

function crossing(a, b) {
  const normal = a.along.clone().cross(b.along);
  const denominator = normal.lengthSq();
  if (denominator <= 1e-8) return null;
  const delta = b.from.clone().sub(a.from);
  const t = delta.clone().cross(b.along).dot(normal) / denominator;
  const u = delta.cross(a.along).dot(normal) / denominator;
  if ((!a.infinite && (t < -epsilon || t > a.length + epsilon))
      || (!b.infinite && (u < -epsilon || u > b.length + epsilon))) return null;
  const point = a.from.clone().addScaledVector(a.along, t);
  return point.distanceTo(b.from.clone().addScaledVector(b.along, u)) <= planeTolerance ? point : null;
}

function pointOnGuide(point, guide) {
  return guide.from.clone().addScaledVector(guide.along, point.clone().sub(guide.from).dot(guide.along));
}

function pointOnBoundary(point, boundary) {
  return boundary.some(edge => {
    const along = point.clone().sub(edge.from).dot(edge.along);
    return along >= -planeTolerance && along <= edge.length + planeTolerance &&
      pointOnGuide(point, edge).distanceToSquared(point) <= planeTolerance * planeTolerance;
  });
}

function directions(point, normal = null) {
  const options = [];
  function add(axis, label) {
    const along = axis.clone();
    if (normal) along.addScaledVector(normal, -along.dot(normal));
    if (along.lengthSq() <= 1e-6) return;
    along.normalize();
    if (options.some(option => Math.abs(option.along.dot(along)) > 1 - 1e-6)) return;
    options.push({ from: point.point, to: point.point.clone().add(along), along, infinite: true, label });
  }
  for (const edge of point.edges || []) {
    add(edge.along, edge.label === "Направляющая" ? "Вдоль направляющей" : "Вдоль ребра");
    const perpendicular = (normal || point.normal)?.clone().cross(edge.along);
    if (perpendicular) add(perpendicular, "Перпендикуляр");
  }
  for (const [index, axis] of axes.entries()) {
    add(axis, normal && Math.abs(normal.dot(axis)) > 1e-6 ? "По плоскости" : `Ось ${axisNames[index]}`);
  }
  return options;
}

function gapToSegment(point, [a, b]) {
  const x = b[0] - a[0];
  const y = b[1] - a[1];
  const length = x * x + y * y;
  const fraction = length ? Math.max(0, Math.min(1, ((point[0] - a[0]) * x + (point[1] - a[1]) * y) / length)) : 0;
  return Math.hypot(point[0] - a[0] - fraction * x, point[1] - a[1] - fraction * y);
}

function sourceEdges(point, edges, preferred) {
  if (!preferred) return [];
  if (!preferred.infinite && (point.distanceTo(preferred.from) <= epsilon || point.distanceTo(preferred.to) <= epsilon)) {
    return edges.filter(edge => !edge.infinite && (edge.from.distanceTo(point) <= epsilon || edge.to.distanceTo(point) <= epsilon));
  }
  return [preferred];
}

export function createDraftingTools({ getCamera, getItems, getMarks, isNodeVisible, getScope, getTarget }) {
  const boundaryCache = new WeakMap();
  let mode;
  let start = null;
  let hover = null;
  let tracking = null;
  let locked = null;
  let automatic = null;
  let measurements = [];
  const future = [];

  const shown = mark => mark.nodeIds.every(isNodeVisible) && (!getScope() || mark.nodeIds.length > 0);
  const visibleMarks = () => getMarks().filter(shown);
  const pointAsArray = item => item.point.toArray();

  function hint(tool) {
    if (tool === "line") return start
      ? "Укажите вторую точку на той же грани. Shift удерживает направление, Alt отключает привязку."
      : "Отрезок. Укажите начальную точку на плоской грани модели.";
    if (start) return "Укажите вторую точку. Shift удерживает направление, Alt отключает привязку.";
    if (tool === "measure") return "Линейка. Укажите две точки, чтобы измерить расстояние в миллиметрах.";
    return "Направляющая. Укажите две точки, чтобы задать направление.";
  }

  function lineSurfaceIssue(surface, allowBoundary = false) {
    if (!surface?.nodeId || surface.faceId == null || !surface.point || !surface.normal?.lengthSq()) {
      return start ? "Укажите вторую точку на той же грани." : "Укажите точку на плоской грани модели.";
    }
    if (!start) return "";
    const matches = start.faceId === surface.faceId
      ? Math.abs(start.normal.dot(surface.normal.clone().normalize())) >= 1 - 1e-6 &&
        Math.abs(surface.point.clone().sub(start.point).dot(start.normal)) <= planeTolerance
      : allowBoundary;
    if (start.nodeIds[0] !== surface.nodeId || !matches) {
      return "Обе точки отрезка должны лежать на одной плоской грани модели.";
    }
    return "";
  }

  function startingBoundary() {
    if (start.boundary) return start.boundary;
    const [nodeId] = start.nodeIds;
    const edges = [];
    for (const item of getItems().values()) {
      if (item.node.id === nodeId) edges.push(...faceBoundary(item, start.faceId, boundaryCache));
      else if (item.node.editorSourceNode === nodeId && item.node.editorSourceFace === start.faceId) {
        for (const faceId of item.faceTriangles.keys()) edges.push(...faceBoundary(item, faceId, boundaryCache));
      }
    }
    start.boundary = edges;
    return edges;
  }

  function prepare(event, { hit, ray, rect, nodeId, surface }, tool) {
    const camera = getCamera();
    if (!camera || !ray || !rect.width || !rect.height) return null;
    const modelEdge = tool === "line";
    const noSnap = event.altKey;
    if (!event.shiftKey || noSnap) locked = null;
    if (modelEdge && lineSurfaceIssue(surface, !noSnap)) return null;
    const sharedBoundary = modelEdge && start && start.faceId !== surface.faceId;
    camera.updateWorldMatrix(true, false);
    const projection = canvasProjection(camera, rect.width, rect.height);
    const mouse = [event.clientX - rect.left, event.clientY - rect.top];
    const screen = point => projection.point(projection.view(point.toArray()));
    const gap = point => {
      const pixel = screen(point);
      return pixel ? Math.hypot(mouse[0] - pixel[0], mouse[1] - pixel[1]) : Infinity;
    };
    const pickedId = hit?.object.userData.nodeId;
    const modelId = modelEdge ? surface.nodeId : pickedId;
    const owner = modelEdge ? modelId : nodeId || modelId || getScope();
    const nodeIds = owner ? [owner] : [];
    const faceNormal = modelEdge ? surface.normal.clone().normalize() : hit?.face?.normal.clone().transformDirection(hit.object.matrixWorld);
    const fixed = start && tool !== "measure";
    const origin = start?.point || getTarget?.() || new Vector3();
    const normal = fixed ? start.normal : faceNormal || start?.normal || cameraPlane(camera);
    const raw = modelEdge ? surface.point.clone() : hit && !fixed ? hit.point : planePoint(ray, origin, normal);
    if (modelEdge && fixed && !sharedBoundary) raw.addScaledVector(normal, origin.clone().sub(raw).dot(normal));
    if (!raw || !screen(raw)) return null;
    const face = modelEdge ? { faceId: start?.faceId ?? surface.faceId, nodeIds } : {};
    if (noSnap) {
      automatic = tracking = null;
      return { point: raw, nodeIds: fixed ? start.nodeIds : nodeIds, normal, edges: [], ...face };
    }

    const tolerance = event.pointerType === "touch" ? 16 : 10;
    const planeOrigin = fixed ? origin : raw;
    const onPlane = point => !(fixed || modelEdge) || Math.abs(point.clone().sub(planeOrigin).dot(normal)) <= planeTolerance;
    const item = getItems().get(pickedId ?? modelId);
    const hitFace = hit && item?.faceIds[hit.faceIndex];
    const boundaryId = modelEdge ? surface.snapFaceId ?? hitFace ?? surface.faceId : hitFace;
    const edges = item ? faceBoundary(item, boundaryId, boundaryCache) : [];
    const candidates = modelEdge && item && !sharedBoundary ? [...edges, ...drawnEdges(item, boundaryCache)] : edges;
    const boundary = modelEdge && !sharedBoundary ? candidates.filter(edge => onPlane(edge.from) && onPlane(edge.to)) : candidates;
    const nearEdges = [];
    let contact = null;
    function consider(point, ids, label, priority, edge) {
      if (!point || !onPlane(point)) return;
      const distance = gap(point);
      if (distance > tolerance || contact && (priority < contact.priority || priority === contact.priority && distance >= contact.distance)) return;
      if (sharedBoundary && !pointOnBoundary(point, startingBoundary())) return;
      contact = { point, nodeIds: ids.slice(0, 2), label, distance, priority, edge };
    }
    function nearby(edge) {
      if (!edge) return;
      const point = nearestRayPoint(ray, edge);
      if (point && gap(point) <= tolerance) nearEdges.push(edge);
      if (!edge.infinite) {
        consider(edge.from, edge.nodeIds, "Конец", 4, edge);
        consider(edge.to, edge.nodeIds, "Конец", 4, edge);
        consider(edge.from.clone().lerp(edge.to, .5), edge.nodeIds, "Середина", 3, edge);
      }
      consider(point, edge.nodeIds, edge.label, 1, edge);
    }
    for (const edge of boundary) nearby(edge);
    if (sharedBoundary) return contact ? { ...contact, ...face, normal, edges: [] } : null;
    if (!modelEdge) for (const mark of visibleMarks()) nearby(markSegment(mark));

    // Only pairs close to the pointer can contribute a crossing.
    for (const [index, a] of nearEdges.slice(0, 24).entries()) {
      for (const b of nearEdges.slice(index + 1, 24)) {
        if (!a.infinite && !b.infinite) continue;
        consider(crossing(a, b), [...new Set([...a.nodeIds, ...b.nodeIds])], "Пересечение", 3, a);
      }
    }

    const options = start ? directions(start, fixed ? normal : null) : tracking ? directions(tracking, tracking.normal) : [];
    let inference = null;
    let inferenceGap = tolerance;
    for (const option of options) {
      const point = nearestRayPoint(ray, option);
      if (!point || !onPlane(point) || gap(option.from) <= tolerance) continue;
      const distance = gap(point);
      if (distance < inferenceGap) {
        inferenceGap = distance;
        inference = option;
      }
    }
    if (event.shiftKey && start && !locked && gap(start.point) > 2) {
      const delta = (contact?.point || raw).clone().sub(start.point).normalize();
      const dominant = options.reduce((best, option) => !best || Math.abs(delta.dot(option.along)) > Math.abs(delta.dot(best.along)) ? option : best, null);
      locked = automatic || inference || dominant;
    }
    automatic = inference;
    const guide = locked || inference;
    if (guide) {
      for (const edge of nearEdges) consider(crossing(guide, edge), edge.nodeIds, "Пересечение", 3, edge);
      if (locked || !contact || contact.priority < 2) {
        const point = locked || fixed ? pointOnGuide(locked && contact ? contact.point : raw, guide) : nearestRayPoint(ray, guide);
        if (point && screen(point)) {
          const exact = contact && point.distanceTo(contact.point) <= epsilon;
          const label = exact ? contact.label : contact && locked ? `${contact.label} · проекция` : guide.label;
          return { point, nodeIds: exact ? contact.nodeIds : start?.nodeIds || nodeIds, label, guide, normal, edges: [], ...face };
        }
      }
    }

    if (contact) {
      const edges = sourceEdges(contact.point, [...boundary, ...nearEdges], contact.edge);
      const support = fixed || modelEdge ? normal : faceNormal && Math.abs(contact.point.clone().sub(hit.point).dot(faceNormal)) <= planeTolerance
        ? faceNormal : cameraPlane(camera, contact.edge?.along);
      const point = { ...contact, edges, normal: support, ...face };
      if (!start && contact.priority > 1) tracking = point;
      return point;
    }
    return { point: raw, nodeIds: fixed ? start.nodeIds : nodeIds, normal, edges: [], ...face };
  }

  function clearHover() {
    const changed = Boolean(hover);
    hover = null;
    return changed;
  }

  function clearStart() {
    const changed = Boolean(start || hover);
    start = hover = tracking = locked = automatic = null;
    return changed;
  }

  return {
    hint,
    hasStart: () => Boolean(start),
    canUndo: (tool = mode) => Boolean(start || tool === "measure" && measurements.length),
    canRedo: (tool = mode) => Boolean(future.at(-1)?.start || tool === "measure" && future.length),
    clearHover,
    cancel() {
      future.length = 0;
      return clearStart();
    },
    reset() {
      clearStart();
      measurements = [];
      future.length = 0;
    },
    hover(tool, event, context) {
      mode = tool;
      hover = prepare(event, context, tool);
      return Boolean(hover);
    },
    click(tool, event, context) {
      mode = tool;
      const next = prepare(event, context, tool);
      if (!next) return { message: (tool === "line" && lineSurfaceIssue(context.surface)) ||
        "Не удаётся указать точку в этом ракурсе.", error: true };
      if (!start) {
        start = next;
        hover = tracking = locked = automatic = null;
        future.length = 0;
        return { message: hint(tool) };
      }
      const length = start.point.distanceTo(next.point);
      if (length <= epsilon) return { message: "Укажите две разные точки.", error: true };
      const from = pointAsArray(start);
      const to = pointAsArray(next);
      const nodeIds = [...new Set([...start.nodeIds, ...next.nodeIds])].slice(0, 2);
      if (tool === "guide" && getMarks().length >= 1000) return { message: "В модели уже 1000 отрезков и направляющих.", error: true };
      if (tool !== "measure" && [...from, ...to].some(value => Math.abs(value) > 1e9)) {
        return { message: "Точка слишком далеко от начала координат модели.", error: true };
      }
      const line = tool === "line" ? { edge: { id: crypto.randomUUID(), nodeId: start.nodeIds[0],
        faceId: start.faceId, points: [from, to] }, normal: start.normal } : null;
      clearStart();
      future.length = 0;
      if (line) return { ...line, message: hint(tool) };
      if (tool === "measure") {
        measurements.push({ from, to, nodeIds });
        return { message: `Расстояние ${millimeters.format(length)} мм.` };
      }
      return { mark: { id: crypto.randomUUID(), kind: tool, from, to, nodeIds }, message: hint(tool) };
    },
    continueLine(edge, normal) {
      const [from, to] = edge.points.map(point => new Vector3(...point));
      const nodeIds = [edge.nodeId];
      clearStart();
      start = { point: to, faceId: edge.faceId, normal, nodeIds, edges: [segment(from, to, nodeIds, "Ребро")] };
      return hint("line");
    },
    undoPoint(tool = mode) {
      if (start) {
        future.push({ start });
        return clearStart();
      }
      if (tool !== "measure" || !measurements.length) return false;
      future.push({ measurement: measurements.pop() });
      return true;
    },
    redoPoint(tool = mode) {
      const item = future.at(-1);
      if (!item || item.measurement && tool !== "measure") return false;
      future.pop();
      if (item.start) start = item.start;
      else measurements.push(item.measurement);
      return true;
    },
    snapOnPlane(event, { ray, rect }, plane) {
      const camera = getCamera();
      if (!camera || event.altKey || !plane || !rect.width || !rect.height) return null;
      camera.updateWorldMatrix(true, false);
      const project = canvasProjection(camera, rect.width, rect.height);
      let match = null;
      let best = event.pointerType === "touch" ? 16 : 10;
      for (const mark of visibleMarks()) {
        const edge = markSegment(mark);
        if (!edge) continue;
        const startGap = plane.normal.dot(edge.from) - plane.offset;
        const normalAlong = plane.normal.dot(edge.along);
        let point;
        if (Math.abs(normalAlong) <= 1e-6) {
          if (Math.abs(startGap) > planeTolerance) continue;
          point = nearestRayPoint(ray, edge);
        } else {
          const distance = -startGap / normalAlong;
          if (!edge.infinite && (distance < 0 || distance > edge.length)) continue;
          point = edge.from.clone().addScaledVector(edge.along, distance);
        }
        const pixel = point && project.point(project.view(point.toArray()));
        if (!pixel) continue;
        const gap = Math.hypot(event.clientX - rect.left - pixel[0], event.clientY - rect.top - pixel[1]);
        if (gap >= best) continue;
        best = gap;
        match = { point, label: edge.label };
      }
      return match;
    },
    hitMark(event, rect) {
      const camera = getCamera();
      if (!camera || !rect.width || !rect.height) return null;
      camera.updateWorldMatrix(true, false);
      const project = canvasProjection(camera, rect.width, rect.height);
      const pointer = [event.clientX - rect.left, event.clientY - rect.top];
      let nearest = null;
      let best = event.pointerType === "touch" ? 14 : 7;
      for (const mark of visibleMarks()) {
        const a = project.view(mark.from), b = project.view(mark.to);
        const line = mark.kind === "guide" ? project.guide(a, b) : project.segment(a, b);
        if (!line) continue;
        const distance = gapToSegment(pointer, line);
        if (distance >= best) continue;
        best = distance;
        nearest = mark.id;
      }
      return nearest;
    },
    graphics(tool, selectedMarkId) {
      const result = visibleMarks().map(mark => ({
        kind: mark.kind, points: [mark.from, mark.to], color: colors[mark.kind],
        strong: mark.id === selectedMarkId, endpoints: mark.id === selectedMarkId,
      }));
      for (const mark of measurements.filter(shown)) result.push({ kind: "dimension", points: [mark.from, mark.to], color: colors.measure });
      if (!draftingTools.has(tool)) return result;
      const color = colors[tool];
      if (start) result.push({ kind: "point", points: [pointAsArray(start)], glyph: "square", color });
      if (hover?.guide && tool !== "guide") {
        result.push({ kind: "guide", points: [hover.guide.from.toArray(), hover.guide.to.toArray()], color: colors.guide });
      }
      if (start && hover && start.point.distanceTo(hover.point) > epsilon) {
        result.push({ kind: tool === "measure" ? "dimension" : tool, points: [pointAsArray(start), pointAsArray(hover)],
          color, strong: true, dashed: true });
      }
      if (hover) result.push({ kind: "point", points: [pointAsArray(hover)], glyph: hover.label ? "diamond" : "cross",
        label: hover.label, color, strong: Boolean(hover.label) });
      return result;
    },
  };
}
