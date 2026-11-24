// SketchUp face IDs are local to an occurrence. Keep the full node ID throughout selection.
export function createObjectIndex(nodes) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const childrenById = new Map();
  for (const node of nodes) {
    if (!byId.has(node.parent)) continue;
    if (!childrenById.has(node.parent)) childrenById.set(node.parent, []);
    childrenById.get(node.parent).push(node);
  }
  const children = id => childrenById.get(id) ?? [];
  const root = byId.get("root");
  const ordered = [];
  const ranges = new Map();

  function index(node, parentVisible) {
    const visible = parentVisible && node.visible !== false;
    const start = ordered.length;
    ordered.push(node);
    for (const child of children(node.id)) index(child, visible);
    ranges.set(node.id, { start, end: ordered.length, visible });
  }
  if (root) index(root, true);

  function contains(ancestorId, id) {
    const ancestor = ranges.get(ancestorId);
    const target = ranges.get(id);
    return Boolean(ancestor && target && ancestor.start <= target.start && target.start < ancestor.end);
  }

  function breadcrumbs(id) {
    const path = [];
    if (!ranges.has(id)) return path;
    for (let node = byId.get(id); node; node = byId.get(node.parent)) path.push(node);
    return path.reverse();
  }

  function pick(scopeId, hitNodeId) {
    if (!ranges.has(hitNodeId)) return null;
    if (!scopeId) return { kind: "object", nodeId: root.id };
    if (!contains(scopeId, hitNodeId)) return null;
    if (hitNodeId === scopeId) return { kind: "geometry", nodeId: scopeId };
    let node = byId.get(hitNodeId);
    while (node.parent !== scopeId) node = byId.get(node.parent);
    return { kind: "object", nodeId: node.id };
  }

  function collectFaces(id, isVisible = nodeId => ranges.get(nodeId).visible) {
    const selected = new Map();
    const range = ranges.get(id);
    if (!range) return selected;
    for (let i = range.start; i < range.end; i++) {
      const node = ordered[i];
      if (!node.faces.length || !isVisible(node.id)) continue;
      const faces = new Set();
      for (const face of node.faces) {
        if (node.faceFlags?.[face]?.visible !== false) faces.add(face);
      }
      if (faces.size) selected.set(node.id, faces);
    }
    return selected;
  }

  return { rootId: root?.id ?? null, get: id => byId.get(id), children, contains, breadcrumbs, pick, collectFaces };
}
