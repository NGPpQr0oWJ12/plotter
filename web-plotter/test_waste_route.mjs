import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const scripts = name => readFileSync(new URL("./static/" + name, import.meta.url), "utf8");
const app = scripts("app.js");
const helpers = ["makeDrawingProgressModel", "snapshotRunPlan"].map(name =>
  app.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");

test("trim is a separate final knife operation in simulation, time and live telemetry", () => {
  runInNewContext(`${scripts("simulation.js")}\n${helpers}
    const regular = {index: 0, points: [[0, 2], [5, 2]], closed: false};
    const trim = {operation: "wasteTrim", points: [[6, -30], [6, 1420]], closed: false};
    const page = {number: 1, startX: 0, feedAfterMm: 10, paths: [regular], pathEndIndices: [2],
      wasteTrim: {paths: [trim], pathEndIndices: [4]}};
    const plan = {pages: [page], estimatedWorkSeconds: 20, cutSpeedMmS: 2, travelSpeedMmS: 4, feedSpeedMmS: 10};
    function partProgressKey() { return 17; }
    const model = makeDrawingProgressModel(plan, [42]);
    assert.equal(model.total, 1);
    assert.equal(model.pathTotals.size, 1);
    const [part, waste] = model.pathsByPage.get(1);
    assert.equal(part.key, 17);
    assert.equal(waste.key, undefined);
    assert.equal(waste.end, 4);
    assert.equal(model.verticesByPage.get(1)[3], trim.points[0]);
    const sim = Object.create(CutSimulation.prototype);
    sim.buildTimeline(plan);
    assert.equal(sim.moves.at(-1).phase, "feed");
    assert.equal(sim.moves.at(-2).from, trim.points[0]);
    assert.equal(sim.moves.at(-2).to, trim.points[1]);
    assert.equal(sim.moves.at(-2).phase, "cut");
    assert.equal(sim.moves.at(-3).phase, "travel");
    assert.equal(sim.moves.at(-2).short, false);
    assert.equal(snapshotRunPlan(plan, 4).samples.at(-1), sim.workDuration);
  `, { assert });
});
