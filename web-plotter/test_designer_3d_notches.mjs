import assert from "node:assert/strict";
import test from "node:test";
import { drawCadOverlay } from "./static/designer-3d-overlay.js";
import { notchPositionOnStitch } from "./static/designer-3d-patterns.js";
import { PerspectiveCamera } from "./static/three-3d.js";

test("notch stays legible as a screen-space tick across the seam", () => {
  const camera = new PerspectiveCamera(45, 2, 0.1, 500);
  camera.position.z = 100;
  const strokes = [];
  let path;
  const context = {
    setTransform() {},
    clearRect() {},
    setLineDash() {},
    beginPath() { path = []; },
    moveTo(x, y) { path.push([x, y]); },
    lineTo(x, y) { path.push([x, y]); },
    stroke() { strokes.push({ path, color: this.strokeStyle, width: this.lineWidth }); },
  };
  const canvas = {
    ownerDocument: { defaultView: { devicePixelRatio: 1 } },
    getBoundingClientRect: () => ({ width: 200, height: 100 }),
    getContext: () => context,
  };
  drawCadOverlay(canvas, camera, [{ kind: "notch", points: [[0, 0, 0], [15, 0, 0]] }]);
  assert.deepEqual(strokes.at(-1), { path: [[100, 42], [100, 58]], color: "#893f16", width: 2 });
  assert.ok(strokes[0].width > strokes[1].width);
});

test("click on a boundary segment maps to its partial, reversed sewing bank", () => {
  const side = { vertices: [2, 4, 7], fractions: [0, 0.25, 1], range: [0.75, 0.25] };
  assert.equal(notchPositionOnStitch([7, 4], 0.5, side), 0.25);
  assert.equal(notchPositionOnStitch([4, 7], 0.5, side), 0.25);
  assert.equal(notchPositionOnStitch([7, 4], 0.125, side), null);
  assert.equal(notchPositionOnStitch([2, 7], 0.5, side), null);
});
