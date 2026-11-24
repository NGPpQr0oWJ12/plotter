import assert from "node:assert/strict";
import test from "node:test";
import { createObjectIndex } from "./static/designer-3d-objects.js";

const first = "root/C12";
const second = "root/C123";
const cushion = first + "/G3";
const otherCushion = second + "/G3";
const hiddenGroup = first + "/G4";
const hiddenChild = hiddenGroup + "/C8";

function node(id, parent, name, faces = [], options = {}) {
  return {
    id, parent, name, visible: true, faceFlags: {}, faces,
    vertices: [[0, 0, 0], [1, 0, 0], [0, 1, 0]], triangles: faces.map(() => [0, 1, 2]), ...options,
  };
}

function model() {
  return createObjectIndex([
    node("root", null, "Model", ["base"]),
    node(cushion, first, "Cushion", ["7", "7", "8"], { faceFlags: { 8: { visible: false } } }),
    node(first, "root", "Seat", ["9"]),
    node(second, "root", "Seat"),
    node(otherCushion, second, "Cushion", ["7", "8"]),
    node(hiddenGroup, first, "Seat", [], { visible: false }),
    node(hiddenChild, hiddenGroup, "Cushion", ["10"]),
  ]);
}

test("a hit selects the model, then one immediate child per open scope", () => {
  const index = model();
  assert.equal(index.rootId, "root");
  assert.deepEqual(index.pick(null, cushion), { kind: "object", nodeId: "root" });
  assert.deepEqual(index.pick("", "root"), { kind: "object", nodeId: "root" });
  assert.deepEqual(index.pick("root", cushion), { kind: "object", nodeId: first });
  assert.deepEqual(index.pick("root", otherCushion), { kind: "object", nodeId: second });
  assert.deepEqual(index.pick(first, cushion), { kind: "object", nodeId: cushion });
  for (const id of ["root", first, cushion]) {
    assert.deepEqual(index.pick(id, id), { kind: "geometry", nodeId: id });
  }
  assert.equal(index.pick(first, otherCushion), null);
  assert.equal(index.pick(first, "root"), null);
  assert.equal(index.pick(null, "absent"), null);
  assert.equal(index.pick("absent", cushion), null);
});

test("breadcrumbs, children and containment use occurrence IDs across repeated component definitions", () => {
  const index = model();
  assert.deepEqual(index.breadcrumbs(otherCushion).map(item => item.id), ["root", second, otherCushion]);
  assert.deepEqual(index.breadcrumbs(cushion).map(item => item.name), ["Model", "Seat", "Cushion"]);
  assert.deepEqual(index.children("root"), [index.get(first), index.get(second)]);
  assert.deepEqual(index.children(cushion), []);
  assert.equal(index.contains(first, cushion), true);
  assert.equal(index.contains(first, first), true);
  assert.equal(index.contains(first, otherCushion), false);
  assert.equal(index.contains(second, cushion), false);
});

test("cover sources include own and nested visible faces, deduplicated per occurrence in triangle order", () => {
  const index = model();
  assert.deepEqual([...index.collectFaces("root")], [
    ["root", new Set(["base"])], [first, new Set(["9"])],
    [cushion, new Set(["7"])], [otherCushion, new Set(["7", "8"])],
  ]);
  assert.deepEqual([...index.collectFaces(cushion)], [[cushion, new Set(["7"])]]);
  assert.deepEqual([...index.collectFaces(hiddenGroup)], []);
  assert.deepEqual([...index.collectFaces(hiddenChild)], []);
  assert.deepEqual([...index.collectFaces("absent")], []);
});

test("runtime visibility includes isolated and revealed descendants and preserves face visibility flags", () => {
  const index = model();
  assert.deepEqual([...index.collectFaces("root", id => id === otherCushion)], [[otherCushion, new Set(["7", "8"])]]);
  assert.deepEqual([...index.collectFaces(first, () => true)], [
    [first, new Set(["9"])], [cushion, new Set(["7"])], [hiddenChild, new Set(["10"])],
  ]);
});
