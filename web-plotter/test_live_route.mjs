import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const helpers = ["sampleOnRoute", "confirmedMotion", "motionPosition", "renderActiveRouteSegment", "pathData", "projectPoint"]
  .map(name => app.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");

function replay(body) {
  runInNewContext(`${helpers}
    const vertices = [null, [0, 0], [10, 0], [10, 10], [25, 10], [25, 20]];
    const start = { point: [5, 0], index: 1 };
    const end = { point: [25, 15], index: 4 };
    const segment = {};
    const $ = () => segment;
    function updateAttribute(node, name, value) { node[name] = value; }
    const motion = confirmedMotion(vertices, start, end, 350);
    ${body}`, { assert });
}

test("only readings on the indexed segment can add a green line", () => replay(`
  const projected = sampleOnRoute(vertices, 2, { x: 10.0006, y: 4 });
  assert.equal(projected.point[0], 10);
  assert.equal(projected.point[1], 4);
  assert.equal(sampleOnRoute(vertices, 2, { x: 9, y: 4 }), null);
  assert.equal(sampleOnRoute(vertices, 2, { x: 10, y: 12 }), null);
  assert.equal(sampleOnRoute(null, 2, { x: 10, y: 4 }), null);
`));

test("sampled head visits the corner and travel leg before stopping at the confirmed point", () => replay(`
  for (const [time, x, y, index] of [
    [0, 5, 0, 1], [50, 10, 0, 2], [100, 10, 5, 2], [150, 10, 10, 3],
    [200, 15, 10, 3], [300, 25, 10, 4], [350, 25, 15, 4], [500, 25, 15, 4],
  ]) {
    const cursor = motionPosition(motion, time);
    assert.equal(cursor.point[0], x);
    assert.equal(cursor.point[1], y);
    assert.equal(cursor.index, index);
  }
  assert.equal(confirmedMotion(vertices, end, start, 350), null);
  assert.equal(confirmedMotion(vertices, end, end, 350), null);
`));

test("manual head follows confirmed XY samples, freezes on lost telemetry and never extends a cut", () => {
  const headFunctions = ["observeHeadPosition", "renderHead"]
    .map(name => app.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");
  runInNewContext(`${helpers}\n${headFunctions}
    let now = 0, nextFrame, headSample, headStaleTimer, headAnimationFrame = null, manualHeadReadUntil = 0;
    const performance = { now: () => now };
    const liveHeadFrame = () => ({ key: "table" });
    const simulation = { active: false }, activeRun = null, live = { responded: true, positionFresh: true };
    const marker = { classList: { toggle() {} }, toggleAttribute(k, value) { this[k] = Boolean(value); } };
    const headMarker = { ...marker }, parkedHeadMarker = { ...marker };
    const camera = { width: 400 }, svg = { clientWidth: 400, classList: marker.classList };
    const positionHeadMarker = (node, x, y) => { node.xy = [x, y]; };
    const requestAnimationFrame = callback => { nextFrame = callback; return 1; };
    const cancelAnimationFrame = () => { nextFrame = null; };
    const document = { hidden: false }, clearTimeout = () => {}, setTimeout = () => 1;
    const renderCutProgress = () => assert.fail("manual position painted a cutting trace");
    function sample(x, y, time, stopped = false) {
      now = time;
      observeHeadPosition({ x, y }, null, stopped);
      renderHead();
    }
    function checkXY(x, y) {
      assert.equal(headMarker.xy[0], -y);
      assert.equal(headMarker.xy[1], -x);
    }
    sample(0, 0, 0);
    sample(18, 24, 120);
    assert.ok(nextFrame);
    nextFrame(180);
    checkXY(9, 12);
    nextFrame(240);
    checkXY(18, 24);
    sample(8, 44, 240);
    nextFrame(300);
    checkXY(13, 34);
    live.responded = false;
    live.positionFresh = false;
    observeHeadPosition(null);
    renderHead(7000);
    checkXY(8, 44);
    assert.equal(headMarker.hidden, false);
    assert.equal(headSample.motion, null);
    assert.equal(nextFrame, null);
    live.responded = live.positionFresh = true;
    sample(20, 10, 8000, true);
    checkXY(20, 10);
    assert.equal(headSample.motion, null);
    sample(32, 10, 8120);
    nextFrame(8180);
    checkXY(26, 10);
    sample(32, 16, 8180);
    checkXY(26, 10);
    nextFrame(8210);
    checkXY(32, 10);
    nextFrame(8240);
    checkXY(32, 16);
  `, { assert });
});

test("active trace draws travel and cut separately, including a closing edge", () => replay(`
  const section = { path: { points: [[25, 10], [25, 20], [35, 20]], closed: true }, entryFrom: [10, 10] };
  renderActiveRouteSegment(motionPosition(motion, 200), section, 0);
  assert.equal(segment.class, "travel-trace");
  assert.equal(segment.d, "M -10.0000 -10.0000 L -10.0000 -15.0000");
  renderActiveRouteSegment(motionPosition(motion, 350), section, 1);
  assert.equal(segment.class, "cut-trace");
  assert.equal(segment.d, "M -10.0000 -25.0000 L -15.0000 -25.0000");
  renderActiveRouteSegment({ point: [30, 15] }, section, 3);
  assert.equal(segment.d, "M -20.0000 -35.0000 L -15.0000 -30.0000");
  renderActiveRouteSegment(null, section, 3);
  assert.equal(segment.d, "");
`));

test("a long contour updates its tail without repainting already drawn chunks", () => {
  const render = app.match(/^function renderVerifiedVertices\([\s\S]*?^}/m)[0];
  runInNewContext(`${helpers}\n${render}
    let verifiedSection = null, paintedVertices = 0;
    const group = { children: [], append(path) { this.children.push(path); }, replaceChildren() { this.children = []; } };
    const entry = {};
    const $ = id => id === "live-verified-vertices" ? group : entry;
    const element = () => ({ writes: 0 });
    function updateAttribute(node, name, value) { node[name] = value; node.writes++; }
    const executedTravel = () => "travel";
    const section = { path: { points: Array.from({ length: 300 }, (_, i) => [i, 0]) } };
    renderVerifiedVertices(section, 130);
    const first = group.children[0];
    renderVerifiedVertices(section, 260);
    assert.equal(group.children.length, 3);
    assert.equal(first.writes, 1);
    assert.match(group.children[1].d, /^M 0.0000 -128.0000 /);
    renderVerifiedVertices(undefined, 0);
    assert.equal(group.children.length, 0);
    assert.equal(entry.d, "");
  `, { assert });
});
