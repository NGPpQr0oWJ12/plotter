import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const functions = ["navigateLayout", "getJson", "jsonRequest", "checkedFetch"]
  .map(name => app.match(new RegExp(`^(?:async )?function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");

function setup() {
  const calls = [], screens = new Map(), memory = new Map();
  const element = id => {
    if (!screens.has(id)) screens.set(id, { hidden: true, dataset: {} });
    return screens.get(id);
  };
  const ctx = {
    demoMode: true, scopedDemoCuts: true, demoCutScope: null, hardwareCutId: null, activeRun: null,
    companionRoutes: new Set(), systemRoutes: new Set(["/api/release-notes"]),
    centralMode: true, companionConnected: false, $: element,
    sessionStorage: { setItem: (key, value) => memory.set(key, value) },
    busy: () => false, layoutVisible: () => !element("layout-workplace").hidden,
    layoutPreparation: () => false, runInProgress: () => Boolean(ctx.activeRun), kitLayout: () => false,
    persistWorkspace: async () => true, readConfiguration: async () => true,
    workspaceQuery: (id, mode) => "/api/operator-workspace?cutId=" + id + "&mode=" + mode,
    setPending() {}, renderControls() {}, clearCurrentUpload() {}, cancelCanvasGesture() {},
    setSaveError() {}, applyWorkspace() {}, requestAnimationFrame() {}, checkedResponse: value => value,
    changeMachineContext(isDemo, cut = null) { ctx.demoMode = isDemo; ctx.demoCutScope = cut; ctx.hardwareCutId = null; },
    applyJobStatus(response) { ctx.hardwareCutId = response.selectedCut; },
    fetch: async (path, options) => {
      const scope = options.headers?.["X-Plotter-Demo-Cut"];
      calls.push({ path, options, scope });
      return { json: async () => ({ status: "active", roll: null, selectedCut: scope }) };
    },
  };
  runInNewContext(functions, ctx);
  return { ctx, calls, memory };
}

test("orders can leave a running demo and return to the same material's virtual controller", async () => {
  const { ctx, calls, memory } = setup();
  for (const id of ["A", "B", "A"]) {
    await ctx.navigateLayout({ id, mode: "demo" });
    assert.equal(ctx.demoCutScope, id);
    assert.equal(memory.get("plotter.lastDemoCut"), id);
    await ctx.getJson("/api/controller-live", ctx.jsonRequest({}));
    assert.equal(calls.at(-1).scope, id);
    ctx.activeRun = { state: "running" };
    await ctx.navigateLayout(null);
    assert.equal(ctx.demoCutScope, null);
    ctx.activeRun = null;
  }
  assert.deepEqual(calls.filter(call => call.path.endsWith("/cut-context")).map(call =>
    [call.scope, JSON.parse(call.options.body).cutId]), [["A", "A"], ["B", "B"], ["A", "A"]]);
  assert(!calls.some(call => call.path.includes("roll-reset")));
});

test("selecting a different demo addresses the target explicitly, while physical requests carry no demo scope", async () => {
  const { ctx, calls } = setup();
  ctx.demoCutScope = "A";
  await ctx.navigateLayout({ id: "B", mode: "demo" });
  assert(calls.every(call => call.scope === "B"));
  await ctx.getJson("/api/release-notes");
  assert.equal(calls.at(-1).scope, undefined);
  ctx.demoMode = false;
  await ctx.getJson("/api/status");
  assert.equal(calls.at(-1).path, "/api/status");
  assert.equal(calls.at(-1).scope, undefined);
});

test("a rejected demo selection leaves the tab bound to the previous material", async () => {
  const { ctx } = setup();
  ctx.demoCutScope = ctx.hardwareCutId = "A";
  const fetch = ctx.fetch;
  ctx.fetch = (path, options) => {
    if (path.endsWith("/cut-context")) throw new Error("Материал недоступен");
    return fetch(path, options);
  };
  await assert.rejects(ctx.navigateLayout({ id: "B", mode: "demo" }), /Материал недоступен/);
  assert.equal(ctx.demoCutScope, "A");
  assert.equal(ctx.hardwareCutId, "A");
});

test("a running demo from an older tab resumes only the same material on the legacy controller", async () => {
  const { ctx } = setup();
  ctx.demoCutScope = "B";
  const select = [];
  const fetch = ctx.fetch;
  ctx.fetch = (path, options) => {
    if (path.endsWith("/cut-context")) {
      const scope = options.headers["X-Plotter-Demo-Cut"], payload = JSON.parse(options.body);
      select.push([scope, payload.cutId, payload.resumeOnly]);
      if (scope) throw Object.assign(new Error(), { details: { code: "demo_legacy_cut" } });
      return { json: async () => ({ status: "active", selectedCut: "A", roll: { state: "advance" } }) };
    }
    return fetch(path, options);
  };
  await ctx.navigateLayout({ id: "A", mode: "demo" });
  assert.deepEqual(select, [["A", "A", undefined], [undefined, "A", true]]);
  assert.equal(ctx.demoCutScope, null);
  assert.equal(ctx.hardwareCutId, "A");
});
