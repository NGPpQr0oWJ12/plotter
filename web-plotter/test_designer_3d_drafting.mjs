import assert from "node:assert/strict";
import test from "node:test";
import { Group, PerspectiveCamera, Raycaster, Vector2, Vector3 } from "./static/three-3d.js";
import { createDraftingTools } from "./static/designer-3d-drafting.js";

function setup() {
  const camera = new PerspectiveCamera(45, 1, 0.1, 500);
  camera.position.set(0, 0, 100);
  camera.lookAt(0, 0, 0);
  camera.updateMatrixWorld();
  const rect = { top: 0, left: 0, width: 800, height: 800 };
  const marks = [];
  const hidden = new Set();
  const nodes = new Map();
  const geometry = {
    vertices: [[-20, -20, 0], [20, -20, 0], [20, 20, 0], [-20, 20, 0]],
    triangles: [[0, 1, 2], [0, 2, 3]], faces: ["face", "face"],
  };
  for (const [id, z] of [["base", 0], ["other", 0], ["raised", 8]]) {
    nodes.set(id, { node: { ...geometry, id, vertices: geometry.vertices.map(([x, y]) => [x, y, z]) },
      faceIds: geometry.faces, faceTriangles: new Map([["face", [0, 1]]]), indices: [0, 1] });
  }
  const drafting = createDraftingTools({ getCamera: () => camera, getItems: () => nodes,
    getMarks: () => marks, isNodeVisible: id => !hidden.has(id), getScope: () => null });
  function sample(point, nodeId, shiftKey = false, altKey = false, { faceId = "face", faceIndex = 0,
    normal = new Vector3(0, 0, 1), snapNodeId = nodeId } = {}) {
    const world = new Vector3(...point);
    const normalized = world.clone().project(camera);
    const event = { clientX: (normalized.x + 1) * rect.width / 2, clientY: (1 - normalized.y) * rect.height / 2,
      shiftKey, altKey, pointerType: "mouse" };
    const ray = new Raycaster();
    ray.setFromCamera(new Vector2(normalized.x, normalized.y), camera);
    const object = new Group();
    object.userData.nodeId = snapNodeId;
    const hit = nodeId ? { point: world, object, face: { normal }, faceIndex } : null;
    const surface = hit && { point: world, nodeId, faceId, normal };
    return [event, { hit, surface, ray: ray.ray, rect }];
  }
  return { drafting, marks, hidden, nodes, rect, sample };
}

test("ruler snaps to real boundary endpoints and reports true spatial distance", () => {
  const { drafting, hidden, sample } = setup();
  const first = sample([-19.5, -19.5, 0], "base");
  drafting.hover("measure", ...first);
  assert.equal(drafting.graphics("measure").at(-1).label, "Конец");
  drafting.click("measure", ...first);
  assert.equal(drafting.click("measure", ...sample([-19.5, -19.5, 8], "raised")).message, "Расстояние 8 мм.");
  const dimension = { kind: "dimension", points: [[-20, -20, 0], [-20, -20, 8]], color: "#14715d" };
  assert.deepEqual(drafting.graphics("measure"), [dimension]);
  assert.equal(drafting.undoPoint(), true);
  assert.deepEqual(drafting.graphics("measure"), []);
  assert.equal(drafting.redoPoint(), true);
  assert.deepEqual(drafting.graphics("measure"), [dimension]);
  hidden.add("raised");
  assert.deepEqual(drafting.graphics("measure"), []);
});

test("model edge locks the inferred direction with Shift and Alt bypasses the snap", () => {
  const { drafting, sample } = setup();
  drafting.click("line", ...sample([-19.5, -19.5, 0], "base"));
  drafting.hover("line", ...sample([5, -20, 0], "base"));
  drafting.hover("line", ...sample([7, -4, 0], "base", true));
  const { edge, mark } = drafting.click("line", ...sample([7, -4, 0], "base", true));
  assert.equal(mark, undefined);
  assert.equal(edge.nodeId, "base");
  assert.equal(edge.faceId, "face");
  assert.deepEqual(edge.points[0], [-20, -20, 0]);
  assert.ok(Math.abs(edge.points[1][0] - 7) < 1e-8);
  assert.equal(edge.points[1][1], -20);
  assert.equal(edge.points[1][2], 0);
  drafting.click("line", ...sample([19.5, 19.5, 0], "base", false, true));
  assert.deepEqual(drafting.graphics("line")[0].points, [[19.5, 19.5, 0]]);
});

test("model edge needs two points on the same original planar face and occurrence", () => {
  const { drafting, sample } = setup();
  assert.equal(drafting.click("line", ...sample([-4, 0, 0])).error, true);
  assert.equal(drafting.hasStart(), false);
  drafting.click("line", ...sample([-4, 0, 0], "base", false, true));
  for (const target of [
    sample([8, 0, 0]),
    sample([8, 0, 0], "other"),
    sample([8, 0, 0], "base", false, true, { faceId: "neighbor" }),
    sample([8, 0, 8], "base"),
    sample([8, 0, 0], "base", false, true, { normal: new Vector3(0, 1, 0) }),
  ]) {
    assert.equal(drafting.hover("line", ...target), false);
    assert.equal(drafting.click("line", ...target).error, true);
    assert.equal(drafting.hasStart(), true);
  }
  const { edge } = drafting.click("line", ...sample([8, 0, 0], "base", false, true));
  assert.deepEqual(edge.points, [[-4, 0, 0], [8, 0, 0]]);
});

test("model edge snaps to a created boundary and continues onto a sibling derived face", () => {
  const { drafting, sample, nodes } = setup();
  const item = nodes.get("base");
  const derived = name => ({ ...item.node, id: name, editorSourceNode: "base", editorSourceFace: "face" });
  nodes.set("first", { node: derived("first"), faceIds: ["first"], faceTriangles: new Map([["first", [0]]]), indices: [0] });
  nodes.set("second", { node: derived("second"), faceIds: ["second"], faceTriangles: new Map([["second", [0]]]), indices: [1] });
  const first = sample([-5, -4.7, 0], "base", false, false, { snapNodeId: "second" });
  assert.equal(drafting.hover("line", ...first), true);
  assert.equal(drafting.graphics("line").at(-1).label, "Ребро");
  drafting.click("line", ...first);
  const { edge, normal } = drafting.click("line", ...sample([9, 0, 0], "base", false, true, { snapNodeId: "first" }));
  assert.equal(edge.nodeId, "base");
  assert.equal(edge.faceId, "face");
  assert.ok(Math.abs(edge.points[0][0] - edge.points[0][1]) < 1e-8);
  assert.equal(drafting.hasStart(), false);
  drafting.continueLine(edge, normal);
  const { edge: linked } = drafting.click("line", ...sample([-1, 6, 0], "base", false, true, { snapNodeId: "second" }));
  assert.deepEqual(linked.points, [[9, 0, 0], [-1, 6, 0]]);
});

test("model edge can start at the midpoint of a dangling edge inside a derived face", () => {
  const { drafting, sample, nodes } = setup();
  const item = nodes.get("base");
  nodes.set("derived", { ...item, node: { ...item.node, id: "derived", editorSourceNode: "base", editorSourceFace: "face",
    vertices: [...item.node.vertices, [-4, 5, 0], [4, 5, 0]], edges: [[4, 5]] } });
  const first = sample([0.2, 5.3, 0], "base", false, false, { snapNodeId: "derived" });
  assert.equal(drafting.hover("line", ...first), true);
  assert.equal(drafting.graphics("line").at(-1).label, "Середина");
  drafting.click("line", ...first);
  const { edge } = drafting.click("line", ...sample([8, 10, 0], "base", false, true, { snapNodeId: "derived" }));
  assert.equal(edge.nodeId, "base");
  assert.deepEqual(edge.points, [[0, 5, 0], [8, 10, 0]]);
});

test("a neighboring face can snap to its shared boundary but rejects a point on its own plane", () => {
  const { drafting, sample, nodes } = setup();
  const item = nodes.get("base");
  item.node.vertices.push([-20, -20, 30], [-20, 20, 30]);
  item.node.triangles = [...item.node.triangles, [0, 3, 4], [3, 5, 4]];
  item.indices.push(2, 3);
  item.faceIds = [...item.faceIds, "side", "side"];
  item.faceTriangles.set("side", [2, 3]);
  const side = { faceId: "side", faceIndex: 2, normal: new Vector3(1, 0, 0) };
  drafting.click("line", ...sample([0, -3, 0], "base", false, true));
  assert.equal(drafting.click("line", ...sample([-20, -5, 20], "base", false, false, side)).error, true);
  assert.equal(drafting.click("line", ...sample([-20, -5, 0.2], "base", false, true, side)).error, true);
  const { edge } = drafting.click("line", ...sample([-20, -5, 0.2], "base", false, false, side));
  assert.equal(edge.faceId, "face");
  assert.equal(edge.points[1][0], -20);
  assert.equal(edge.points[1][2], 0);
});

test("guide extends outside its endpoints and can snap drawing in empty space", () => {
  const { drafting, marks, rect, sample } = setup();
  drafting.click("guide", ...sample([-6, 5, 0], null, false, true));
  const { mark } = drafting.click("guide", ...sample([6, 5, 0], null, false, true));
  marks.push(mark);
  const [event] = sample([-25, 5, 0]);
  assert.equal(drafting.hitMark(event, rect), mark.id);
  drafting.hover("guide", ...sample([-25, 5.35, 0]));
  const preview = drafting.graphics("guide").at(-1);
  assert.equal(preview.label, "Направляющая");
  assert.ok(Math.abs(preview.points[0][1] - 5) < 1e-8);
  const plane = { normal: new Vector3(0, 0, 1), offset: 0 };
  const anchor = drafting.snapOnPlane(...sample([-25, 5.35, 0]), plane);
  assert.equal(anchor.label, "Направляющая");
  assert.ok(Math.abs(anchor.point.y - 5) < 1e-8);
  assert.equal(drafting.snapOnPlane(...sample([-25, 5.35, 0], null, false, true), plane), null);
  drafting.cancel();
  drafting.hover("line", ...sample([1, 5.35, 0], "base"));
  assert.deepEqual(drafting.graphics("line").at(-1).points, [[1, 5.35, 0]]);
});
