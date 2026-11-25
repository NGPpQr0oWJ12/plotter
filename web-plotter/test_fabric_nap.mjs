import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const functions = ["bounds", "mapPath", "normalizeAngle", "rotateFabricNapPaths", "canFlipFabricNap", "flipFabricNap",
  "indexDrawingParts", "identifyDrawingParts", "expandToWholeParts", "partMembers", "unionPlacementBoxes",
  "drawingHistoryState", "rememberDrawing", "stepDrawingHistory", "workspaceSnapshot"];
const source = functions.map((name) => {
  const match = app.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"));
  assert.ok(match, name);
  return match[0];
}).join("\n");

function runLayoutTest(body) {
  return runInNewContext(`${source}
    const original = [
      { points: [[10.15, 20], [50.3, 20], [50.3, 50], [10.15, 44]], partNumber: 1, partName: "Лекало", closed: true, notch: false, layer: "0" },
      { points: [[20, 25], [28, 25], [24, 30]], partNumber: 1, closed: true, notch: false, layer: "Отверстие" },
      { points: [[10.15, 26], [13, 26]], partNumber: 1, closed: false, notch: true, layer: "Насечка" },
      { points: [[33, 28], [34, 32]], partNumber: 1, closed: false, notch: false, layer: "Внутренний" },
      { points: [[110, 75], [130, 75], [120, 95]], partNumber: 2, partName: "Лекало", closed: true, notch: false, layer: "0", partAngle: 30 },
    ];
    let placed = original;
    let excluded = new Set([2]);
    let selection = new Set();
    let pathBoxes, drawingParts, drawingPartsReady, partDiscovery;
    let fabricNapReversed = false, fabricNapPair = null, napPreviewOriginal = null, napChangePending = false;
    let workspaceChanges = 0, preview = false, cutPlan = {}, pendingCanvasPlan = {}, measurementCloseHistory = null;
    const drawingUndo = [], drawingRedo = [];
    const source = { filename: "fixture.dxf", warnings: [] };
    let nestingSettings = null, routeApplied = true;
    const restoringWorkspace = false, currentUpload = null, uploadUncertain = false, xiaoHandoff = null;
    const activeRun = null, cutProgress = null, controllerRoll = null, placementConflicts = new Set();
    const live = { stopAvailable: false }, simulation = { active: false }, contourDialog = { open: false };
    const svg = { focus() {} };
    const layoutReadOnly = () => preview, layoutPreparation = () => preview, layoutVisible = () => true;
    const drawingLocked = () => false, busy = () => false, placementPreviewActive = () => false;
    const fixedDesignWidth = () => false, workspaceViewStorageKey = () => null, workspaceViewSnapshot = () => ({});
    const renderPartLegend = () => {}, renderControls = () => {}, cancelCanvasGesture = () => {};
    const resetCutPlan = () => {}, updateSelection = () => {}, refreshPlacementWarnings = () => {};
    const scheduleWorkspaceSave = () => { if (!preview) workspaceChanges++; };
    const $ = () => ({ checked: false });
    const jsonRequest = (data) => data;
    const getJson = async (url) => {
      assert.equal(url, "/api/nest-parts");
      return { parts: [{ number: 1, indices: [0, 1, 2, 3], notchIndices: [2] },
        { number: 2, indices: [4], notchIndices: [] }] };
    };
    function updateDrawing({ edited = true } = {}) {
      pathBoxes = placed.map((path) => bounds([path]));
      indexDrawingParts();
      if (edited) scheduleWorkspaceSave();
    }
    updateDrawing({ edited: false });
    (async () => { ${body} })();
  `, { assert, AbortSignal });
}

test("nap rotates each instance in its own bounds and keeps the grouped paths together", () => runLayoutTest(`
  const boxes = [...drawingParts.values()].map(({ box }) => box);
  flipFabricNap();
  assert.deepEqual([...drawingParts.values()].map(({ box }) => box), boxes);
  assert.deepEqual(placed[0].points[0], [50.3, 50]);
  assert.ok(Math.abs(placed[1].points[0][0] - 40.45) < 1e-10);
  assert.equal(placed[1].points[0][1], 45);
  assert.deepEqual(placed[2].points[0], [50.3, 44]);
  assert.ok(Math.abs(placed[3].points[0][0] - 27.45) < 1e-10);
  assert.equal(placed[3].points[0][1], 42);
  assert.deepEqual(placed[4].points[0], [130, 95]);
  assert.equal(placed[4].partAngle, 210);
  assert.equal(placed[2].notch, true);
  assert.equal(excluded.has(2), true);
  assert.equal(fabricNapReversed, true);
  const rotated = placed;
  stepDrawingHistory();
  assert.equal(placed, original);
  assert.equal(fabricNapReversed, false);
  stepDrawingHistory(true);
  assert.equal(placed, rotated);
  assert.equal(fabricNapReversed, true);
  flipFabricNap();
  assert.equal(placed, original);
  assert.equal(fabricNapReversed, false);
`));

test("legacy layouts wait for grouping and preview keeps the original saved geometry", () => runLayoutTest(`
  preview = true;
  placed = original.map(({ partNumber, ...path }) => path);
  updateDrawing({ edited: false });
  flipFabricNap();
  assert.equal(napPreviewOriginal, null);
  await identifyDrawingParts();
  const saved = placed;
  flipFabricNap();
  assert.notEqual(placed, saved);
  assert.deepEqual(placed[2].points[0], [50.3, 44]);
  assert.deepEqual(placed[4].points[0], [130, 95]);
  assert.equal(workspaceSnapshot().paths, saved);
  assert.equal(workspaceSnapshot().optimization.napReversed, undefined);
  assert.equal(workspaceChanges, 0);
  assert.equal(napChangePending, false);
  assert.equal(drawingUndo.length, 0);
  flipFabricNap();
  assert.equal(placed, saved);
  assert.equal(workspaceChanges, 0);
`));
