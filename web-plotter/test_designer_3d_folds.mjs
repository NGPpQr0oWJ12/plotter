import assert from "node:assert/strict";
import test from "node:test";
import { clothFoldSegments } from "./static/designer-3d-folds.js";

function bands(angles) {
  const profile = [[0, 0]];
  for (const angle of angles) {
    const [x, z] = profile.at(-1);
    const radians = angle * Math.PI / 180;
    profile.push([x + 20 * Math.cos(radians), z + 20 * Math.sin(radians)]);
  }
  const rows = [-12, 0, 12];
  const vertices = profile.flatMap(([x, z]) => rows.map(y => [x, y, z]));
  const triangles = [];
  for (let column = 0; column < angles.length; column++) {
    for (let row = 0; row + 1 < rows.length; row++) {
      const a = column * rows.length + row;
      const b = a + rows.length;
      triangles.push([a, b, b + 1], [a, b + 1, a + 1]);
    }
  }
  return { vertices, triangles };
}

test("rounded bands reveal shoulders and a sharp crease without diagonal or perimeter lines", () => {
  const placement = bands([0, 0, 15, 30, 65, 65]);
  const folds = clothFoldSegments(placement);
  assert.deepEqual(folds.map(({ kind, vertices }) => [kind, vertices]), [
    ["transition", [6, 7]], ["transition", [7, 8]], ["transition", [9, 10]],
    ["transition", [10, 11]], ["crease", [12, 13]], ["crease", [13, 14]],
  ]);
  for (const segment of folds) assert.deepEqual(segment.points, segment.vertices.map(index => placement.vertices[index]));
  const mixedWinding = { ...placement, triangles: placement.triangles.map((triangle, index) =>
    index % 2 ? [...triangle].reverse() : triangle) };
  assert.deepEqual(clothFoldSegments(mixedWinding), folds);
});

test("flat and slightly noisy triangulations do not render a relief grid", () => {
  const placement = bands([0, 0, 0, 0]);
  assert.deepEqual(clothFoldSegments(placement), []);
  const vertices = placement.vertices.map(([x, y], index) => [x, y, (index % 3) * (index % 2) * 0.03]);
  assert.deepEqual(clothFoldSegments({ ...placement, vertices }), []);
});

test("directional transition chains continue across non-coplanar triangles", () => {
  const placement = bands([0, 0, 12, 24, 24]);
  const vertices = placement.vertices.map(([x, y, z], index) => [x, y, z + (index % 6 === 4 ? 0.3 : 0)]);
  assert.deepEqual(clothFoldSegments({ ...placement, vertices }).map(fold => fold.vertices), [[6, 7], [7, 8], [9, 10], [10, 11]]);
});

test("disconnected cloth cut banks and non-manifold edges do not create relief seams", () => {
  const placement = {
    vertices: [[0, 0, 0], [0, 25, 0], [20, 0, 0], [0, 0, 0], [0, 25, 0], [0, 0, 20], [-20, 0, 10]],
    triangles: [[0, 1, 2], [3, 4, 5]],
  };
  assert.deepEqual(clothFoldSegments(placement), []);
  assert.deepEqual(clothFoldSegments({ ...placement, triangles: [[0, 1, 2], [0, 1, 5], [0, 1, 6]] }), []);
});
