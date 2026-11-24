import assert from "node:assert/strict";
import test from "node:test";
import { PerspectiveCamera } from "./static/three-3d.js";
import { connectedFaces, fabricSurface, linkedFaces, surfaceZoneFaces, visibleFacesInBox } from "./static/designer-3d-selection.js";

test("connected selection fills a bent strip across hard edges and stops at the mesh island", () => {
  const length = 64;
  const items = { vertices: [], triangles: [], faces: [], edges: [] };
  for (let i = 0; i <= length; i++) {
    const z = i % 2;
    items.vertices.push([i, 0, z], [i, 1, z]);
    items.edges.push([2 * i, 2 * i + 1]);
    if (i === length) break;
    items.triangles.push([2 * i, 2 * i + 2, 2 * i + 1], [2 * i + 2, 2 * i + 3, 2 * i + 1]);
    items.faces.push("strip-" + i, "strip-" + i);
  }
  const next = items.vertices.length;
  items.vertices.push([-1, 1, 0], [-1, 2, 0], [-1, 0, 0], [-1, -1, 0]);
  items.triangles.push([0, next, next + 1], [0, 1, next + 2], [0, next + 2, next + 3]);
  items.faces.push("same-vertex", "hidden-bridge", "behind-bridge");
  const item = { node: items, indices: items.triangles.map((_, i) => i).filter(i => i !== length * 2 + 1) };
  assert.deepEqual([...linkedFaces(item, 0)], ["strip-0"]);
  assert.deepEqual([...connectedFaces(item, 0)], Array.from({ length }, (_, i) => "strip-" + i));
  assert.equal(connectedFaces(item, length * 2 + 1).size, 0);
  assert.deepEqual([...connectedFaces(item, length * 2)], ["same-vertex"]);
});

test("rounded zone includes all bend faces and stops at flat shoulders", () => {
  const sections = [[0, -18], [0, -10], [.76, -6.17], [2.93, -2.93], [6.17, -.76], [10, 0], [60, 0]];
  const names = ["front", "bend-1", "bend-2", "bend-3", "bend-4", "top"];
  const node = {
    vertices: sections.flatMap(([y, z]) => [[0, y, z], [80, y, z]]),
    triangles: names.flatMap((_, i) => [[2 * i, 2 * i + 1, 2 * i + 3], [2 * i, 2 * i + 3, 2 * i + 2]]),
    faces: names.flatMap(name => [name, name]),
    edges: sections.map((_, i) => [2 * i, 2 * i + 1]),
    edgeFlags: sections.map(() => ({ soft: true })),
  };
  const item = { node, indices: node.triangles.map((_, index) => index) };
  for (let strip = 1; strip <= 4; strip++) {
    assert.deepEqual([...surfaceZoneFaces(item, strip * 2)].sort(), names.slice(1, 5));
  }
  assert.deepEqual([...surfaceZoneFaces(item, 0)], ["front"]);
  assert.deepEqual([...surfaceZoneFaces(item, 10)], ["top"]);
});

test("a precise box excludes partially enclosed faces and faces behind the visible zone", () => {
  const camera = new PerspectiveCamera(90, 1, .1, 100);
  camera.position.set(0, 0, 10);
  camera.lookAt(0, 0, 0);
  const view = { left: 0, top: 0, width: 200, height: 200 };
  const box = { left: 50, top: 90, width: 100, height: 45 };
  function quad(id, x0, x1, y0, y1, z) {
    return { mesh: { visible: true }, indices: [0, 1], node: {
      id, vertices: [[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]],
      triangles: [[0, 1, 2], [0, 2, 3]], faces: [id, id],
    } };
  }
  const items = [quad("top", -8, 8, 0, 5, 0), quad("strip", -4, 4, -3, -2, 1),
    quad("behind-strip", -2, 2, -2.6, -2.3, 0)];
  assert.deepEqual([...visibleFacesInBox(items, camera, view, box).keys()].sort(), ["strip", "top"]);
  assert.deepEqual([...visibleFacesInBox(items, camera, view, box, { contained: true }).keys()], ["strip"]);
});

test("fabric mesh welds selected planes while keeping visible source anchors stable", () => {
  const left = {
    vertices: [[0, 0, 0], [8, 0, 0], [8, 10, 0], [0, 10, 0], [4, -5, 0]],
    triangles: [[0, 4, 1], [0, 1, 2], [0, 2, 3], [2, 1, 0]],
    faces: ["hidden", "left", "left", "duplicate"],
    faceFlags: { hidden: { visible: false } },
  };
  const right = {
    vertices: [[8.004, 0, 0], [16, 0, 5], [16, 10, 5], [8.004, 10, 0], [18, 10, 5]],
    triangles: [[0, 1, 2], [0, 2, 3], [1, 4, 2]],
    faces: ["right", "right", "unselected"],
  };
  const parts = [{ node: left, indices: [0, 1, 2, 3] }, { node: right, indices: [0, 1], surfaceIndex: 2 }];
  const expected = {
    vertices: [...left.vertices.slice(0, 4), right.vertices[1], right.vertices[2]],
    triangles: [[0, 1, 2], [0, 2, 3], [1, 4, 5], [1, 5, 2]],
    faces: ["0:left", "0:left", "2:right", "2:right"],
  };
  assert.deepEqual(fabricSurface(parts), expected);
  assert.deepEqual(fabricSurface([{ ...parts[0], indices: [1, 2, 3] }, parts[1]]), expected);
});
