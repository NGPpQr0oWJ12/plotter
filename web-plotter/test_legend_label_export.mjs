import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
const source = ["originalLabelKeys", "legendLabelKey", "localPrinterRequest", "labelRequest"].map(name =>
  app.match(new RegExp(`^(?:async )?function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");

function setup(extra = {}) {
  const ctx = { managedCut: { id: "cut", cutRevision: 4 }, machineModeEpoch: 1,
    machine: { apiVersion: 25 }, centralMode: true, companionConnected: false, demoMode: false,
    workspaceChange: 2, workspaceSaved: 1, layoutVisible: () => true, layoutReadOnly: () => false,
    jsonRequest: value => value, checkedFetch: (url, payload) => ({ url, payload }), ...extra };
  runInNewContext(source, ctx);
  return ctx;
}

test("the layout dock keeps the printer beside the Xiao tools and the printer dialog owns auto and PDF", () => {
  const html = readFileSync(new URL("./static/index.html", import.meta.url), "utf8");
  const workplaces = readFileSync(new URL("./static/workplaces.js", import.meta.url), "utf8");
  const topbar = html.match(/<header\b[^>]*id="workplace-topbar"[^>]*>[\s\S]*?<\/header>/)?.[0];
  const dock = html.match(/<div\b[^>]*id="workplace-dock"[^>]*>[\s\S]*?(?=<section\b)/)?.[0];
  const dialog = html.match(/<dialog\b[^>]*id="label-printer-dialog"[^>]*>[\s\S]*?<\/dialog>/)?.[0];
  const printerButtons = (markup = "") => Array.from(
    markup.matchAll(/<button\b[^>]*\bid="(label-(?:printer|auto|open))"/g), match => match[1]).sort();
  assert.deepEqual(printerButtons(topbar), []);
  assert.deepEqual(printerButtons(dock), ["label-printer"]);
  assert.deepEqual(printerButtons(dialog), ["label-auto", "label-open"]);
  assert.ok(/dock\.append\(\s*xiaoActions,\s*printerActions\s*\)/.test(workplaces),
    "opening a layout must reattach the printer controls after rebuilding the dock");
});

test("printer list and confirmed demo print target the operator's Windows helper from the shared site", async () => {
  const calls = [];
  const ctx = setup({ location: { hostname: "shared.example" }, demoMode: true, workspaceSaved: 2,
    companionFetch: async (url, options) => { calls.push({ url, options }); return { ok: true }; },
    checkedResponse: response => response, checkedFetch: () => assert.fail("printer calls must not go to the central host") });
  await ctx.labelRequest("/api/label-printers");
  await ctx.labelRequest("/api/label-print-part", { cutId: "cut", partKey: "23", printer: "Xprinter", confirmed: true });
  assert.deepEqual(calls.map(call => call.url), ["/api/label-printers", "/api/label-print-part"]);
  assert.equal(calls[1].options.partKey, "23");
  assert.equal(calls[1].options.cutRevision, 4);
  assert.equal(calls[1].options.mode, "demo");
});

test("on localhost a switch of material during save prevents a physical print", async () => {
  const ctx = setup({ location: { hostname: "localhost" }, checkedFetch: () => assert.fail("unexpected print") });
  ctx.persistWorkspace = async () => { ctx.managedCut = { id: "other", cutRevision: 1 }; return true; };
  await assert.rejects(ctx.labelRequest("/api/label-print-part", { cutId: "cut", partKey: "23" }), /изменился/);
});

test("autoprint never downloads a file and only the controlling remote demo registers its Windows helper", async () => {
  const labels = readFileSync(new URL("./static/labels.js", import.meta.url), "utf8");
  const stationSource = labels.slice(labels.indexOf("export function mountLabelStation")).replace(/^export /, "");
  const control = () => ({ setAttribute() {}, append() {}, classList: { remove() {}, add() {} } });
  for (const [autoRelay, token] of [[false, null], [true, null], [true, "control"]]) {
    const requests = [];
    let tick;
    const controls = new Map(["label-actions", "label-printer", "label-auto", "label-open", "label-printer-dialog",
      "label-print-status"].map(id => [id, control()]));
    const ctx = { AbortController, document: { hidden: false, getElementById: id => controls.get(id), addEventListener() {} },
      node: control, clearTimeout() {}, setTimeout(fn) { tick = fn; return 1; },
      downloadLabelPDF: () => assert.fail("automatic download") };
    runInNewContext(stationSource, ctx);
    const station = ctx.mountLabelStation(async (url, data) => {
      requests.push([url, data]);
      return { json: async () => ({ enabled: true, autoPending: true, jobs: [{ id: 1, status: "ready" }],
        autoRelay, relayKey: "run-key" }) };
    });
    station.setContext({ id: "cut", mode: "demo" }, true, false, token);
    await new Promise(resolve => setImmediate(resolve));
    tick();
    await new Promise(resolve => setImmediate(resolve));
    const routes = requests.map(([url]) => url);
    assert.deepEqual(routes, autoRelay && token
      ? ["/api/labels-status", "/api/labels-auto-relay", "/api/labels-status"]
      : ["/api/labels-status", "/api/labels-status"]);
    if (autoRelay && token) assert.equal(requests[1][1].relayKey, "run-key");
  }
});

test("manual export of one piece and of the full set flush the layout and use its saved revision", async () => {
  for (const key of [undefined, "12"]) {
    const ctx = setup({ machine: { apiVersion: 24 } });
    ctx.persistWorkspace = async () => { ctx.managedCut.cutRevision++; return true; };
    const { payload } = await ctx.labelRequest("/api/labels-pdf", { cutId: "cut", cutRevision: 4, ...(key && { key }) });
    assert.equal(payload.cutRevision, 5);
    assert.equal(payload.key, key);
  }
});

test("an old companion, a failed save, or a switch of cut or mode during save cannot send a manual export", async () => {
  for (const change of ["old", "failed", "cut", "mode"]) {
    const ctx = setup({ checkedFetch: () => assert.fail("unexpected PDF request") });
    if (change === "old") { ctx.machine.apiVersion = 24; ctx.companionConnected = true; }
    ctx.persistWorkspace = async () => {
      if (change === "cut") ctx.managedCut = { id: "next", cutRevision: 5 };
      if (change === "mode") ctx.machineModeEpoch++;
      return change !== "failed";
    };
    await assert.rejects(ctx.labelRequest("/api/labels-pdf", { cutId: "cut" }), /обновите|Сохраните|изменился/);
  }
});

test("a read-only unnumbered piece uses its original included path key and mixed groups keep the saved number", () => {
  const ctx = setup();
  ctx.readOnlyLabelKeys = ctx.originalLabelKeys([{}, {}, {}, { partNumber: 7 }, {}], new Set([0]));
  assert.equal(ctx.legendLabelKey({ number: 12, indices: [0, 1, 2] }), "path:0");
  assert.equal(ctx.legendLabelKey({ number: 7, indices: [2, 3, 4] }), "7");
  ctx.readOnlyLabelKeys = null;
  assert.equal(ctx.legendLabelKey({ number: 12, displayNumber: "1/2" }), "12");
});
