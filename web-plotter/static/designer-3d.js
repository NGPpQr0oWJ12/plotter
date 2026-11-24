import {
  AmbientLight, Box3, BufferGeometry, Color, DirectionalLight, DoubleSide,
  Float32BufferAttribute, Group, LineBasicMaterial, LineSegments, Mesh, MeshStandardMaterial, OrbitControls,
  PerspectiveCamera, Raycaster, Scene, Triangle, Vector2, Vector3, WebGLRenderer,
} from "/three-3d.js";
import { fabricSurface, surfaceZoneFaces, visibleFacesInBox } from "/designer-3d-selection.js";
import { withFabricWeave } from "/designer-3d-fabric.js";
import { clothFoldSegments } from "/designer-3d-folds.js";
import { clothBoundary, createClothOutline } from "/designer-3d-outline.js";
import { drawCadOverlay } from "/designer-3d-overlay.js";
import { mountPatternTools } from "/designer-3d-patterns.js";
import { confirmAction } from "/confirm-action.js";
import { createObjectIndex } from "/designer-3d-objects.js";
import { magneticEdges, worldEdgeKey } from "/designer-3d-magnet.js";
import { createDraftingTools, draftingTools } from "/designer-3d-drafting.js";

const api = "/api/cad/3d/";
const fabricColors = ["#3179c5", "#ce6949", "#469273", "#8b66ac", "#b88829", "#348a93",
  "#b76088", "#787aad", "#6e883e", "#a7613e", "#846d96", "#5481aa"];
let fabricListId = 0;
const toolSpecs = [
  ["select", "select", "Ткани и разметка", "V"], ["box", "rectangle", "Грани для ткани", "B"],
  ["outline", "polygon", "Полилиния", "P"],
  ["arc", "arc-three", "Дуга", "A"], ["measure", "measure", "Линейка", "D"],
  ["line", "line", "Отрезок", "L"], ["guide", "reference", "Направляющая", "H"],
  ["split", "polyline", "Разделить швом", "K"], ["slash", "slit", "Надрез", "T"],
  ["sew", "join", "Шов и насечки", "J"], ["notch", "notch", "Насечка", "N"],
  ["erase", "erase", "Ластик", "E"],
];
const contourTools = new Set(["outline", "arc"]);
const cutTools = new Map([["split", "seam"], ["slash", "slash"]]);
const faceTools = new Set(["box", ...contourTools]);
const clothInk = { selected: "#152034", a: "#073c6a", b: "#753815", hovered: "#40546b" };

function routeSlice(points, from, to) {
  const distances = [0];
  for (let i = 1; i < points.length; i++) distances.push(distances[i - 1] + points[i].distanceTo(points[i - 1]));
  const length = distances.at(-1);
  if (!length) return [];
  const start = Math.max(0, Math.min(1, Math.min(from, to))) * length;
  const end = Math.max(0, Math.min(1, Math.max(from, to))) * length;
  const at = distance => {
    const index = distances.findIndex((value, i) => i > 0 && value >= distance && value > distances[i - 1]);
    return points[index - 1].clone().lerp(points[index],
      (distance - distances[index - 1]) / (distances[index] - distances[index - 1]));
  };
  const path = [at(start), ...points.filter((_, i) => distances[i] > start && distances[i] < end), at(end)];
  if (from > to) path.reverse();
  return path.map(point => point.toArray());
}

function sameEdgePath(a, b) {
  if (a.length < 2 || a.length !== b.length) return false;
  const matches = (start, step) => a.every(([x, y, z], index) => {
    const [u, v, w] = b[start + step * index];
    return Math.hypot(x - u, y - v, z - w) < .05;
  });
  return matches(0, 1) || matches(b.length - 1, -1);
}

export function mountSurfaceEditor(host, { request, initial, onChange, onTransfer, onSyncPatterns, history, icon }) {
  const doc = host.ownerDocument;
  const listId = "surface-fabrics-" + ++fabricListId;
  const toolButtons = toolSpecs.map(([id, symbol, label, shortcut]) =>
    '<button type="button" class="button cad-icon-button" data-tool="' + id + '" title="' + label + ' · ' + shortcut +
    '" aria-label="' + label + '" aria-pressed="false">' + icon(symbol) + '</button>').join("");
  host.innerHTML = `
    <header class="surface-toolbar">
      <nav class="surface-path" aria-label="Путь в модели"></nav>
      <button class="button surface-fit" type="button" disabled>Вписать</button>
      <button class="button" type="button" data-run="hide-object" hidden>Скрыть объект</button>
      <button class="button" type="button" data-run="restore" hidden>Показать всё</button>
      <button class="button" type="button" data-run="cloth-view" aria-pressed="false" disabled>Только ткань</button>
      <button class="button primary" type="button" data-run="name-fabric" hidden>Назначить ткань</button>
    </header>
    <div class="surface-work">
      <aside class="surface-tools" aria-label="Инструменты 3D">${toolButtons}
        <button class="button cad-icon-button" type="button" data-run="history-back" title="Назад · Ctrl+Z" aria-label="Назад">${icon("undo")}</button>
        <button class="button cad-icon-button" type="button" data-run="history-forward" title="Вперёд · Ctrl+Shift+Z" aria-label="Вперёд">${icon("redo")}</button>
      </aside>
      <div class="surface-viewport">
        <p class="surface-placeholder">Импортируйте модель SketchUp.</p>
        <canvas class="surface-overlay" aria-hidden="true"></canvas>
        <div class="surface-box" hidden></div>
        <div class="surface-line-actions" hidden>
          <button class="button primary" type="button" data-run="finish">Замкнуть</button>
          <button class="button" type="button" data-run="undo">Убрать точку</button>
          <button class="button" type="button" data-run="cancel">Отмена</button>
        </div>
        <div class="cad-context-menu surface-context-menu" role="menu" aria-label="Действия на модели" hidden></div>
      </div>
      <aside class="surface-inspector" aria-label="Ткани на модели">
        <h2>Ткани</h2>
        <input class="surface-search" type="search" aria-label="Поиск ткани" placeholder="Название или номер детали">
        <div class="surface-assigned-fabrics"></div>
        <div class="surface-actions">
          <button class="button" type="button" data-run="isolate-fabric" hidden>Изолировать</button>
          <button class="button" type="button" data-run="edit-contour" hidden>Контур</button>
          <button class="button" type="button" data-run="remove-fabric" hidden>Удалить</button>
        </div>
        <section class="surface-seam-palette" aria-label="Шов и насечки" hidden>
          <div class="surface-seam-head"><strong>Шов и насечки</strong>
            <button class="button" type="button" data-run="close-palette" aria-label="Закрыть инструменты шва">×</button>
          </div>
          <div class="surface-pattern-host"></div>
        </section>
      </aside>
    </div>
    <dialog class="surface-fabric-dialog">
      <form class="surface-form">
        <h2>Назначить ткань</h2>
        <label>Название ткани
          <input type="text" name="fabric" list="${listId}" maxlength="80" placeholder="Название ткани" autocomplete="off" required>
        </label>
        <datalist id="${listId}"></datalist>
        <label class="surface-color">Цвет <input type="color" name="color" value="${fabricColors[0]}" aria-label="Цвет ткани"></label>
        <p class="surface-fabric-message" role="status"></p>
        <div class="surface-actions">
          <button class="button" type="button" data-run="cancel-fabric">Отмена</button>
          <button class="button primary" type="submit">Назначить</button>
        </div>
      </form>
    </dialog>
    <p class="surface-status" role="status" aria-live="polite"></p>`;

  const viewport = host.querySelector(".surface-viewport");
  const placeholder = host.querySelector(".surface-placeholder");
  const path = host.querySelector(".surface-path");
  const fitButton = host.querySelector(".surface-fit");
  const form = host.querySelector(".surface-form");
  const nameField = form.elements.fabric;
  const choices = form.querySelector("datalist");
  const status = host.querySelector(".surface-status");
  const fabricDialog = host.querySelector(".surface-fabric-dialog");
  const fabricMessage = host.querySelector(".surface-fabric-message");
  const canvasOverlay = host.querySelector(".surface-overlay");
  const marquee = host.querySelector(".surface-box");
  const tools = [...host.querySelectorAll("[data-tool]")];
  const actions = Object.fromEntries([...host.querySelectorAll("[data-run]")].map(button => [button.dataset.run, button]));
  const search = host.querySelector(".surface-search");
  const assignedList = host.querySelector(".surface-assigned-fabrics");
  const patternDetails = host.querySelector(".surface-seam-palette");
  const lineActions = host.querySelector(".surface-line-actions");
  const contextMenu = host.querySelector(".surface-context-menu");
  const lifecycle = new AbortController();
  const signal = lifecycle.signal;
  const items = new Map();
  const fabrics = new Map();
  const placementMeshes = new Map();
  let attachment = initial;
  let hierarchy;
  let scope = null;
  let selection = new Map();
  let planeClick = null;
  let mode = "select";
  let hiddenNodes = new Set();
  let fabricOnly = false;
  let isolatedFabric = null;
  let selectedFabric = null;
  let clothPick = null;
  let stitchPanels = [];
  const legacyMeshes = new Map();
  let importedModel;
  let selectedObject = null;
  let selectedMark = null;
  let eraseHover = null;
  let erasePathCache = null;
  let confirmingRemoval = false;
  let cancelPointerGesture;
  let draft = null;
  let cutDraft = null, cutHover = null;
  const draftBack = [], draftForward = [];
  let draftPreview = null;
  let contourDrag = null;
  let dragFrame = 0;
  let previewTimer = null;
  let previewInFlight = false;
  let routeInFlight = false;
  let hoverPoint = null;
  let hoveredFace = null;
  let hoveredPanel = null;
  let hoverFrame = 0;
  let geometryResult = null;
  let focusedJoin = null;
  let pairTarget = null;
  let pairHighlight = [];
  let seamHover = null;
  let notchHoverFeedback = false;
  let focusedPanel = null;
  let buttonMap;
  let customColor = false;
  let renderer, scene, camera, controls, objects, panels, cutLines, observer;
  let visible = false;
  let pending = false;

  const ray = new Raycaster();
  const tagRay = new Raycaster();
  const neutral = new Color("#ffffff");
  const accent = new Color("#1474b3");
  const hoverTint = new Color("#77c5ec");
  const screenPoint = new Vector2();
  const foldCache = new WeakMap();
  const boundaryCache = new WeakMap();
  const outlineViewport = { value: new Vector2(1, 1) };
  const outlineNear = { value: 0.1 };
  const keyFor = value => value.toLocaleLowerCase("ru");
  const state = () => attachment?.state || {};
  const drafting = createDraftingTools({
    getCamera: () => camera, getItems: () => items, getMarks: () => state().drafting || [],
    isNodeVisible: id => Boolean(hierarchy) && nodeVisible(id), getScope: () => scope, getTarget: () => controls?.target,
  });
  const patternTools = mountPatternTools(host.querySelector(".surface-pattern-host"), {
    getAttachment: () => attachment,
    request,
    onState: next => commitState(next, false),
    onBusyChange: updateControls,
    onTransfer: (entities, transfer) => onTransfer(entities, transfer),
    onGeometry(result) {
      if (!result) return;
      geometryResult = result;
      updatePanels();
      if (result.cadSync) Promise.resolve(onSyncPatterns?.(result.cadSync, attachment)).then(report => {
        if (geometryResult !== result) return;
        const issue = report?.conflicts?.[0] || report?.issues?.[0];
        if (issue) notify(issue.message, true);
      }).catch(error => { if (geometryResult === result) notify(error.message, true); });
    },
    onJoinSelection(join) { focusedJoin = join; render(); },
    onPickEdge(side) {
      chooseTool("sew");
      setPairTarget(side);
      openPatterns(side === "mark" ? "notches" : side === "allowance" ? "transfer" : "seams");
      notify(side === "a" || side === "b" ? "Щёлкните по подсвеченной кромке стороны " + (side === "a" ? "А." : "Б.") :
        "Щёлкните по нужной кромке на модели.");
    },
    onCancelPick() { setPairTarget(null); },
    onPairHighlight(edges) {
      pairHighlight = edges;
      paintClothSelection();
      renderAssignedFabrics();
      render();
    },
    onPanelSelection: focusPanel,
  });
  function notify(message, error = false) {
    status.textContent = message;
    status.classList.toggle("is-error", error);
    if (fabricDialog.open) fabricMessage.textContent = message;
  }

  function render() {
    if (!renderer || !visible) return;
    outlineNear.value = camera.near;
    renderer.render(scene, camera);
    const annotations = drafting.graphics(mode, selectedMark);
    const modelEdge = !fabricOnly && state().modelEdges?.find(edge => edge.id === selectedMark && nodeVisible(edge.nodeId));
    if (modelEdge) annotations.push({ kind: "line", points: modelEdge.points, color: "#1474b3", strong: true });
    addFabricTags(annotations);
    if (draft) {
      const points = draft.outline || [...(draft.route?.points || []),
        ...draft.points.slice(draft.route?.count || 0).map(point => point.toArray())];
      if (draft.closed && !draft.outline) points.push(points[0]);
      const tint = form.elements.color.value;
      annotations.push({ kind: "seam", points, color: tint, dashed: !draft.closed, strong: true });
      if (!draft.closed && hoverPoint && points.length) {
        const preview = draft.arcMid ? arcSamples(hoverPoint) || [hoverPoint] : [hoverPoint];
        annotations.push({ kind: "seam", points: [points.at(-1), ...preview.map(point => point.toArray())], color: tint, dashed: true });
      }
      for (const [index, point] of draft.points.entries()) {
        const moving = contourDrag?.index === index;
        annotations.push({ kind: "point", points: [point.toArray()], glyph: "square", strong: moving,
          color: moving ? "#e07a30" : "#08638a" });
      }
      if (draft.seed) annotations.push({ kind: "point", points: [anchorPoint(draft.surface.geometry, draft.seed).toArray()],
        glyph: "cross", color: tint });
    }
    if (cutTools.has(mode)) {
      const points = cutDraft?.path.map(item => item.point.toArray()) || [];
      const color = mode === "split" ? "#156c86" : "#a44927";
      if (points.length > 1) annotations.push({ kind: "seam", points, color, strong: true });
      for (const point of points) annotations.push({ kind: "point", points: [point], glyph: "square", color });
      if (cutHover) {
        const point = cutHover.point.toArray();
        if (points.length) annotations.push({ kind: "seam", points: [points.at(-1), point], color, dashed: true });
        annotations.push({ kind: "point", points: [point], glyph: cutHover.snap ? "diamond" : "cross", color,
          label: cutHover.boundary ? "Кромка" : cutHover.snap ? "Скругление" : "" });
      }
    }
    if (mode === "sew") {
      const seams = pairHighlight.flatMap(edge => {
        if (!edge || !placementMeshes.has(edge.panelId)) return [];
        const [from, to] = edge.direction < 0 ? [...edge.range].reverse() : edge.range;
        if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) return [];
        const points = routeSlice(edge.points.map(point => new Vector3(...point)), from, to);
        return [{ kind: "seam", points, endpoints: true, directional: true, color: edge.side === "Б" ? clothInk.b : clothInk.a,
          label: "Кромка " + edge.side, strong: true }];
      });
      if (seams.length === 2 && sameEdgePath(seams[0].points, seams[1].points)) {
        annotations.push({ ...seams[0], directional: false, color: clothInk.selected, label: "Кромки А и Б" });
      } else annotations.push(...seams);
    }
    if (seamHover && (mode === "sew" || mode === "notch")) {
      const role = pairTarget === "b" ? "b" : pairTarget === "a" ? "a" : "selected";
      annotations.push({ kind: "seam", points: seamHover.join.points, color: clothInk[role], strong: true, endpoints: true,
        dashed: true, label: pairTarget === "a" ? "Кромка А" : pairTarget === "b" ? "Кромка Б" : seamHover.join.edgeLabel || "Кромка" });
      if (mode === "notch" || pairTarget === "mark") annotations.push({ kind: "point", points: [seamHover.point.toArray()],
        color: "#713116", glyph: "diamond", strong: true });
    }
    if (mode === "sew" && focusedJoin && isLayerVisible(focusedJoin.nodeId)) {
      annotations.push({ kind: "seam", points: focusedJoin.points, color: "#076896", strong: true });
    }
    if (mode === "erase" && eraseHover?.points) {
      const { points, type } = eraseHover;
      const kind = type === "guide" || type === "line" ? type : "seam";
      annotations.push({ kind, points, color: "#942b21", strong: true, endpoints: true });
    }
    for (const { mark, point, tangentEnd } of notchesOnModel()) annotations.push({
      kind: "notch", points: [point.toArray(), tangentEnd.toArray()], color: "#893f16",
      ...(mode === "notch" ? { label: mark.label } : {}),
    });
    if (mode === "erase" && eraseHover?.point) annotations.push({
      kind: "point", points: [eraseHover.point.toArray()], color: "#942b21", glyph: "diamond", strong: true,
    });
    drawCadOverlay(canvasOverlay, camera, annotations);
  }

  function resize() {
    if (!visible) return;
    const { width, height } = viewport.getBoundingClientRect();
    if (!width || !height) return;
    outlineViewport.value.set(width, height);
    renderer.setSize(width, height, false);
    const aspect = width / height;
    const scale = Math.min(camera.aspect, 1) / Math.min(aspect, 1);
    camera.position.sub(controls.target).multiplyScalar(scale).add(controls.target);
    camera.aspect = aspect;
    camera.updateProjectionMatrix();
    render();
  }

  function fit(specific = false, view = null) {
    const bounds = new Box3();
    if (!camera) return;
    const part = (specific || isolatedFabric) && !selection.size && (placementMeshes.get(isolatedFabric || focusedPanel) || legacyMeshes.get(isolatedFabric));
    if (part) bounds.setFromObject(part);
    for (const [id, item] of items) {
      if (part || fabricOnly) break;
      if (!item.mesh.visible) continue;
      const faces = specific && selection.get(id);
      if (faces?.size) {
        for (const face of faces) for (const index of item.faceTriangles.get(face)) {
          for (const vertex of item.node.triangles[item.indices[index]]) bounds.expandByPoint(new Vector3(...item.node.vertices[vertex]));
        }
      } else if (!specific || !selection.size && selectedObject && hierarchy.contains(selectedObject, id)) bounds.union(item.bounds);
    }
    if (fabricOnly && bounds.isEmpty()) {
      for (const mesh of [...placementMeshes.values(), ...legacyMeshes.values()]) bounds.expandByObject(mesh);
    }
    if (specific && bounds.isEmpty()) return fit(false, view);
    if (bounds.isEmpty()) return;
    const radius = Math.max(bounds.getSize(new Vector3()).length() / 2, 1);
    const distance = radius / Math.sin(camera.fov * Math.PI / 360) / Math.min(camera.aspect, 1);
    const direction = view ? new Vector3(...view).normalize() : camera.position.clone().sub(controls.target).normalize();
    controls.target.copy(bounds.getCenter(new Vector3()));
    camera.position.copy(controls.target).addScaledVector(direction, distance);
    camera.near = Math.max(radius / 1000, 0.01);
    camera.far = distance * 30;
    controls.minDistance = Math.max(radius * 0.005, camera.near * 2);
    controls.maxDistance = distance * 12;
    camera.updateProjectionMatrix();
    controls.update();
    render();
  }

  function attachScene() {
    if (renderer) return;
    try { renderer = new WebGLRenderer({ antialias: true }); }
    catch { throw new Error("Для просмотра 3D включите аппаратное ускорение WebGL в браузере."); }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const canvas = renderer.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute("aria-label", "3D-модель. V выбирает ткань и разметку. B выбирает грани основы.");
    viewport.prepend(canvas);
    scene = new Scene();
    scene.background = new Color("#edf1f4");
    camera = new PerspectiveCamera(40, 1, 0.1, 1e7);
    camera.position.set(1, -1.6, 1);
    camera.up.set(0, 0, 1);
    controls = new OrbitControls(camera, canvas);
    controls.zoomToCursor = true;
    controls.screenSpacePanning = true;
    buttonMap = { orbit: controls.mouseButtons.LEFT, pan: controls.mouseButtons.RIGHT };
    controls.mouseButtons.MIDDLE = buttonMap.orbit;
    updateMouseMode();
    controls.addEventListener("change", render);
    const light = new DirectionalLight(0xffffff, 1.8);
    light.position.set(1, -2, 3);
    scene.add(new AmbientLight(0xffffff, 1.05), light);
    objects = new Group();
    panels = new Group();
    cutLines = new Group();
    scene.add(objects, panels, cutLines);
    observer = new ResizeObserver(resize);
    observer.observe(viewport);

    let pointer, secondary;
    let skipClick = false;
    canvas.addEventListener("contextmenu", event => {
      event.preventDefault();
      if (!secondary?.moved) openContextMenu(event);
      secondary = null;
    }, { signal });
    canvas.addEventListener("pointerdown", event => {
      closeContextMenu();
      clearHover();
      if (event.button === 2) secondary = { x: event.clientX, y: event.clientY, moved: false };
      if (!event.isPrimary || event.button !== 0 || pending) return;
      const index = contourHandle(event);
      if (index >= 0) {
        event.preventDefault();
        event.stopImmediatePropagation();
        contourDrag = { id: event.pointerId, index, anchor: draft.anchors[index], point: draft.points[index] };
        canvas.setPointerCapture(event.pointerId);
        canvas.focus({ preventScroll: true });
        canvas.style.cursor = "grabbing";
        skipClick = true;
        updateControls();
        render();
        return;
      }
      pointer = { x: event.clientX, y: event.clientY, id: event.pointerId, dragged: false };
      skipClick = false;
      if (mode === "box") canvas.setPointerCapture(event.pointerId);
    }, { signal, capture: true });
    canvas.addEventListener("pointermove", event => {
      if (secondary && (event.buttons & 2) &&
          Math.hypot(secondary.x - event.clientX, secondary.y - event.clientY) > 5) secondary.moved = true;
      if (contourDrag?.id === event.pointerId) {
        cancelAnimationFrame(dragFrame);
        dragFrame = requestAnimationFrame(() => moveContourHandle(event));
        return;
      }
      if (pointer?.id === event.pointerId && Math.hypot(pointer.x - event.clientX, pointer.y - event.clientY) >= 5) {
        pointer.dragged = true;
        if (mode === "box") drawMarquee(boxFrom(pointer, event), boxContainsFaces(pointer, event));
      } else if (!event.buttons && event.pointerType !== "touch") {
        queueHover(event);
      } else {
        clearHover();
      }
    }, { signal });
    canvas.addEventListener("pointerleave", () => {
      if (!contourDrag) canvas.style.cursor = "";
      clearHover();
    }, { signal });
    canvas.addEventListener("pointerup", event => {
      if (contourDrag?.id === event.pointerId) {
        cancelAnimationFrame(dragFrame);
        moveContourHandle(event);
        stopContourDrag();
        void refreshDraftPreview();
        return;
      }
      if (pointer?.id !== event.pointerId) return;
      if (mode === "box" && pointer.dragged) {
        planeClick = null;
        const rect = canvas.getBoundingClientRect();
        const found = visibleFacesInBox(items.values(), camera, rect, boxFrom(pointer, event),
          { contained: boxContainsFaces(pointer, event) });
        selectFaces(found, selectionOperation(event));
        marquee.hidden = true;
      }
      skipClick = pointer.dragged;
      pointer = null;
    }, { signal });
    function cancelPointer(event) {
      if (contourDrag?.id === event.pointerId) stopContourDrag(true);
      if (pointer?.id === event.pointerId) pointer = null;
      skipClick = true;
      marquee.hidden = true;
      clearHover();
    }
    cancelPointerGesture = () => {
      const id = pointer?.id;
      pointer = secondary = null;
      skipClick = true;
      marquee.hidden = true;
      if (id != null && canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
      stopContourDrag();
    };
    canvas.addEventListener("pointercancel", cancelPointer, { signal });
    canvas.addEventListener("lostpointercapture", event => {
      if (contourDrag?.id === event.pointerId) cancelPointer(event);
    }, { signal });
    canvas.addEventListener("click", event => {
      if (!skipClick && !pending && !["orbit", "pan"].includes(mode)) pick(event);
    }, { signal });
    host.addEventListener("keydown", onKey, { signal });
    doc.addEventListener("keydown", onEscapeKey, { signal, capture: true });
    doc.addEventListener("keydown", onHistoryKey, { signal, capture: true });
    doc.addEventListener("pointerdown", event => {
      if (!contextMenu.contains(event.target)) closeContextMenu();
    }, { signal });
  }

  function boxFrom(start, event) {
    const rect = viewport.getBoundingClientRect();
    const x = Math.max(rect.left, Math.min(rect.right, event.clientX));
    const y = Math.max(rect.top, Math.min(rect.bottom, event.clientY));
    return { left: Math.min(start.x, x), top: Math.min(start.y, y), width: Math.abs(x - start.x), height: Math.abs(y - start.y) };
  }

  function boxContainsFaces(start, event) {
    return event.shiftKey || event.clientX >= start.x;
  }

  function drawMarquee(box, contained) {
    const rect = viewport.getBoundingClientRect();
    marquee.style.left = box.left - rect.left + "px";
    marquee.style.top = box.top - rect.top + "px";
    marquee.style.width = box.width + "px";
    marquee.style.height = box.height + "px";
    marquee.classList.toggle("is-crossing", !contained);
    marquee.hidden = false;
  }

  function updateMouseMode() {
    if (controls) controls.mouseButtons.LEFT = mode === "orbit" ? buttonMap.orbit : mode === "pan" ? buttonMap.pan : null;
    viewport.dataset.mode = mode;
  }

  function chooseTool(next, { canceling = false } = {}) {
    if (pending && !canceling || !hierarchy) return;
    if (next !== mode) {
      planeClick = null;
      closeContextMenu();
      cancelClothCut();
      drafting.cancel();
      clearHover();
      if (!(contourTools.has(next) && contourTools.has(mode))) cancelDraft();
      else if (draft) draft.arcMid = draft.arcPlane = null;
      if (next === "select" && selection.size) selectFaces(new Map());
    }
    if (contourTools.has(next) || next === "box" || next === "line") {
      if (fabricOnly) setFabricView(false);
    }
    if (next !== "sew") setPairTarget(null);
    if (next !== "sew" && next !== "notch") patternDetails.hidden = true;
    const previousMode = mode;
    mode = next;
    if (next === "sew" && previousMode !== "sew") {
      if (!fabricOnly) setFabricView(true);
      isolatedFabric = null;
      stitchPanels = [];
      setPairTarget(null);
      patternTools.setStitchPanels?.([]);
      updatePanels();
      openPatterns("seams");
    } else if (next === "notch") {
      openPatterns("notches");
      patternTools.setNotchStatus?.(null);
    }
    paintClothSelection();
    renderAssignedFabrics();
    updateMouseMode();
    updateControls();
    notify(toolHint());
    render();
  }

  function toolHint() {
    if (mode === "box") return "Щёлкайте по граням, чтобы собрать ткань. Повторный щелчок снимает грань, три быстрых выбирают связанную зону. Можно выделить грани рамкой.";
    if (mode === "outline") return "Укажите точки на поверхности модели. Щёлкните по первой точке или нажмите Enter, чтобы замкнуть контур.";
    if (mode === "arc") return "Щёлкните начало, точку на дуге и конец. Добавляйте дуги к полилинии.";
    if (mode === "sew") return "Выберите две ткани, затем укажите кромку шва на каждой.";
    if (mode === "notch") return "Укажите точку на кромке ткани. Если кромки сшиты, насечка появится на обеих.";
    if (mode === "erase") return "Щёлкните по разметке или ткани, чтобы удалить её. Escape возвращает к выбору.";
    if (cutTools.has(mode)) {
      if (!cutDraft?.path.length) return "Укажите начало на кромке ткани. Линии скругления помогают выбрать направление.";
      if (mode === "split") return "Проведите шов по ткани до другой точки кромки. Enter применяет шов.";
      return "Укажите конец надреза внутри ткани. Enter применяет надрез.";
    }
    if (draftingTools.has(mode)) return drafting.hint(mode);
    return "Щёлкните по ткани, ребру или разметке. Двойной щелчок по модели открывает группу. Для назначения ткани выберите «Грани для ткани» (B).";
  }

  function onKey(event) {
    if (pending || fabricDialog.open || event.target.matches("input, textarea, select") || event.ctrlKey || event.metaKey || event.altKey) return;
    const tool = toolSpecs.find(([, , , shortcut]) => event.code === "Key" + shortcut);
    if (tool) chooseTool(tool[0]);
    else if (event.code === "KeyF") fit(true);
    else if (event.key === "Enter" && !event.target.closest("button")) {
      if (cutDraft?.path.length) finishClothCut();
      else if (draft && !draft.closed) finishDraft();
      else if (!actions["name-fabric"].hidden) openFabricDialog();
      else return;
    }
    else if (event.key === "Backspace" && (draft || cutDraft)) {
      if (cutDraft) changeCutPoint(false);
      else undoPoint();
    }
    else if (event.key === "Delete" && selectedMark && !draft) {
      removeDraftMark(selectedMark);
    } else return;
    event.preventDefault();
    event.stopPropagation();
  }

  function onEscapeKey(event) {
    if (event.key !== "Escape" || event.isComposing || event.defaultPrevented || !visible || !hierarchy) return;
    const dialog = doc.querySelector("dialog[open]");
    if (dialog) {
      if (dialog === fabricDialog || confirmingRemoval && dialog.id === "confirm-action-dialog") {
        dialog.addEventListener("close", resetSelectTool, { once: true, signal });
      }
      return;
    }
    if (event.target !== doc.body && !host.parentElement.contains(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
    resetSelectTool();
  }

  function resetSelectTool() {
    const wasSelecting = mode === "select";
    cancelPointerGesture?.();
    closeContextMenu();
    setPairTarget(null);
    if (stitchPanels.length) {
      stitchPanels = [];
      patternTools.setStitchPanels([]);
    }
    chooseTool("select", { canceling: true });
    if (wasSelecting) {
      selectedMark = null;
      clearFabricSelection();
      if (selection.size) selectFaces(new Map());
      else render();
    }
    renderer.domElement.focus({ preventScroll: true });
  }

  function onHistoryKey(event) {
    if (!visible || event.defaultPrevented || event.altKey || !(event.ctrlKey || event.metaKey) ||
        event.isComposing || doc.querySelector("dialog[open]")) return;
    const target = event.target;
    if (target !== doc.body && !host.parentElement.contains(target) || target.matches("input, textarea, select, [contenteditable]")) return;
    const key = /^Key[YZ]$/.test(event.code) ? event.code.at(-1).toLowerCase() : event.key.toLowerCase();
    if (key !== "z" && (key !== "y" || event.shiftKey)) return;
    event.preventDefault();
    event.stopPropagation();
    runHistory(key === "y" || event.shiftKey);
  }

  function disposeMeshes() {
    for (const { mesh, edgeLines } of items.values()) {
      objects.remove(mesh);
      mesh.geometry.dispose();
      mesh.material.dispose();
      if (edgeLines) disposeMesh(edgeLines);
    }
    items.clear();
  }

  function meshFor(node) {
    const positions = [];
    const faceIds = [];
    const indices = [];
    const faceTriangles = new Map();
    const replaced = new Set(node.editorHiddenFaces);
    for (const [index, vertices] of node.triangles.entries()) {
      const face = node.faces[index];
      if (replaced.has(face) || node.faceFlags?.[face]?.visible === false) continue;
      for (const vertex of vertices) positions.push(...node.vertices[vertex]);
      if (!faceTriangles.has(face)) faceTriangles.set(face, []);
      faceTriangles.get(face).push(faceIds.length);
      faceIds.push(face);
      indices.push(index);
    }
    if (!faceIds.length && !replaced.size) return;
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(positions, 3));
    geometry.setAttribute("color", new Float32BufferAttribute(new Float32Array(positions.length), 3));
    geometry.setAttribute("fabricMask", new Float32BufferAttribute(new Float32Array(faceIds.length * 3), 1));
    geometry.computeVertexNormals();
    geometry.computeBoundingBox();
    const mesh = new Mesh(geometry, withFabricWeave(new MeshStandardMaterial({
      vertexColors: true, side: DoubleSide, roughness: 1,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    }), true));
    mesh.userData.nodeId = node.id;
    const edgeLines = sketchupEdges(node);
    if (edgeLines) mesh.add(edgeLines);
    objects.add(mesh);
    items.set(node.id, { node, mesh, edgeLines, bounds: geometry.boundingBox, faceIds, faceTriangles, indices });
    for (const faceId of faceTriangles.keys()) paintFace({ nodeId: node.id, faceId }, false);
  }

  function sketchupEdges(node) {
    const positions = [];
    for (const [index, [start, end]] of (node.edges || []).entries()) {
      const flags = node.edgeFlags?.[index];
      if (flags?.visible === false || flags?.soft) continue;
      positions.push(...node.vertices[start], ...node.vertices[end]);
    }
    if (!positions.length) return null;
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(positions, 3));
    const lines = new LineSegments(geometry, new LineBasicMaterial({ color: "#39434c", depthWrite: false }));
    lines.renderOrder = 1;
    return lines;
  }

  function paintFace({ nodeId, faceId }, update = true) {
    const item = items.get(nodeId);
    const positions = item?.faceTriangles.get(faceId);
    if (!positions) return;
    const fabric = fabrics.get(nodeId)?.get(faceId);
    const color = fabric ? new Color(fabric.color) : neutral.clone();
    if (selection.get(nodeId)?.has(faceId)) color.lerp(accent, 0.83).multiplyScalar(0.86);
    else if (hoveredFace?.nodeId === nodeId && hoveredFace.faceId === faceId) color.lerp(hoverTint, 0.52);
    const { color: attribute, fabricMask } = item.mesh.geometry.attributes;
    for (const index of positions) {
      for (let offset = 0; offset < 3; offset++) {
        attribute.setXYZ(3 * index + offset, color.r, color.g, color.b);
        fabricMask.setX(3 * index + offset, fabric ? 1 : 0);
      }
    }
    if (update) {
      const first = positions[0], last = positions.at(-1);
      attribute.addUpdateRange(9 * first, 9 * (last - first + 1));
      attribute.needsUpdate = true;
      fabricMask.addUpdateRange(3 * first, 3 * (last - first + 1));
      fabricMask.needsUpdate = true;
    }
  }

  function renderedPath() {
    path.replaceChildren();
    const sceneLink = doc.createElement("button");
    sceneLink.type = "button";
    sceneLink.className = "button";
    sceneLink.textContent = "Сцена";
    sceneLink.onclick = () => openGroup(null);
    path.append(sceneLink);
    for (const node of hierarchy.breadcrumbs(scope)) {
      const link = doc.createElement("button");
      link.type = "button";
      link.className = "button";
      link.textContent = attachment.state?.componentNames?.[node.id] || node.name;
      link.onclick = () => openGroup(node.id);
      path.append(link);
    }
    path.lastElementChild.setAttribute("aria-current", "location");
    path.scrollLeft = path.scrollWidth;
  }

  function nodeVisible(id) {
    return (!scope || hierarchy.contains(scope, id)) && !hierarchy.breadcrumbs(id).some(node => hiddenNodes.has(node.id));
  }

  function updateVisibility() {
    clearHover();
    objects.visible = !fabricOnly;
    for (const [id, item] of items) item.mesh.visible = nodeVisible(id);
    renderAssignedFabrics();
    updatePanels();
  }

  function openGroup(id) {
    if (pending || scope === id) return;
    cancelDraft();
    planeClick = null;
    cancelClothCut();
    drafting.cancel();
    scope = id;
    isolatedFabric = null;
    selectFaces(new Map());
    selectedObject = id;
    updateVisibility();
    renderedPath();
    updateControls();
    fit();
    notify(toolHint());
  }

  function setFabricView(value) {
    fabricOnly = value;
    if (!value) {
      isolatedFabric = null;
      if (mode === "sew") {
        mode = "select";
        setPairTarget(null);
        stitchPanels = [];
        patternTools.setStitchPanels?.([]);
        updateMouseMode();
      }
      patternDetails.hidden = true;
    }
    updateVisibility();
    updateControls();
  }

  function isolateFabric(id) {
    isolatedFabric = id;
    if (id && !fabricOnly) setFabricView(true);
    else updateVisibility();
    updateControls();
    fit(true);
  }

  function selectionOperation(event) {
    if (event.altKey) return "subtract";
    if (mode === "box" && event.shiftKey) return "toggle";
    return event.shiftKey || event.ctrlKey || event.metaKey ? "add" : "replace";
  }

  function intersection(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    screenPoint.set((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2);
    ray.setFromCamera(screenPoint, camera);
    return !fabricOnly && ray.intersectObjects(objects.children.filter(mesh => mesh.visible), false)[0] || null;
  }

  function fabricAt(hit) {
    if (!placementMeshes.size && !legacyMeshes.size) return null;
    const target = ray.intersectObjects([...placementMeshes.values(), ...legacyMeshes.values()], false)[0];
    return target && (!hit || target.distance <= hit.distance + 0.05) ? target : null;
  }

  function fabricIdAt(hit, target = fabricAt(hit)) {
    if (target) return target.object.userData.panelId || target.object.userData.fabricId;
    if (!hit) return null;
    const id = hit.object.userData.nodeId;
    const face = items.get(id)?.faceIds[hit.faceIndex];
    const fabric = fabrics.get(id)?.get(face);
    return fabric ? legacyFabricId(id, fabric) : null;
  }

  function setHover(face, panelId = null) {
    const oldFace = hoveredFace;
    const oldPanel = hoveredPanel;
    if (oldFace?.nodeId === face?.nodeId && oldFace?.faceId === face?.faceId && oldPanel === panelId) return false;
    hoveredFace = face;
    hoveredPanel = panelId;
    if (oldFace) paintFace(oldFace);
    if (face) paintFace(face);
    if (oldPanel) paintPlacement(placementMeshes.get(oldPanel) || legacyMeshes.get(oldPanel));
    if (panelId) paintPlacement(placementMeshes.get(panelId) || legacyMeshes.get(panelId));
    return true;
  }

  function clearHover() {
    cancelAnimationFrame(hoverFrame);
    hoverFrame = 0;
    const hadPoint = Boolean(hoverPoint || cutHover || seamHover || eraseHover);
    hoverPoint = cutHover = null;
    seamHover = null;
    eraseHover = null;
    if (notchHoverFeedback) {
      notchHoverFeedback = false;
      patternTools.setNotchStatus?.(null);
    }
    const draftingChanged = drafting.clearHover();
    if (setHover(null) || hadPoint || draftingChanged) render();
  }

  function queueHover(event) {
    const sewing = mode === "sew" || mode === "notch";
    if (pending || !items.size || (!draft && !faceTools.has(mode) && !draftingTools.has(mode) && !cutTools.has(mode) && !sewing && mode !== "erase")) return;
    cancelAnimationFrame(hoverFrame);
    hoverFrame = requestAnimationFrame(() => {
      hoverFrame = 0;
      if (!visible || pending) return;
      const hit = intersection(event);
      if (mode === "erase") {
        const before = eraseHover;
        eraseHover = findEraseTarget(event, hit);
        setHover(null, eraseHover?.type === "fabric" ? eraseHover.id : null);
        renderer.domElement.style.cursor = eraseHover ? "pointer" : "";
        if (before?.type !== eraseHover?.type || before?.id !== eraseHover?.id) {
          notify(eraseHover?.label || toolHint());
        }
        render();
        return;
      }
      if (draftingTools.has(mode)) {
        drafting.hover(mode, event, draftingContext(hit));
        render();
        return;
      }
      if (cutTools.has(mode)) {
        cutHover = clothCutSample(event, hit);
        render();
        return;
      }
      if (sewing) {
        const fabric = fabricAt(hit);
        const id = fabric?.object.userData.panelId;
        if (pairTarget || mode === "notch") {
          seamHover = nearestSeamEdge(event, (fabric || hit)?.point, pairTarget || "notch", mode === "notch" ? id : null);
          if (mode === "notch" && (seamHover || notchHoverFeedback)) {
            notchHoverFeedback = Boolean(seamHover);
            patternTools.setNotchStatus?.(seamHover ? { edge: seamHover.join, fraction: seamHover.fraction } : null);
          }
        } else setHover(null, id);
        renderer.domElement.style.cursor = seamHover || id && !pairTarget ? "pointer" : "";
        render();
        return;
      }
      renderer.domElement.style.cursor = contourHandle(event, hit) >= 0 ? "grab" : "";
      const nodeId = hit?.object.userData.nodeId;
      if (draft && !draft.closed) hoverPoint = contourSample(event, hit)?.point || null;
      const panelId = mode === "select" ? fabricIdAt(hit) : null;
      if (panelId) renderer.domElement.style.cursor = "pointer";
      const face = !panelId && hit && faceTools.has(mode) && (!draft || onDrawingSurface(nodeId))
        ? { nodeId, faceId: items.get(nodeId).faceIds[hit.faceIndex] } : null;
      if (setHover(face, panelId) || draft && !draft.closed) render();
    });
  }

  function pick(event) {
    const hit = intersection(event);
    if (mode === "erase") {
      if (event.detail < 2) eraseAt(event, hit);
      return;
    }
    if (cutTools.has(mode)) return pickClothCut(event, hit);
    if (draftingTools.has(mode)) {
      const result = drafting.click(mode, event, draftingContext(hit));
      if (result?.edge) {
        void addModelEdge(result);
        return;
      }
      if (result?.mark) {
        selectedMark = result.mark.id;
        commitState({ ...state(), drafting: [...(state().drafting || []), result.mark] }, false);
      }
      if (result?.message) notify(result.message, result.error);
      else if (result?.mark) notify(toolHint());
      updateControls();
      render();
      return;
    }
    if (mode === "notch") return addNotchAt(event, fabricAt(hit));
    if (mode === "box") return pickPlaneFace(event, hit);
    if (mode === "sew") {
      const fabric = fabricAt(hit);
      if (pairTarget) return pickJoin(event, fabric?.point);
      if (!fabric) return;
      const id = fabric.object.userData.panelId;
      if (!id) return notify("Для шва нужна деталь с лекалом.", true);
      chooseStitchPanel(id, fabric.point);
      return;
    }
    if (fabricOnly && mode === "select" && event.shiftKey) {
      const id = fabricAt(hit)?.object.userData.panelId;
      if (id) selectContextCloth(id);
      return;
    }
    if (mode === "select" && event.detail === 1 && selectionOperation(event) === "replace") {
      const mark = drafting.hitMark(event, viewport.getBoundingClientRect());
      const edge = modelEdgeAt(event, hit);
      if (mark || edge) {
        selectedMark = mark || edge.id;
        render();
        return;
      }
      selectedMark = null;
      const target = fabricAt(hit);
      const id = fabricIdAt(hit, target);
      if (id) return chooseFabric(id, (target || hit).point);
      clearFabricSelection();
    }
    if (contourTools.has(mode)) {
      if (draft && !draft.closed && draft.points.length >= 3 && contourHandle(event, hit, true) === 0) return finishDraft();
      return pickDrawing(hit, event);
    }
    if (!hit) {
      if (mode === "select") selectFaces(new Map());
      return;
    }
    const id = hit.object.userData.nodeId;
    const target = hierarchy.pick(scope, items.get(id).node.editorSourceNode || id);
    if (event.detail === 2 && mode === "select" && target?.kind === "object") return openGroup(target.nodeId);
    if (mode === "select" && event.detail === 1) {
      selectedObject = target?.nodeId || id;
      updateControls();
      render();
    }
  }

  function pickPlaneFace(event, hit) {
    const operation = event.altKey ? "subtract" : event.ctrlKey || event.metaKey ? "add" : "toggle";
    if (!hit) {
      planeClick = null;
      if (!event.altKey && !event.shiftKey && !event.ctrlKey && !event.metaKey) selectFaces(new Map());
      return;
    }
    const id = hit.object.userData.nodeId;
    const item = items.get(id);
    const face = item.faceIds[hit.faceIndex];
    const same = planeClick?.id === id && planeClick.face === face && planeClick.operation === operation;
    if (event.detail > 3 || event.detail === 2 && same) return;
    if (event.detail === 3 && same) {
      const zone = surfaceZoneFaces(item, item.indices[hit.faceIndex]);
      const { base } = planeClick;
      planeClick = null;
      selectFaces(new Map([[id, zone.size ? zone : new Set([face])]]), operation, base);
      return;
    }
    planeClick = { id, face, operation, base: selection };
    selectFaces(new Map([[id, new Set([face])]]), operation);
  }

  function draftingContext(hit) {
    const target = fabricOnly ? fabricAt(hit) : hit;
    const data = target?.object.userData;
    const panel = data?.panelId && state().panels?.find(panel => panel.id === data.panelId);
    const layer = panel && state().fabricLayers?.find(layer => "fabric:" + layer.id === panel.nodeId);
    const nodeId = data?.nodeId || (layer?.surfaces.length === 1 ? layer.surfaces[0].nodeId : null);
    const item = hit && items.get(hit.object.userData.nodeId);
    const surface = item && hit.face && {
      nodeId: item.node.editorSourceNode || item.node.id,
      faceId: item.node.editorSourceFace || item.faceIds[hit.faceIndex],
      snapFaceId: item.faceIds[hit.faceIndex],
      point: hit.point,
      normal: hit.face.normal,
    };
    return { hit: target, nodeId, surface, ray: ray.ray, rect: viewport.getBoundingClientRect() };
  }

  function selectContextCloth(id) {
    if (!id || stitchPanels.includes(id)) return;
    if (stitchPanels.length === 2) stitchPanels = [];
    if (!stitchPanels.length && selectedFabric && selectedFabric !== id &&
        state().panels?.some(panel => panel.id === selectedFabric)) stitchPanels = [selectedFabric];
    stitchPanels.push(id);
    selectedFabric = id;
    clothPick = null;
    focusPanel(id);
    renderAssignedFabrics();
    notify(stitchPanels.length === 2 ? "Выбраны две ткани. Правый щелчок открывает команды шва." : "Выберите вторую ткань с Shift.");
  }

  function startStitch(id) {
    const selected = [...stitchPanels];
    chooseTool("sew");
    stitchPanels = [];
    for (const panelId of [...new Set([...selected, id].filter(Boolean))].slice(0, 2)) chooseStitchPanel(panelId);
  }

  function closeContextMenu(focus = false) {
    if (contextMenu.hidden) return;
    contextMenu.hidden = true;
    if (focus) renderer?.domElement.focus({ preventScroll: true });
  }

  function openContextMenu(event) {
    if (pending || patternTools.busy() || !hierarchy) return;
    const hit = intersection(event);
    const fabric = fabricAt(hit);
    const panelId = fabric?.object.userData.panelId;
    const mark = drafting.hitMark(event, viewport.getBoundingClientRect());
    const edge = modelEdgeAt(event, hit);
    const commands = [];
    const add = (name, symbol, run) => commands.push({ name, symbol, run });
    const sewn = state().panels || [];
    if (draft && !draft.closed && draft.points.length >= 3) add("Замкнуть контур", "polygon", finishDraft);
    if (clothCutReady()) add(mode === "split" ? "Применить шов" : "Сделать надрез", mode === "split" ? "polyline" : "slit", finishClothCut);
    if (panelId && !draft && !cutDraft) {
      add("Разделить швом", "polyline", () => startClothCut("split", panelId, event));
      add("Надрез от кромки", "slit", () => startClothCut("slash", panelId, event));
    }
    if ((panelId || stitchPanels.length === 2) && sewn.length >= 2) add("Шов между деталями", "join", () => startStitch(panelId));
    if (panelId || fabricOnly && sewn.length) {
      add("Поставить насечку", "notch", () => {
        chooseTool("notch");
        if (nearestSeamEdge(event, fabric?.point, "notch", panelId)) addNotchAt(event, fabric);
      });
    }
    if (panelId && !draft) {
      add("Изолировать ткань", "select", () => { chooseFabric(panelId); isolateFabric(panelId); });
    }
    if (hit && !draft && !fabricOnly) {
      add("Назначить ткань", "rectangle", () => {
        if (!selection.size) {
          const id = hit.object.userData.nodeId;
          selectFaces(new Map([[id, new Set([items.get(id).faceIds[hit.faceIndex]])]]));
        }
        openFabricDialog();
      });
    }
    if (mark) add("Удалить линию или направляющую", "erase", () => removeDraftMark(mark));
    if (edge) add("Удалить ребро модели", "erase", () => removeDraftMark(edge.id));
    add("Линейка", "measure", () => chooseTool("measure"));
    add("Отрезок", "line", () => chooseTool("line"));
    add("Направляющая", "reference", () => chooseTool("guide"));
    contextMenu.replaceChildren();
    for (const { name, symbol, run } of commands) {
      const button = doc.createElement("button");
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.innerHTML = icon(symbol);
      button.append(doc.createTextNode(name));
      button.onclick = () => { closeContextMenu(true); run(); };
      contextMenu.append(button);
    }
    contextMenu.hidden = false;
    const bounds = viewport.getBoundingClientRect();
    contextMenu.style.left = Math.max(4, Math.min(event.clientX - bounds.left, bounds.width - contextMenu.offsetWidth - 4)) + "px";
    contextMenu.style.top = Math.max(4, Math.min(event.clientY - bounds.top, bounds.height - contextMenu.offsetHeight - 4)) + "px";
    contextMenu.firstElementChild?.focus({ preventScroll: true });
  }

  function chooseStitchPanel(id, point) {
    if (stitchPanels.length === 2) {
      stitchPanels = [];
      setPairTarget(null);
    }
    if (stitchPanels.includes(id)) return notify("Выберите вторую ткань.", true);
    stitchPanels.push(id);
    selectedFabric = id;
    clothPick = point ? { id, point } : null;
    focusPanel(id);
    renderAssignedFabrics();
    if (stitchPanels.length === 2) patternTools.beginPairPanels(...stitchPanels);
    else {
      patternTools.setStitchPanels?.(stitchPanels);
      notify("Сторона А выбрана. Укажите ткань для стороны Б.");
    }
  }

  function setPairTarget(side) {
    if (pairTarget !== side) seamHover = null;
    pairTarget = side;
    patternTools.setPickTarget?.(side);
    paintClothSelection();
  }

  function selectFaces(found, operation = "replace", base = selection) {
    const prior = selection;
    if (operation === "toggle") {
      const selected = [...found].every(([id, faces]) => [...faces].every(face => base.get(id)?.has(face)));
      operation = selected ? "subtract" : "add";
    }
    selection = operation === "replace" ? new Map() : new Map([...base].map(([id, faces]) => [id, new Set(faces)]));
    for (const [id, additions] of found) {
      const faces = selection.get(id) || new Set();
      for (const face of additions) {
        if (operation === "subtract") faces.delete(face);
        else faces.add(face);
      }
      if (faces.size) selection.set(id, faces);
      else selection.delete(id);
    }
    for (const map of [prior, selection]) for (const [nodeId, faces] of map) {
      for (const faceId of faces) paintFace({ nodeId, faceId });
    }
    const id = selection.keys().next().value;
    const sourceId = items.get(id)?.node.editorSourceNode || id;
    selectedObject = sourceId ? hierarchy.pick(scope || hierarchy.rootId, sourceId)?.nodeId || sourceId : null;
    const assignments = [...selection].flatMap(([id, faces]) => [...faces].map(face => fabrics.get(id)?.get(face)));
    const first = assignments[0];
    const fabric = first && assignments.every(item => item?.name === first.name && item.color === first.color) ? first : null;
    if (fabric || operation === "replace") nameField.value = fabric?.name || "";
    if (fabric) form.elements.color.value = fabric.color;
    customColor = false;
    nameField.setCustomValidity("");

    updateControls();
    render();
  }

  function updateControls() {
    const count = [...selection.values()].reduce((total, set) => total + set.size, 0);
    const ready = draft?.closed && draft.seed && !contourDrag;
    const assign = actions["name-fabric"];
    assign.textContent = draft ? (draft.closed && !ready ? "Подготавливаем ткань…" : "Уложить ткань") :
      "Назначить ткань" + (count > 1 ? " · " + count : "");
    assign.hidden = draft ? !draft.closed : !count || fabricOnly && mode === "sew";
    assign.disabled = pending || Boolean(draft && !ready);
    form.querySelector('[type="submit"]').disabled = assign.disabled;
    actions["cloth-view"].disabled = pending || !hierarchy || Boolean(draft || cutDraft?.path.length);
    actions["cloth-view"].setAttribute("aria-pressed", String(fabricOnly));
    actions["hide-object"].hidden = fabricOnly || !selectedObject || selectedObject === hierarchy?.rootId || Boolean(draft);
    actions.restore.hidden = !isolatedFabric && !hiddenNodes.size;
    actions["isolate-fabric"].hidden = !selectedFabric;
    actions["remove-fabric"].hidden = !selectedFabric;
    actions["edit-contour"].hidden = Boolean(draft) || mode === "sew" || cutTools.has(mode) ||
      !state().seams?.some(seam => seam.closed && seam.placementFor === selectedFabric);
    lineActions.hidden = !draft && !cutDraft?.path.length;
    actions.finish.textContent = cutDraft ? mode === "split" ? "Применить шов" : "Сделать надрез" : "Замкнуть";
    actions.finish.disabled = pending || (cutDraft ? !clothCutReady() : Boolean(draft?.closed || draft?.arcMid) || (draft?.points.length || 0) < 3);
    actions.undo.textContent = draft?.closed ? "Разомкнуть" : "Убрать точку";
    actions.undo.disabled = pending || (!draft && !cutDraft?.path.length);
    updateHistoryControls();
    for (const button of tools) {
      button.setAttribute("aria-pressed", String(button.dataset.tool === mode));
      button.disabled = !hierarchy || pending ||
        (button.dataset.tool === "sew" && (state().panels || []).length < 2) ||
        ((button.dataset.tool === "notch" || cutTools.has(button.dataset.tool)) && !(state().panels || []).length);
    }
  }

  function nearestSeamEdge(event, hitPoint, purpose, panelId = null) {
    if (!panelId && (purpose === "a" || purpose === "b")) panelId = patternTools.currentPickPanel?.(purpose);
    const candidates = purpose ? patternTools.candidatesFor(purpose) : geometryResult?.joins || [];
    return nearestEdgeAt(event, hitPoint, candidates.filter(candidate =>
      (!panelId || candidate.panelId === panelId) &&
      (purpose ? placementMeshes.has(candidate.panelId) : isLayerVisible(candidate.nodeId))),
    event.pointerType === "touch" ? 22 : 14);
  }

  function nearestEdgeAt(event, hitPoint, candidates, radius) {
    const rect = viewport.getBoundingClientRect();
    const cursor = new Vector2(event.clientX, event.clientY);
    let best;
    for (const join of candidates) {
      const points = join.points.map(point => new Vector3(...point));
      const length = points.slice(1).reduce((sum, point, i) => sum + point.distanceTo(points[i]), 0);
      let traveled = 0;
      for (let i = 0; i < points.length - 1; i++) {
        const a = points[i], b = points[i + 1];
        const line = b.clone().sub(a);
        const size = line.length();
        if (!size) continue;
        const from = a.clone().project(camera), to = b.clone().project(camera);
        const start = new Vector2(rect.left + (1 + from.x) * rect.width / 2, rect.top + (1 - from.y) * rect.height / 2);
        const segment = new Vector2((to.x - from.x) * rect.width / 2, (from.y - to.y) * rect.height / 2);
        const t = Math.max(0, Math.min(1, cursor.clone().sub(start).dot(segment) / (segment.lengthSq() || 1)));
        const pixels = cursor.distanceTo(start.addScaledVector(segment, t));
        const depthA = -a.clone().applyMatrix4(camera.matrixWorldInverse).z;
        const depthB = -b.clone().applyMatrix4(camera.matrixWorldInverse).z;
        if (depthA < camera.near || depthB < camera.near) { traveled += size; continue; }
        const fraction = (t / depthB) / ((1 - t) / depthA + t / depthB);
        const point = a.clone().addScaledVector(line, fraction);
        const worldTolerance = point.distanceTo(camera.position) * Math.tan(camera.fov * Math.PI / 360) * radius * 2.5 / rect.height;
        if (pixels < (best?.pixels ?? radius) && (!hitPoint || point.distanceTo(hitPoint) < worldTolerance)) {
          best = { join, fraction: (traveled + size * fraction) / length, pixels, point };
        }
        traveled += size;
      }
    }
    return best;
  }

  function modelEdgeAt(event, hit) {
    if (!hit || fabricOnly || !state().modelEdges?.length) return null;
    const node = items.get(hit.object.userData.nodeId)?.node;
    if (!node) return null;
    const nodeId = node.editorSourceNode || node.id;
    const candidates = state().modelEdges.filter(edge => edge.nodeId === nodeId);
    return nearestEdgeAt(event, hit.point, candidates, event.pointerType === "touch" ? 13 : 7)?.join || null;
  }

  function* notchesOnModel() {
    for (const panel of geometryResult?.panels || []) {
      if (!placementMeshes.has(panel.id) || !panel.marks?.length) continue;
      const edges = new Map(panel.edges.map(edge => [edge.id, edge]));
      for (const mark of panel.marks) {
        const edge = edges.get(mark.edgeId);
        if (!edge?.sourcePoints?.length) continue;
        const [a, b] = edge.points;
        const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (!length) continue;
        const fraction = Math.hypot(mark.point[0] - a[0], mark.point[1] - a[1]) / length;
        const [from, to] = edge.sourcePoints.map(point => new Vector3(...point));
        const point = from.clone().lerp(to, Math.max(0, Math.min(1, fraction)));
        const tangentEnd = point.clone().add(to.sub(from));
        yield { panelId: panel.id, mark, point, tangentEnd };
      }
    }
  }

  function notchAt(event, hitPoint) {
    const rect = viewport.getBoundingClientRect();
    let distance = event.pointerType === "touch" ? 16 : 9;
    let target;
    for (const item of notchesOnModel()) {
      const point = item.point.clone().project(camera);
      if (Math.abs(point.z) > 1) continue;
      const pixels = Math.hypot(event.clientX - rect.left - (1 + point.x) * rect.width / 2,
        event.clientY - rect.top - (1 - point.y) * rect.height / 2);
      const tolerance = item.point.distanceTo(camera.position) * Math.tan(camera.fov * Math.PI / 360) * 27 / rect.height;
      if (pixels >= distance || hitPoint && item.point.distanceTo(hitPoint) > tolerance) continue;
      distance = pixels;
      target = { ...item, id: item.mark.id, type: "notch", label: "Удалить насечку" };
    }
    return target;
  }

  function erasePaths() {
    if (erasePathCache) return erasePathCache;
    const assignments = state();
    const patterns = new Map((geometryResult?.panels || []).map(panel => [panel.id, panel]));
    const paths = [];
    for (const stitch of geometryResult?.fitStitches || []) {
      const type = assignments.pairs?.some(pair => pair.id === stitch.id) ? "pair" :
        assignments.joins?.some(join => join.id === stitch.id) ? "join" : null;
      if (!type) continue;
      for (const side of [stitch.a, stitch.b]) {
        if (!placementMeshes.has(side.panelId)) continue;
        const pattern = patterns.get(side.panelId);
        if (!pattern?.sourceVertices) continue;
        const points = side.vertices.map(index => new Vector3(...pattern.sourceVertices[index]));
        paths.push({ id: stitch.id, type, label: "Удалить соединительный шов", points: routeSlice(points, ...side.range) });
      }
    }
    for (const seam of geometryResult?.seams || []) {
      if (!seam.coverCut || !seam.panelIds.some(id => placementMeshes.has(id))) continue;
      paths.push({ id: seam.id, type: seam.kind === "slash" ? "slash" : "split", points: seam.points,
        label: seam.kind === "slash" ? "Удалить надрез" : "Разделительный шов образует границы лекал." });
    }
    erasePathCache = paths;
    return erasePathCache;
  }

  function findEraseTarget(event, hit) {
    const fabric = fabricAt(hit);
    const point = (fabric || hit)?.point;
    const markId = drafting.hitMark(event, viewport.getBoundingClientRect());
    if (markId) {
      const mark = state().drafting.find(item => item.id === markId);
      if (mark) return { id: markId, type: mark.kind, label: mark.kind === "guide" ? "Удалить направляющую" : "Удалить отрезок",
        points: [mark.from, mark.to] };
    }
    const edge = modelEdgeAt(event, hit);
    if (edge) return { ...edge, type: "model-edge", label: "Удалить ребро модели" };
    const notch = notchAt(event, point);
    if (notch) return notch;
    const cut = nearestEdgeAt(event, point, erasePaths(), event.pointerType === "touch" ? 13 : 7);
    if (cut) return cut.join;
    const id = fabricIdAt(hit, fabric);
    return id ? { id, type: "fabric", label: "Удалить ткань", point } : null;
  }

  function removeNotch({ panelId, mark }) {
    const current = state();
    if (mark.pairId || mark.joinId) {
      const paired = Boolean(mark.pairId);
      const field = paired ? "pairs" : "joins";
      const id = mark.pairId || mark.joinId;
      const record = current[field]?.find(item => item.id === id);
      const result = (paired ? geometryResult.pairs : geometryResult.confirmedJoins)?.find(item => item.id === id);
      const fraction = result?.marks.find(item => item.id === mark.id)?.fraction;
      const index = record?.marks?.indexOf(fraction) ?? -1;
      if (index < 0) return false;
      const next = { ...record, marks: record.marks.filter((_, i) => i !== index) };
      if (record.markIds) next.markIds = record.markIds.filter((_, i) => i !== index);
      if (paired && record.ease?.length) {
        next.ease = Array.from({ length: record.marks.length + 1 }, (_, i) => record.ease[i] || 0);
        next.ease.splice(index, 2, next.ease[index] + next.ease[index + 1]);
      }
      commitState({ ...current, [field]: current[field].map(item => item === record ? next : item) });
      return true;
    }
    const panel = current.panels?.find(item => item.id === panelId);
    if (!panel?.notches?.some(item => item.id === mark.id)) return false;
    const next = { ...panel, notches: panel.notches.filter(item => item.id !== mark.id) };
    commitState({ ...current, panels: current.panels.map(item => item === panel ? next : item) });
    return true;
  }

  function eraseAt(event, hit) {
    if (patternTools.busy()) return;
    const target = findEraseTarget(event, hit);
    if (!target) return notify("Укажите ткань или элемент разметки.");
    clearHover();
    const { id, type } = target;
    if (type === "fabric") return void removeFabric(id);
    if (type === "guide" || type === "line" || type === "model-edge") return removeDraftMark(id);
    if (type === "split") return notify("Для отмены разделения ткани используйте «Назад».", true);
    if (type === "notch") {
      if (removeNotch(target)) return notify("Насечка удалена.");
      return notify("Эта насечка создаётся разметкой шва.", true);
    }
    const field = type === "slash" ? "seams" : type === "join" ? "joins" : "pairs";
    commitState({ ...state(), [field]: state()[field].filter(item => item.id !== id) });
    notify(type === "slash" ? "Надрез удалён." : "Соединительный шов удалён.");
  }

  function addNotchAt(event, fabric) {
    if (patternTools.busy()) return;
    notchHoverFeedback = false;
    const id = fabric?.object.userData.panelId;
    const best = nearestSeamEdge(event, fabric?.point, "notch", id);
    if (!best) {
      const message = "Укажите точку на кромке ткани.";
      patternTools.setNotchStatus?.({ message, error: true });
      return notify(message, true);
    }
    const joined = nearestSeamEdge(event, fabric?.point, null);
    const declared = joined && (state().joins?.some(item => item.id === joined.join.id) ||
      state().seams?.some(item => item.kind === "seam" && item.coverCut &&
        geometryResult.seams?.some(seam => seam.id === item.id && seam.panelIds.length === 2 &&
          joined.join.panelIds.every(id => seam.panelIds.includes(id)))));
    const message = patternTools.addEdgeNotch(best.join, best.fraction,
      declared && joined.pixels <= best.pixels + 1 ? joined : null);
    const text = message || "Насечка добавлена.";
    patternTools.setNotchStatus?.({ edge: best.join, fraction: best.fraction, message: text, error: Boolean(message) });
    notify(text, Boolean(message));
  }

  function pickJoin(event, hitPoint) {
    const pickedPair = pairTarget;
    const best = nearestSeamEdge(event, hitPoint, pickedPair);
    if (!best) return notify("Щёлкните ближе к кромке детали.", true);
    if (pickedPair) {
      setPairTarget(null);
      patternTools.pickPairSide(pickedPair, best.join, best.fraction);
      if (stitchPanels.length === 2 && pickedPair === "a") {
        setPairTarget("b");
        notify("Укажите кромку Б на второй ткани.");
      } else {
        openPatterns(pickedPair === "mark" ? "notches" : pickedPair === "allowance" ? "transfer" : "seams");
        notify(pickedPair === "a" || pickedPair === "b" ? "Кромка выбрана. Задайте параметры и сохраните шов." : "Кромка выбрана.");
      }
    } else {
      patternTools.selectJoin(best.join.id, best.fraction);
    }
  }

  function openPatterns(section) {
    patternTools.show();
    if (section) {
      patternDetails.hidden = false;
      patternTools.openSection?.(section);
    }
  }

  function startClothCut(tool, id, event) {
    chooseTool(tool);
    chooseFabric(id);
    pickClothCut(event, intersection(event), true);
  }

  function clothCutSample(event, hit) {
    const picked = fabricAt(hit);
    const panelId = cutDraft?.panelId || picked?.object.userData.panelId || selectedFabric;
    const mesh = placementMeshes.get(panelId);
    if (!mesh || picked && picked.object !== mesh) return null;
    const { placement, folds } = mesh.userData;
    if (!placement) return null;
    const needsBoundary = !cutDraft?.path.length;
    const rect = viewport.getBoundingClientRect();
    if (!mesh.userData.cutSnap) {
      mesh.userData.cutSnap = magneticEdges(placement, new Set(folds.map(segment => worldEdgeKey(...segment.points))));
    }
    const depthHit = picked || hit;
    const edge = mesh.userData.cutSnap.pick(event, camera, rect, depthHit, undefined, true);
    const fold = !event.altKey && !needsBoundary &&
      mesh.userData.cutSnap.pick(event, camera, rect, depthHit);
    const snapped = needsBoundary || !event.altKey && edge && (!fold || edge.rank <= fold.rank + 1) ? edge : fold;
    if (!snapped && (needsBoundary || !picked)) return null;
    const index = snapped ? snapped.anchor.triangle : picked.faceIndex;
    const [a, b, c] = placement.triangles[index].map(vertex => new Vector3(...placement.vertices[vertex]));
    const normal = Triangle.getNormal(a, b, c, new Vector3());
    return { panelId, point: snapped ? snapped.point : picked.point, normal, boundary: snapped?.edge.count === 1,
      snap: Boolean(snapped), triangle: [a, b, c] };
  }

  function pickClothCut(event, hit, quiet = false) {
    if (!cutTools.has(mode) || patternTools.busy()) return;
    const sample = clothCutSample(event, hit);
    if (!sample) {
      if (!quiet) notify(cutDraft?.path.length ? "Продолжите линию по выбранной ткани." : "Выберите точку на краю ткани.", true);
      return;
    }
    if (!cutDraft) {
      const panel = state().panels.find(item => item.id === sample.panelId);
      const source = panel && workSurface(scope || hierarchy.rootId, panel.nodeId);
      if (!source?.recorded) return notify("У детали нет исходной поверхности для разметки.", true);
      cutDraft = { panelId: sample.panelId, source, path: [], forward: [],
        mesh: buildMesh(source.geometry, new MeshStandardMaterial({ side: DoubleSide })) };
    }
    if (cutDraft.path.some(item => item.point.distanceToSquared(sample.point) < .0025)) {
      return notify("Укажите другую точку на ткани.", true);
    }
    if (cutDraft.path.length >= 200) return notify("На линии можно указать до 200 точек.", true);
    const probe = new Raycaster(sample.point.clone().addScaledVector(sample.normal, .5), sample.normal.clone().negate(), 0, 1);
    let sourceHit = probe.intersectObject(cutDraft.mesh, false)[0];
    if (!sourceHit) {
      const center = sample.triangle.reduce((point, vertex) => point.addScaledVector(vertex, 1 / 3), new Vector3());
      probe.ray.origin.addScaledVector(center.sub(sample.point).normalize(), .005);
      sourceHit = probe.intersectObject(cutDraft.mesh, false)[0];
    }
    const anchor = sourceHit && sourceHit.point.distanceTo(sample.point) < .2 &&
      anchorInGeometry({ faceIndex: sourceHit.faceIndex, point: sample.point }, cutDraft.source.geometry);
    if (!anchor) return notify("Точка должна лежать на исходной поверхности ткани.", true);
    cutDraft.path.push({ ...sample, anchor: anchor.anchor });
    cutDraft.forward.length = 0;
    cutHover = null;
    notify(toolHint());
    updateControls();
    render();
  }

  function clothCutReady() {
    return cutDraft?.path.length >= 2 && cutDraft.path[0].boundary &&
      cutDraft.path.at(-1).boundary === (mode === "split");
  }

  function cancelClothCut() {
    if (cutDraft) disposeMesh(cutDraft.mesh);
    cutDraft = cutHover = null;
  }

  function changeCutPoint(forward) {
    if (!cutDraft) return false;
    const [from, to] = forward ? [cutDraft.forward, cutDraft.path] : [cutDraft.path, cutDraft.forward];
    if (!from.length) return false;
    to.push(from.pop());
    cutHover = null;
    updateControls();
    render();
    return true;
  }

  function finishClothCut() {
    if (!clothCutReady() || pending || patternTools.busy()) return;
    const target = cutDraft, kind = cutTools.get(mode);
    return load(async () => {
      const result = await post("edit-cut", { ...attachment, panelId: target.panelId, kind,
        anchors: target.path.map(item => item.anchor) });
      if (signal.aborted) return;
      cancelClothCut();
      if (kind === "seam" && isolatedFabric === target.panelId) isolatedFabric = null;
      selectedFabric = target.panelId;
      commitState(result.state, false);
      patternTools.refresh(result.calculated);
      patternTools.selectPanel(selectedFabric);
      notify(kind === "seam" ? "Шов разделил ткань на два лекала." : "Надрез добавлен в лекало.");
    }, kind === "seam" ? "Разделяем ткань по шву…" : "Проверяем надрез…");
  }

  function commitState(next, refresh = true) {
    const before = state();
    if (before.panels !== next.panels && next.panels) {
      clothPick = null;
      let number = Math.max(before.nextPanelNumber || 1, ...next.panels.map(panel => (Number(panel.number?.slice(1)) || 0) + 1));
      next = { ...next, panels: next.panels.map(panel => panel.number ? panel : {
        ...panel, number: "Д" + String(number++).padStart(2, "0"),
      }) };
      next.nextPanelNumber = number;
    }
    attachment = { ...attachment, state: next };
    onChange(attachment);
    if (refresh) patternTools.refresh();
    renderAssignedFabrics();
    updateControls();
  }

  function fabricNames() {
    const names = new Map();
    for (const assignments of fabrics.values()) {
      for (const fabric of assignments.values()) names.set(keyFor(fabric.name), fabric);
    }
    for (const panel of state().panels || []) if (panel.fabricName && panel.color && !names.has(keyFor(panel.fabricName))) {
      names.set(keyFor(panel.fabricName), { name: panel.fabricName, color: panel.color });
    }
    return names;
  }

  function legacyFabricId(nodeId, { name, color }) {
    return "surface:" + JSON.stringify([nodeId, name, color]);
  }

  function clothEntries() {
    const entries = (state().panels || []).map(panel => ({
      id: panel.id, panel, name: panel.fabricName || panel.name || "Ткань", color: panel.color || fabricColors[0],
    }));
    for (const [nodeId, assignments] of fabrics) {
      const byFabric = new Map();
      for (const [faceId, fabric] of assignments) {
        const id = legacyFabricId(nodeId, fabric);
        if (!byFabric.has(id)) byFabric.set(id, { id, nodeId, name: fabric.name, color: fabric.color, faces: new Set() });
        if (items.get(nodeId)?.faceTriangles.has(faceId)) byFabric.get(id).faces.add(faceId);
      }
      for (const entry of byFabric.values()) if (entry.faces.size) entries.push(entry);
    }
    return entries;
  }

  function chooseFabric(id, point) {
    const entry = clothEntries().find(item => item.id === id);
    if (!entry) return;
    if (mode === "sew" && entry.panel) return chooseStitchPanel(id, point);
    if (draft) {
      nameField.value = entry.name;
      form.elements.color.value = entry.color;
      customColor = false;
      colorDraftPreview();
      return;
    }
    if (mode === "select") stitchPanels = [];
    const old = selectedFabric;
    selectedFabric = id;
    clothPick = point ? { id, point } : null;
    if (isolatedFabric && isolatedFabric !== id) isolateFabric(null);
    if (entry.panel) {
      selectFaces(new Map());
      patternTools.selectPanel(id);
    } else if (nodeVisible(entry.nodeId)) {
      selectFaces(new Map([[entry.nodeId, entry.faces]]));
    }
    if (entry.panel) focusPanel(id);
    if (!fabricOnly && (old?.startsWith("surface:") || !entry.panel)) updatePanels();
    paintClothSelection();
    renderAssignedFabrics();
    updateControls();
    if (mode === "select") notify("Выбрана ткань · " + [entry.panel?.number, entry.name].filter(Boolean).join(" · "));
    render();
  }

  function clearFabricSelection() {
    if (!selectedFabric && !stitchPanels.length) return;
    const old = selectedFabric;
    selectedFabric = clothPick = null;
    stitchPanels = [];
    patternTools.selectPanel(null);
    if (!fabricOnly && old?.startsWith("surface:")) updatePanels();
    paintClothSelection();
    renderAssignedFabrics();
    updateControls();
    notify(toolHint());
    render();
  }

  function clothRole(id) {
    if (mode === "sew") {
      const side = pairHighlight.find(edge => edge?.panelId === id)?.side;
      if (side) return side === "Б" ? "b" : "a";
    }
    if ((mode === "sew" || mode === "select") && stitchPanels.includes(id)) return stitchPanels[0] === id ? "a" : "b";
    return selectedFabric === id ? "selected" : "";
  }

  function renderAssignedFabrics() {
    const focused = doc.activeElement?.dataset.fabricId;
    assignedList.replaceChildren();
    const query = keyFor(search.value.trim());
    let activeRow;
    for (const { id, panel, name, color } of clothEntries()) {
      if (query && !keyFor([name, panel?.name, panel?.number].filter(Boolean).join(" ")).includes(query)) continue;
      const row = doc.createElement("button");
      row.type = "button";
      row.className = "button surface-fabric-row";
      row.dataset.fabricId = id;
      const role = clothRole(id);
      row.dataset.fabricRole = role;
      row.setAttribute("aria-pressed", String(Boolean(role)));
      const swatch = doc.createElement("span");
      swatch.className = "surface-fabric-swatch";
      swatch.style.backgroundColor = color;
      const label = doc.createElement("span");
      label.className = "surface-fabric-name";
      label.textContent = panel?.number ? panel.number + " · " + name : name;
      row.append(swatch, label);
      if (role) {
        const marker = doc.createElement("span");
        marker.className = "surface-fabric-marker";
        marker.textContent = role === "a" ? "А" : role === "b" ? "Б" : "Выбрана";
        marker.setAttribute("aria-hidden", "true");
        if (role === "a" || role === "b") row.setAttribute("aria-description", "Сторона " + marker.textContent + " шва");
        row.append(marker);
      }
      row.title = label.textContent;
      row.onclick = () => chooseFabric(id);
      assignedList.append(row);
      if (id === selectedFabric) activeRow = row;
      if (id === focused) row.focus({ preventScroll: true });
    }
    if (!assignedList.childElementCount) {
      const empty = doc.createElement("p");
      empty.textContent = query ? "Совпадений нет" : "Назначьте ткань на модели.";
      assignedList.append(empty);
    }
    if (activeRow) {
      const bounds = assignedList.getBoundingClientRect();
      const row = activeRow.getBoundingClientRect();
      if (row.bottom > bounds.bottom) assignedList.scrollTop += row.bottom - bounds.bottom;
      else if (row.top < bounds.top) assignedList.scrollTop -= bounds.top - row.top;
    }
  }

  function renderFabricChoices() {
    choices.replaceChildren();
    for (const { name } of fabricNames().values()) {
      const option = doc.createElement("option");
      option.value = name;
      choices.append(option);
    }
  }

  function chosenFabric() {
    const name = nameField.value.trim();
    if (!name || /[\u0000-\u001f]/.test(name)) {
      nameField.setCustomValidity("Введите название ткани.");
      nameField.reportValidity();
      return null;
    }
    const names = fabricNames();
    const existing = names.get(keyFor(name));
    return { name: existing?.name || name, color: customColor ? form.elements.color.value : existing?.color || nextFabricColor(names) };
  }

  function nextFabricColor(names) {
    const used = new Set([...names.values()].map(fabric => fabric.color));
    return fabricColors.find(color => !used.has(color)) ||
      "#" + new Color().setHSL((used.size * 0.381966) % 1, 0.5, 0.42).getHexString();
  }

  function saveSelection(fabric) {
    const count = [...selection.values()].reduce((total, faces) => total + faces.size, 0);
    for (const [nodeId, selected] of selection) {
      if (!fabrics.has(nodeId)) fabrics.set(nodeId, new Map());
      const faces = fabrics.get(nodeId);
      for (const faceId of selected) {
        if (fabric) faces.set(faceId, { nodeId, faceId, ...fabric });
        else faces.delete(faceId);
      }
    }
    const surfaceFabrics = [...fabrics.values()].flatMap(assignments => [...assignments.values()]);
    commitState({ ...state(), surfaceFabrics }, false);
    renderAssignedFabrics();
    renderFabricChoices();
    selectFaces(new Map());
    notify((fabric ? "Ткань назначена · " + fabric.name : "Ткань снята") + " · граней " + count);
  }

  form.onsubmit = event => {
    event.preventDefault();
    if (pending || patternTools.busy() || (draft && (!draft.closed || !draft.seed || contourDrag))) return;
    const fabric = chosenFabric();
    if (!fabric) return;
    if (draft?.closed && draft.seed) void savePart(fabric, draft);
    else if (selection.size) void savePart(fabric);
  };
  nameField.oninput = () => {
    nameField.setCustomValidity("");
    customColor = false;
    const names = fabricNames();
    const known = names.get(keyFor(nameField.value.trim()));
    form.elements.color.value = known?.color || nextFabricColor(names);
    if (draft) colorDraftPreview();
  };
  form.elements.color.oninput = () => { customColor = true; if (draft) colorDraftPreview(); };
  function openFabricDialog() {
    if (pending || fabricDialog.open || actions["name-fabric"].hidden || actions["name-fabric"].disabled) return;
    fabricMessage.textContent = "";
    fabricDialog.showModal();
    nameField.focus();
  }
  actions["name-fabric"].onclick = openFabricDialog;
  actions["cancel-fabric"].onclick = () => { if (!pending) fabricDialog.close(); };
  fabricDialog.addEventListener("cancel", event => { if (pending) event.preventDefault(); }, { signal });
  fitButton.onclick = () => fit(true);
  search.oninput = renderAssignedFabrics;
  actions["cloth-view"].onclick = () => {
    if (!fabricOnly && mode !== "select") chooseTool("select");
    setFabricView(!fabricOnly);
  };
  actions["edit-contour"].onclick = () => editContour(selectedFabric);
  actions["isolate-fabric"].onclick = () => {
    if (!selectedFabric) return;
    if (!placementMeshes.has(selectedFabric) && !legacyMeshes.has(selectedFabric)) {
      scope = null;
      renderedPath();
    }
    isolateFabric(selectedFabric);
  };
  actions["hide-object"].onclick = () => {
    if (!selectedObject) return;
    drafting.cancel();
    hiddenNodes.add(selectedObject);
    if (scope === selectedObject) scope = hierarchy.get(scope)?.parent || null;
    selectFaces(new Map());
    commitState({ ...state(), hiddenNodes: [...hiddenNodes] }, false);
    renderedPath();
    updateVisibility();
    updateControls();
  };
  actions.restore.onclick = () => {
    if (hiddenNodes.size) {
      hiddenNodes.clear();
      commitState({ ...state(), hiddenNodes: [] }, false);
    }
    isolatedFabric = null;
    updateVisibility();
    updateControls();
    fit();
  };
  function removeDraftMark(id) {
    if (state().modelEdges?.some(edge => edge.id === id)) {
      return void changeModelEdges(state().modelEdges.filter(edge => edge.id !== id), () => "Ребро удалено.");
    }
    const marks = state().drafting || [];
    if (selectedMark === id) selectedMark = null;
    commitState({ ...state(), drafting: marks.filter(mark => mark.id !== id) }, false);
    notify("Линия или направляющая удалена.");
    render();
  }

  async function removeFabric(id) {
    const entry = clothEntries().find(item => item.id === id);
    if (!entry || pending || patternTools.busy()) return;
    const modelId = attachment.id;
    const name = [entry.panel?.number, entry.name].filter(Boolean).join(" · ");
    confirmingRemoval = true;
    let confirmed;
    try {
      confirmed = await confirmAction({ title: "Удалить " + name + "?", message: "Будут удалены ткань и связанные с ней лекала.",
        confirmLabel: "Удалить ткань", signal });
    } finally {
      confirmingRemoval = false;
    }
    if (!confirmed || signal.aborted || attachment.id !== modelId) return;
    if (selectedFabric === id) selectedFabric = clothPick = null;
    if (isolatedFabric === id) isolatedFabric = null;
    stitchPanels = stitchPanels.filter(panelId => panelId !== id);
    if (entry.panel) patternTools.removePart(entry.id);
    else {
      selectFaces(new Map([[entry.nodeId, entry.faces]]));
      saveSelection(null);
      updatePanels();
    }
    notify("Ткань удалена · " + name);
    updateControls();
  }
  actions["remove-fabric"].onclick = () => void removeFabric(selectedFabric);
  actions["close-palette"].onclick = () => {
    setPairTarget(null);
    stitchPanels = [];
    patternTools.setStitchPanels?.([]);
    patternDetails.hidden = true;
    chooseTool("select");
    renderAssignedFabrics();
  };
  actions.finish.onclick = () => cutDraft ? finishClothCut() : finishDraft();
  actions.undo.onclick = () => cutDraft ? changeCutPoint(false) : undoPoint();
  actions.cancel.onclick = () => {
    if (!cutDraft) return cancelDraft();
    cancelClothCut();
    updateControls();
    notify(toolHint());
    render();
  };
  actions["history-back"].onclick = () => runHistory(false);
  actions["history-forward"].onclick = () => runHistory(true);
  for (const button of tools) button.onclick = () => chooseTool(button.dataset.tool);

  function workSurface(root, preferred = null, selectedFaces = null, joinFolded = Boolean(selectedFaces)) {
    const selected = selectedFaces || new Map([...hierarchy.collectFaces(root, id => Boolean(items.get(id)) && nodeVisible(id))]
      .map(([id, faces]) => [id, new Set([...faces].filter(face => items.get(id).faceTriangles.has(face)))])
      .filter(([, faces]) => faces.size));
    const recorded = (state().fabricLayers || []).find(layer => preferred ? "fabric:" + layer.id === preferred :
      (!joinFolded || layer.joinFolded) && layer.surfaces.length === selected.size && layer.surfaces.every(({ nodeId, faces }) => {
        const valid = selected.get(nodeId);
        return valid?.size === faces.length && faces.every(face => valid.has(face));
      }));
    const layer = recorded || {
      id: crypto.randomUUID(), name: (hierarchy.get(root)?.name || "Поверхность").slice(0, 120), color: "#57767c",
      surfaces: [...selected].map(([nodeId, faces]) => ({ nodeId, faces: [...faces] })),
      ...(joinFolded ? { joinFolded: true } : {}),
    };
    const geometry = fabricSurface(layer.surfaces.flatMap(({ nodeId, faces }, surfaceIndex) => {
      const item = items.get(nodeId);
      if (!item) return [];
      const include = new Set(faces);
      return [{ node: item.node, surfaceIndex, indices: item.indices.filter(i => include.has(item.node.faces[i])) }];
    }));
    if (!geometry.triangles.length) return null;
    const boundary = new Set();
    for (const { nodeId } of layer.surfaces) {
      const node = items.get(nodeId)?.node;
      if (!node) continue;
      for (const [index, edge] of (node.edges || []).entries()) {
        const flags = node.edgeFlags?.[index];
        if (flags?.visible === false || flags?.soft) continue;
        boundary.add(worldEdgeKey(...edge.map(vertex => node.vertices[vertex])));
      }
    }
    return { layer, nodeId: "fabric:" + layer.id, geometry, boundary, recorded: Boolean(recorded) };
  }

  function buildMesh(geometry, material) {
    const buffer = new BufferGeometry();
    buffer.setAttribute("position", new Float32BufferAttribute(geometry.vertices.flat(), 3));
    buffer.setIndex(geometry.triangles.flat());
    buffer.computeVertexNormals();
    return new Mesh(buffer, material);
  }

  function disposeMesh(mesh) {
    mesh.geometry.dispose();
    mesh.material.dispose();
  }

  function assignLayer(next, work) {
    if (!work.recorded) next.fabricLayers = [...(next.fabricLayers || []), work.layer];
  }

  function onDrawingSurface(id) {
    return draft?.surface.layer.surfaces.some(part => part.nodeId === id);
  }

  function drawingPlane(hit) {
    if (!hit?.face) return null;
    const normal = hit.face.normal.clone().normalize();
    return { normal, offset: normal.dot(hit.point) };
  }

  function anchorPoint(node, anchor) {
    if (anchor.vertex !== undefined) return new Vector3(...node.vertices[anchor.vertex]);
    return node.triangles[anchor.triangle].reduce((point, vertex, index) =>
      point.addScaledVector(new Vector3(...node.vertices[vertex]), anchor.barycentric[index]), new Vector3());
  }

  function sampleAnchor(hit, snap) {
    const faceHit = ray.intersectObject(draft.mesh, false)[0];
    if (!faceHit || faceHit.point.distanceTo(hit.point) > 0.35) return null;
    const node = draft.surface.geometry;
    const triangle = faceHit.faceIndex;
    const corners = node.triangles[triangle].map(index => new Vector3(...node.vertices[index]));
    const tolerance = Math.min(12, hit.distance * Math.tan(camera.fov * Math.PI / 360) * 12 / viewport.clientHeight);
    if (snap) {
      const candidates = node.triangles[triangle].map(vertex => ({ vertex }));
      for (const seam of state().seams || []) {
        if (seam.id !== draft.editingSeam && seam.nodeId === draft.surface.nodeId && seam.placementFor) candidates.push(...seam.anchors);
      }
      let nearest;
      for (const anchor of candidates) {
        const point = anchorPoint(node, anchor);
        const distance = point.distanceTo(faceHit.point);
        if (distance < (nearest?.distance ?? tolerance)) nearest = { anchor, point, distance };
      }
      for (const [a, b] of [[0, 1], [1, 2], [2, 0]]) {
        if (!draft.surface.boundary.has(worldEdgeKey(corners[a].toArray(), corners[b].toArray()))) continue;
        const segment = corners[b].clone().sub(corners[a]);
        const fraction = Math.max(0, Math.min(1, faceHit.point.clone().sub(corners[a]).dot(segment) / segment.lengthSq()));
        const point = corners[a].clone().addScaledVector(segment, fraction);
        const distance = point.distanceTo(faceHit.point);
        if (distance >= (nearest?.distance ?? tolerance)) continue;
        const barycentric = [0, 0, 0];
        barycentric[a] = 1 - fraction;
        barycentric[b] = fraction;
        nearest = { point, distance, anchor: { triangle, barycentric }, snapped: true };
      }
      if (nearest) return nearest;
    }
    const weights = Triangle.getBarycoord(faceHit.point, ...corners, new Vector3());
    if (!weights) return null;
    return { point: faceHit.point, anchor: { triangle, barycentric: weights.toArray().map(value => Math.max(0, Math.min(1, value))) } };
  }

  function contourSample(event, hit, snap = true) {
    if (snap && event && !event.altKey) {
      draft.magnet ??= magneticEdges(draft.surface.geometry, draft.surface.boundary);
      const match = draft.magnet.pick(event, camera, viewport.getBoundingClientRect(), hit);
      if (match) return { ...match, label: "Ребро" };
      const guide = drafting.snapOnPlane(event, draftingContext(hit), draft.arcPlane || drawingPlane(hit));
      if (guide) {
        const probe = new Raycaster(camera.position, guide.point.clone().sub(camera.position).normalize());
        const projected = probe.intersectObject(draft.mesh, false)[0];
        const anchor = projected?.point.distanceTo(guide.point) < .35 && anchorInGeometry(projected, draft.surface.geometry);
        if (anchor) return { ...anchor, label: guide.label };
      }
    }
    if (!hit || !onDrawingSurface(hit.object.userData.nodeId)) return null;
    return sampleAnchor(hit, snap && !event?.altKey);
  }

  function anchorInGeometry(hit, { vertices, triangles }) {
    const corners = triangles[hit.faceIndex].map(index => new Vector3(...vertices[index]));
    const weights = Triangle.getBarycoord(hit.point, ...corners, new Vector3());
    if (!weights) return null;
    const barycentric = weights.toArray().map(value => value < .0000001 ? 0 : Math.min(1, value));
    const total = barycentric.reduce((sum, value) => sum + value, 0);
    return total && { point: hit.point, anchor: { triangle: hit.faceIndex, barycentric: barycentric.map(value => value / total) } };
  }

  function contourHandle(event, knownHit, firstOnly = false) {
    if (!draft || !contourTools.has(mode) || (!draft.closed && !firstOnly)) return -1;
    const rect = renderer.domElement.getBoundingClientRect();
    let nearest = -1;
    let limit = event.pointerType === "touch" ? 19 : 13;
    const points = firstOnly ? draft.points.slice(0, 1) : draft.points;
    for (const [index, point] of points.entries()) {
      const projected = point.clone().project(camera);
      if (projected.z < -1 || projected.z > 1) continue;
      const distance = Math.hypot(event.clientX - rect.left - (1 + projected.x) * rect.width / 2,
        event.clientY - rect.top - (1 - projected.y) * rect.height / 2);
      if (distance < limit) {
        limit = distance;
        nearest = index;
      }
    }
    if (nearest < 0) return nearest;
    const hit = knownHit ?? intersection(event);
    const distance = camera.position.distanceTo(draft.points[nearest]);
    const tolerance = distance * Math.tan(camera.fov * Math.PI / 360) * 32 / rect.height;
    return hit && distance > hit.distance + tolerance ? -1 : nearest;
  }

  function moveContourHandle(event) {
    if (!draft?.closed || !contourDrag || contourDrag.id !== event.pointerId) return;
    const hit = intersection(event);
    const next = contourSample(event, hit);
    if (!next || draft.points.some(point => point.distanceTo(next.point) < 1e-5)) return;
    if (!contourDrag.recorded) {
      rememberDraft();
      contourDrag.recorded = true;
    }
    draft.anchors[contourDrag.index] = next.anchor;
    draft.points[contourDrag.index] = next.point;
    scheduleDraftPreview(90);
    render();
  }

  function stopContourDrag(revert = false) {
    if (!contourDrag) return;
    const { index, id, point, anchor } = contourDrag;
    contourDrag = null;
    cancelAnimationFrame(dragFrame);
    const canvas = renderer.domElement;
    canvas.style.cursor = "";
    if (canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
    if (revert && draft?.closed && draft.anchors[index] !== anchor) {
      draft.anchors[index] = anchor;
      draft.points[index] = point;
      scheduleDraftPreview(0);
    }
    updateControls();
    render();
  }

  function disposeDraftPreview() {
    if (!draftPreview) return;
    scene.remove(draftPreview);
    disposeMesh(draftPreview);
    draftPreview = null;
  }

  function colorDraftPreview() {
    if (draftPreview) draftPreview.material.color.set(form.elements.color.value);
    render();
  }

  function scheduleDraftPreview(delay, seed = null) {
    draft.seed = null;
    draft.previewError = false;
    draft.previewRequest = { revision: ++draft.revision, anchors: [...draft.anchors], seed };
    updateControls();
    if (!delay) void refreshDraftPreview();
    else if (!previewTimer && !previewInFlight) previewTimer = setTimeout(refreshDraftPreview, delay);
  }

  async function refreshDraftPreview() {
    clearTimeout(previewTimer);
    previewTimer = null;
    if (previewInFlight || routeInFlight || !draft?.closed || !draft.previewRequest || signal.aborted) return;
    const target = draft;
    const { revision, anchors, seed } = target.previewRequest;
    const closedVersion = target.closedVersion;
    target.previewRequest = null;
    const next = { ...state() };
    assignLayer(next, target.surface);
    if (target.editingSeam) next.seams = next.seams.filter(seam => seam.id !== target.editingSeam);
    previewInFlight = true;
    const sameContour = () => !signal.aborted && draft === target && target.closed && target.closedVersion === closedVersion;
    const current = () => sameContour() && target.revision === revision;
    try {
      const geometry = await post("preview-contour", { ...attachment, state: next,
        nodeId: target.surface.nodeId, anchors, surfaceRoute: target.surfaceRoute, ...(seed ? { seed } : {}) });
      if (!current() && !(sameContour() && contourDrag)) return;
      disposeDraftPreview();
      draftPreview = buildMesh(geometry, withFabricWeave(new MeshStandardMaterial({
        side: DoubleSide, color: form.elements.color.value, transparent: true, opacity: 0.43, depthWrite: false,
        forceSinglePass: true, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2,
      })));
      scene.add(draftPreview);
      if (current()) {
        target.outline = geometry.outline;
        target.seed = geometry.seed;
        notify("Перетащите точки на поверхности или нажмите «Уложить ткань».");
        updateControls();
      }
      render();
    } catch (error) {
      if (!current()) return;
      disposeDraftPreview();
      target.previewError = true;
      target.outline = null;
      notify(error.message, true);
      updateControls();
      render();
    } finally {
      previewInFlight = false;
      if (draft?.previewRequest && !previewTimer) void refreshDraftPreview();
      else if (!draft?.closed && draft?.routePending) void refreshDraftRoute();
    }
  }

  function scheduleDraftRoute() {
    if (!draft?.surfaceRoute || draft.anchors.length < 2) return;
    draft.routePending = { revision: ++draft.revision, anchors: [...draft.anchors] };
    void refreshDraftRoute();
  }

  async function refreshDraftRoute() {
    if (routeInFlight || previewInFlight || !draft?.routePending || draft.closed || signal.aborted) return;
    const target = draft;
    const { revision, anchors } = target.routePending;
    target.routePending = null;
    const next = { ...state() };
    assignLayer(next, target.surface);
    const current = () => !signal.aborted && draft === target && !target.closed && target.revision === revision;
    routeInFlight = true;
    try {
      const { paths } = await post("route-contour", { ...attachment, state: next, nodeId: target.surface.nodeId, anchors });
      if (!current()) return;
      target.route = { count: anchors.length, points: paths.flatMap((leg, index) => index ? leg.slice(1) : leg) };
      render();
    } catch (error) {
      if (current()) notify(error.message, true);
    } finally {
      routeInFlight = false;
      if (draft?.closed && draft.previewRequest && !previewTimer) void refreshDraftPreview();
      else if (!draft?.closed && draft?.routePending) void refreshDraftRoute();
    }
  }

  function arcSamples(end) {
    const origin = draft.points.at(-1), middle = draft.arcMid;
    const x = middle.clone().sub(origin);
    const u = x.length();
    if (u <= 1e-5) return null;
    x.normalize();
    const y = draft.arcPlane.normal.clone().cross(x);
    const offset = end.clone().sub(origin);
    const v = offset.dot(x), w = offset.dot(y);
    if (Math.abs(w) <= 1e-5) return null;
    const cx = u / 2, cy = (v * v + w * w - u * v) / (2 * w);
    const radius = Math.hypot(cx, cy);
    const turn = Math.PI * 2;
    const start = Math.atan2(-cy, -cx);
    const arcAngle = (a, b) => (Math.atan2(b - cy, a - cx) - start + turn) % turn;
    const forward = arcAngle(v, w);
    const sweep = arcAngle(u, 0) <= forward ? forward : forward - turn;
    const step = 2 * Math.acos(Math.max(-1, 1 - 0.25 / radius));
    const count = Math.max(3, Math.min(96, Math.ceil(Math.abs(sweep) / step)));
    const center = origin.clone().addScaledVector(x, cx).addScaledVector(y, cy);
    return Array.from({ length: count }, (_, index) => {
      const angle = start + sweep * (index + 1) / count;
      return center.clone().addScaledVector(x, Math.cos(angle) * radius).addScaledVector(y, Math.sin(angle) * radius);
    });
  }

  function arcAnchors(points) {
    const normal = draft.arcPlane.normal;
    const probe = new Raycaster();
    const geometry = draft.surface.geometry;
    const anchors = [];
    for (const point of points) {
      probe.set(point.clone().addScaledVector(normal, 0.5), normal.clone().negate());
      const hit = probe.intersectObject(draft.mesh, false)[0];
      if (!hit || hit.point.distanceTo(point) > 0.2) return null;
      const corners = geometry.triangles[hit.faceIndex].map(index => new Vector3(...geometry.vertices[index]));
      const weights = Triangle.getBarycoord(hit.point, ...corners, new Vector3());
      if (!weights) return null;
      anchors.push({ triangle: hit.faceIndex, barycentric: weights.toArray().map(value => Math.max(0, Math.min(1, value))) });
    }
    return anchors;
  }

  function pickDrawing(hit, event) {
    if (!draft) {
      if (!hit) return;
      const work = workSurface(scope || hierarchy.rootId, null, null, true);
      if (!work) return;
      const mesh = buildMesh(work.geometry, new MeshStandardMaterial({ side: DoubleSide }));
      draft = { surface: work, mesh, surfaceRoute: true, points: [], anchors: [], revision: 0, closedVersion: 0 };
    }
    const sample = contourSample(event, hit, !draft.closed);
    if (!sample) return notify("Продолжите на поверхности открытой модели.", true);
    if (draft.closed) {
      if (draft.seed) return;
      scheduleDraftPreview(0, sample.anchor);
    } else {
      if (draft.points.some(point => point.distanceTo(sample.point) < 1e-5)) return;
      if (mode === "arc" && draft.points.length && !draft.arcMid) {
        const plane = drawingPlane(hit);
        if (!plane || [draft.points.at(-1), sample.point].some(point =>
          Math.abs(plane.normal.dot(point) - plane.offset) > 0.35)) {
          return notify("Начало и середина дуги должны лежать на одной плоскости.", true);
        }
        rememberDraft();
        draft.arcPlane = plane;
        draft.arcMid = sample.point;
        notify("Укажите конец дуги.");
      } else if (mode === "arc" && draft.arcMid) {
        if (Math.abs(draft.arcPlane.normal.dot(sample.point) - draft.arcPlane.offset) > 0.35) {
          return notify("Конец дуги должен лежать на той же плоскости.", true);
        }
        const points = arcSamples(sample.point);
        const anchors = points && arcAnchors(points.slice(0, -1));
        if (!anchors) return notify("Дуга должна проходить на выбранной плоскости. Уточните конец дуги.", true);
        rememberDraft();
        draft.anchors.push(...anchors, sample.anchor);
        draft.points.push(...points.slice(0, -1), sample.point);
        draft.arcMid = draft.arcPlane = null;
        notify("Продолжите контур или замкните его.");
      } else {
        rememberDraft();
        draft.points.push(sample.point);
        draft.anchors.push(sample.anchor);
      }
      scheduleDraftRoute();
      hoverPoint = null;
    }
    updateControls();
    render();
  }

  function editContour(id) {
    const panel = state().panels?.find(item => item.id === id);
    const seam = state().seams?.find(item => item.closed && item.placementFor === id);
    if (!panel || !seam || pending) return;
    if (!isLayerVisible(panel.nodeId)) return notify("Откройте группу и покажите поверхность ткани.", true);
    chooseTool("outline");
    const surface = workSurface(scope || hierarchy.rootId, panel.nodeId);
    if (!surface?.recorded || seam.anchors.length < 3) return;
    const points = seam.anchors.map(anchor => anchorPoint(surface.geometry, anchor));
    draft = { surface, mesh: buildMesh(surface.geometry, new MeshStandardMaterial({ side: DoubleSide })),
      surfaceRoute: seam.surfaceRoute === true, points, anchors: [...seam.anchors], revision: 0, closedVersion: 1,
      closed: true, editingPanel: id, editingSeam: seam.id };
    nameField.value = panel.fabricName || panel.name;
    form.elements.color.value = panel.color || fabricColors[0];
    customColor = true;
    updatePanels();
    scheduleDraftPreview(0, panel.seedBarycentric ? { triangle: panel.seed, barycentric: panel.seedBarycentric } : null);
  }

  function cancelDraft(preserveHistory = false) {
    const editing = draft?.editingPanel;
    stopContourDrag();
    clearTimeout(previewTimer);
    previewTimer = null;
    disposeDraftPreview();
    if (draft) disposeMesh(draft.mesh);
    draft = null;
    if (preserveHistory !== true) draftBack.length = draftForward.length = 0;
    hoverPoint = null;
    if (editing) updatePanels();
    updateControls();
    render();
  }

  function undoPoint() {
    if (draftBack.length) restoreDraft(false);
    else if (draft?.closed) {
      const open = { ...draftSnapshot(), closed: false, seed: null };
      replaceDraft(open);
    } else return cancelDraft();
    updateControls();
    render();
  }

  function draftSnapshot() {
    if (!draft?.points.length) return null;
    const { surface, surfaceRoute, closed, seed, arcPlane, arcMid, route, editingPanel, editingSeam } = draft;
    return { surface, surfaceRoute, closed, seed, arcPlane, arcMid, route, editingPanel, editingSeam,
      points: [...draft.points], anchors: [...draft.anchors] };
  }

  function rememberDraft() {
    draftBack.push(draftSnapshot());
    if (draftBack.length > 50) draftBack.shift();
    draftForward.length = 0;
  }

  function replaceDraft(snapshot) {
    cancelDraft(true);
    if (snapshot) {
      draft = { ...snapshot, seed: null, revision: 0, closedVersion: 1,
        mesh: buildMesh(snapshot.surface.geometry, new MeshStandardMaterial({ side: DoubleSide })) };
      if (snapshot.editingPanel) updatePanels();
      if (snapshot.closed) scheduleDraftPreview(0, snapshot.seed);
      else scheduleDraftRoute();
    }
    updateControls();
    render();
  }

  function restoreDraft(forward) {
    const from = forward ? draftForward : draftBack, to = forward ? draftBack : draftForward;
    if (!from.length) return false;
    to.push(draftSnapshot());
    replaceDraft(from.pop());
    return true;
  }

  function updateHistoryControls() {
    const blocked = pending || !hierarchy || patternTools.busy() || Boolean(contourDrag) || Boolean(history?.busy());
    actions["history-back"].disabled = blocked ||
      !(cutDraft?.path.length || draftBack.length || drafting.canUndo?.(mode) || history?.canUndo());
    actions["history-forward"].disabled = blocked ||
      !(cutDraft?.forward.length || draftForward.length || drafting.canRedo?.(mode) || history?.canRedo());
  }

  function runHistory(forward) {
    if ((forward ? actions["history-forward"] : actions["history-back"]).disabled) return;
    if (!changeCutPoint(forward) && !restoreDraft(forward)) {
      const handled = draftingTools.has(mode) && (forward ? drafting.redoPoint(mode) : drafting.undoPoint(mode));
      if (!handled) {
        cancelClothCut();
        cancelDraft();
        if (forward) history?.redo();
        else history?.undo();
      }
    }
    notify(forward ? "Действие возвращено." : "Действие отменено.");
    updateControls();
    render();
  }

  function post(route, payload) {
    return request(api + route, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal });
  }

  function finishDraft() {
    if (!draft || pending || patternTools.busy()) return;
    if (draft.closed || draft.points.length < 3 || draft.arcMid) return;
    rememberDraft();
    draft.closed = true;
    draft.closedVersion++;
    hoverPoint = null;
    notify("Строим предпросмотр ткани…");
    scheduleDraftPreview(0);
    render();
  }

  function selectedRegionFaces(work) {
    const selected = new Set(work.layer.surfaces.flatMap(({ nodeId }, index) =>
      [...(selection.get(nodeId) || [])].map(face => index + ":" + face)));
    return [...new Set(work.geometry.faces.filter(face => selected.has(face)))];
  }

  async function savePart(fabric, contour = null) {
    const root = selection.size === 1 ? selection.keys().next().value : scope || hierarchy.rootId;
    const work = contour?.surface || workSurface(root, null, selection);
    if (!work) return notify("На этой поверхности нет доступных граней.", true);
    const next = { ...state() };
    assignLayer(next, work);
    const faces = contour ? null : selectedRegionFaces(work);
    if (faces && !faces.length) return notify("Выберите поверхности для ткани.", true);
    return load(async () => {
      const { regions } = contour ? { regions: [{ faces: [], seed: contour.seed.triangle, seedBarycentric: contour.seed.barycentric }] } :
        await post("selected-regions", { ...attachment, state: next, nodeId: work.nodeId, faces });
      if (signal.aborted) return;
      const previous = next.panels?.find(panel => panel.id === contour?.editingPanel);
      const panels = regions.map((region, index) => ({ ...previous, ...region, id: previous?.id || crypto.randomUUID(),
        name: fabric.name + (regions.length > 1 ? " " + (index + 1) : ""),
        nodeId: work.nodeId, color: fabric.color, fabricName: fabric.name }));
      next.panels = previous ? next.panels.map(panel => panel.id === previous.id ? panels[0] : panel) :
        [...(next.panels || []), ...panels];
      if (contour) {
        const seam = { id: contour.editingSeam || crypto.randomUUID(), nodeId: work.nodeId, kind: "seam", closed: true,
          placementFor: panels[0].id, anchors: contour.anchors, ...(contour.surfaceRoute ? { surfaceRoute: true } : {}) };
        next.seams = contour.editingSeam ? next.seams.map(item => item.id === seam.id ? seam : item) : [...(next.seams || []), seam];
      }
      const ids = new Set(panels.map(panel => panel.id));
      const calculated = await post("calculate", { ...attachment, state: next, sync2d: true,
        ...(!contour ? { checkPlacement: [...ids] } : {}) });
      if (signal.aborted) return;
      const diagnostic = calculated.diagnostics?.find(issue => ids.has(issue.panelId) &&
        (issue.code === "cover_panel_overlap" || !calculated.placements.some(item => item.id === issue.panelId)));
      if (diagnostic || !panels.every(panel => calculated.placements?.some(item => item.id === panel.id))) {
        throw new Error(diagnostic?.message || "Не удалось построить ткань на выбранной поверхности.");
      }
      cancelDraft();
      fabricDialog.close();
      selectedFabric = panels[0].id;
      commitState(next, false);
      selectFaces(new Map());
      renderFabricChoices();
      patternTools.refresh(calculated);
      patternTools.selectPanel(selectedFabric);
      const flat = panels.every(panel => calculated.panels?.some(item => item.id === panel.id));
      const amount = panels.length > 1 ? " · Лекал: " + panels.length : "";
      notify(flat ? "Ткань нанесена · " + fabric.name + amount : "Ткань нанесена. Проверьте ошибки развёртки.", !flat);
    }, "Рассчитываем ткань и лекало…");
  }

  function isLayerVisible(nodeId) {
    const layer = state().fabricLayers?.find(item => "fabric:" + item.id === nodeId);
    return Boolean(layer?.surfaces.every(part => nodeVisible(part.nodeId)));
  }

  function focusPanel(panel) {
    focusedPanel = typeof panel === "string" ? panel : panel?.id;
    const active = state().panels?.find(item => item.id === focusedPanel);
    if (active && !selection.size && !draft) {
      nameField.value = active.fabricName || "";
      form.elements.color.value = active.color || fabricColors[0];
      customColor = Boolean(active.color);
    }
    paintClothSelection();
    updateControls();
    render();
  }

  function paintPlacement(mesh) {
    if (!mesh) return;
    const { panelId, fabricId, materialColor, placement } = mesh.userData;
    const id = panelId || fabricId;
    const role = clothRole(id);
    const hovered = id === hoveredPanel;
    mesh.material.color.copy(materialColor);
    if (role || hovered) {
      const light = 0.2126 * materialColor.r + 0.7152 * materialColor.g + 0.0722 * materialColor.b;
      const tint = light < .27 ? neutral : accent;
      mesh.material.color.lerp(tint, role ? .17 : .08);
      if (!mesh.userData.outline) {
        if (!boundaryCache.has(placement)) boundaryCache.set(placement, clothBoundary(placement));
        const edges = boundaryCache.get(placement);
        if (!edges.length) return;
        mesh.userData.outline = createClothOutline(edges, outlineViewport, outlineNear);
        mesh.add(mesh.userData.outline.mesh);
      }
      const { outline } = mesh.userData;
      const erasing = mode === "erase" && hovered;
      outline.paint(erasing ? "#942b21" : clothInk[role || "hovered"], erasing ? 3.8 : role === pairTarget ? 4.6 : role ? 3.8 : 2.3);
      outline.mesh.visible = true;
    } else if (mesh.userData.outline) mesh.userData.outline.mesh.visible = false;
  }

  function paintClothSelection() {
    for (const mesh of panels?.children || []) paintPlacement(mesh);
  }

  function tagPoints(mesh) {
    if (mesh.userData.tagPoints) return mesh.userData.tagPoints;
    const { vertices, triangles } = mesh.userData.placement;
    const candidates = [];
    const stride = Math.max(1, Math.floor(triangles.length / 180));
    for (let i = 0; i < triangles.length; i += stride) {
      const [a, b, c] = triangles[i].map(index => new Vector3(...vertices[index]));
      const area = b.clone().sub(a).cross(c.clone().sub(a)).lengthSq();
      candidates.push({ area, point: a.add(b).add(c).divideScalar(3) });
    }
    mesh.userData.tagPoints = candidates.sort((a, b) => b.area - a.area).slice(0, 12).map(item => item.point);
    return mesh.userData.tagPoints;
  }

  function fabricTagPoint(mesh, id, occluders) {
    const centers = tagPoints(mesh);
    const candidates = clothPick?.id === id ? [clothPick.point, ...centers] : centers;
    for (const point of candidates) {
      const screen = point.clone().project(camera);
      if (Math.abs(screen.x) > .97 || Math.abs(screen.y) > .97 || Math.abs(screen.z) > 1) continue;
      const direction = point.clone().sub(camera.position);
      const distance = direction.length();
      tagRay.set(camera.position, direction.normalize());
      tagRay.far = Math.max(0, distance - Math.max(.05, distance * 0.00002));
      if (!tagRay.intersectObjects(occluders, false).length) return point;
    }
    return null;
  }

  function addFabricTags(annotations) {
    const occluders = [...panels.children];
    if (!fabricOnly) occluders.push(...objects.children.filter(object => object.visible));
    for (const mesh of panels.children) {
      const id = mesh.userData.panelId || mesh.userData.fabricId;
      const role = clothRole(id);
      if (!role) continue;
      const point = fabricTagPoint(mesh, id, occluders);
      if (!point) continue;
      const panel = state().panels?.find(item => item.id === id);
      const name = panel?.number || panel?.fabricName || mesh.userData.label || "";
      const label = role === "a" || role === "b" ? (role === "a" ? "А" : "Б") + " · " + name : name;
      annotations.push({ kind: "point", points: [point.toArray()], glyph: "diamond", color: clothInk[role], strong: true, label });
    }
  }

  function updatePanels() {
    if (!panels) return;
    erasePathCache = null;
    placementMeshes.clear();
    for (const mesh of [...panels.children]) {
      panels.remove(mesh);
      for (const line of mesh.children) disposeMesh(line);
      disposeMesh(mesh);
    }
    for (const line of [...cutLines.children]) {
      cutLines.remove(line);
      disposeMesh(line);
    }
    legacyMeshes.clear();
    for (const placement of geometryResult?.placements || []) {
      if (placement.id === draft?.editingPanel || !isLayerVisible(placement.nodeId) ||
        (isolatedFabric && placement.id !== isolatedFabric)) continue;
      const panel = state().panels?.find(item => item.id === placement.id);
      if (!panel) continue;
      const color = new Color(panel.color || "#57767c");
      if (geometryResult.diagnostics?.some(issue => issue.code === "cover_panel_overlap" && issue.panelId === panel.id)) color.set("#ce4538");
      const mesh = buildMesh(placement, withFabricWeave(new MeshStandardMaterial({ color, side: DoubleSide,
        polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4 })));
      mesh.userData.panelId = panel.id;
      mesh.userData.materialColor = color;
      mesh.userData.placement = placement;
      addClothFolds(mesh, placement, color);
      placementMeshes.set(panel.id, mesh);
      paintPlacement(mesh);
      panels.add(mesh);
    }
    if (fabricOnly || selectedFabric?.startsWith("surface:")) for (const entry of clothEntries()) {
      if (entry.panel || isolatedFabric && entry.id !== isolatedFabric || !nodeVisible(entry.nodeId) ||
          !fabricOnly && entry.id !== selectedFabric) continue;
      const item = items.get(entry.nodeId);
      const triangles = item.indices.filter(index => entry.faces.has(item.node.faces[index])).map(index => item.node.triangles[index]);
      const placement = { vertices: item.node.vertices, triangles };
      const color = new Color(entry.color);
      const mesh = buildMesh(placement, withFabricWeave(new MeshStandardMaterial({ color, side: DoubleSide,
        polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4 })));
      addClothFolds(mesh, placement, color);
      mesh.userData.fabricId = entry.id;
      mesh.userData.label = entry.name;
      mesh.userData.materialColor = color;
      mesh.userData.placement = placement;
      legacyMeshes.set(entry.id, mesh);
      paintPlacement(mesh);
      panels.add(mesh);
    }
    for (const seam of geometryResult?.seams || []) {
      if (!seam.coverCut || !seam.panelIds.some(id => placementMeshes.has(id))) continue;
      const segments = seam.points.slice(1).map((point, index) => [seam.points[index], point]);
      if (segments.length) cutLines.add(clothStroke(segments, seam.kind === "slash" ? "#63280f" : "#1c2330", 1, 3));
    }
    render();
  }

  function clothStroke(segments, color, opacity, order) {
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(segments.flat(2), 3));
    const material = new LineBasicMaterial({ color, opacity, transparent: opacity < 1, depthWrite: false });
    // The filled cloth uses polygon offset. Keep these lines on top of it while testing scene depth.
    material.onBeforeCompile = shader => {
      shader.vertexShader = shader.vertexShader.replace("#include <project_vertex>",
        "#include <project_vertex>\n gl_Position.z -= 0.000025 * gl_Position.w;");
    };
    const line = new LineSegments(geometry, material);
    line.renderOrder = order;
    return line;
  }

  function addClothFolds(mesh, placement, color) {
    if (!foldCache.has(placement)) foldCache.set(placement, clothFoldSegments(placement));
    const folds = foldCache.get(placement);
    mesh.userData.folds = folds;
    const tint = color.clone().lerp(new Color("#24232b"), .75);
    for (const [kind, opacity] of [["crease", .76], ["transition", .48]]) {
      const segments = folds.filter(segment => segment.kind === kind).map(segment => segment.points);
      if (segments.length) mesh.add(clothStroke(segments, tint, opacity, 2));
    }
  }

  function restoreFabrics() {
    fabrics.clear();
    for (const fabric of state().surfaceFabrics || []) {
      const { nodeId, faceId } = fabric;
      if (!fabrics.has(nodeId)) fabrics.set(nodeId, new Map());
      fabrics.get(nodeId).set(faceId, fabric);
    }
  }

  function replaceWorkingModel(value) {
    clearHover();
    disposeMeshes();
    selection = new Map();
    planeClick = null;
    hierarchy = createObjectIndex(value.nodes);
    for (const node of value.nodes) if (node.visible !== false) meshFor(node);
    if (scope && !hierarchy.get(scope)) scope = null;
    if (selectedObject && !hierarchy.get(selectedObject)) selectedObject = null;
    renderedPath();
    updateVisibility();
    fitButton.disabled = !items.size;
  }

  function changeModelEdges(edges, done) {
    if (pending || patternTools.busy()) return;
    const before = state().modelEdges || [];
    const next = { ...state(), modelEdges: edges };
    return load(async () => {
      const model = await post("model-surface", { ...attachment, state: next, previousEdges: before });
      if (signal.aborted) return;
      selectedMark = null;
      commitState(next, false);
      replaceWorkingModel(model);
      notify(done());
      render();
    }, "Обновляем рёбра модели…");
  }

  function addModelEdge({ edge, normal }) {
    return changeModelEdges([...(state().modelEdges || []), edge], () => {
      if (mode === "line") drafting.continueLine(edge, normal);
      return "Ребро добавлено. Щёлкните, чтобы продолжить, или нажмите Esc.";
    });
  }

  function showModel(value, next) {
    attachScene();
    clearHover();
    disposeMeshes();
    attachment = next;
    hierarchy = createObjectIndex(value.nodes);
    scope = null;
    planeClick = null;
    selection = new Map();
    selectedObject = isolatedFabric = selectedFabric = clothPick = null;
    selectedMark = null;
    drafting.reset();
    hiddenNodes = new Set(state().hiddenNodes || []);
    fabricOnly = false;
    stitchPanels = [];
    patternDetails.hidden = true;
    geometryResult = focusedJoin = focusedPanel = null;
    mode = "select";
    updateMouseMode();
    cancelClothCut();
    cancelDraft();
    search.value = "";
    restoreFabrics();
    for (const node of value.nodes) if (node.visible !== false) meshFor(node);
    selectFaces(new Map());
    renderFabricChoices();
    openPatterns();
    patternTools.refresh();
    renderedPath();
    updateVisibility();
    updateControls();
    placeholder.hidden = true;
    fitButton.disabled = !items.size;
    resize();
    fit();
    notify(toolHint());
  }

  const fetchModel = id => request(api + "model?id=" + encodeURIComponent(id), { signal });
  function workingModel(next) {
    return next.state?.modelEdges?.length ? post("model-surface", next) : Promise.resolve(importedModel);
  }

  async function load(operation, message) {
    if (pending) return;
    pending = true;
    clearHover();
    updateControls();
    notify(message);
    try { await operation(); }
    catch (error) { if (!signal.aborted) notify(error.message, true); }
    finally { pending = false; if (!signal.aborted) updateControls(); }
  }

  updateControls();
  return {
    setVisible(value, loadModel = true) {
      visible = value;
      if (!visible) { closeContextMenu(); stopContourDrag(); clearHover(); return; }
      if (loadModel && attachment && !hierarchy) {
        void load(async () => {
          importedModel = await fetchModel(attachment.id);
          const value = await workingModel(attachment);
          if (!signal.aborted) showModel(value, attachment);
        }, "Открываем модель…");
      } else if (renderer) {
        resize();
        renderer.domElement.focus({ preventScroll: true });
      }
    },
    importFile(file) {
      return load(async () => {
        const { id } = await request(api + "import?filename=" + encodeURIComponent(file.name), {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file, signal,
        });
        importedModel = await fetchModel(id);
        if (signal.aborted) return;
        showModel(importedModel, { id, state: {} });
        onChange(attachment);
      }, "Читаем модель SketchUp…");
    },
    async applyCadState(next, { recalculate = false, calculated = null } = {}) {
      if (next?.id !== attachment?.id) return;
      const before = state();
      attachment = next;
      if (hierarchy && before.modelEdges !== state().modelEdges) {
        drafting.cancel();
        await load(async () => {
          const model = await workingModel(next);
          if (!signal.aborted && attachment === next) replaceWorkingModel(model);
        }, "Восстанавливаем рёбра модели…");
        if (signal.aborted || attachment !== next) return;
      }
      if (hierarchy && before.hiddenNodes !== state().hiddenNodes) {
        hiddenNodes = new Set(state().hiddenNodes || []);
        while (scope && !nodeVisible(scope)) scope = hierarchy.get(scope)?.parent || null;
        renderedPath();
        updateVisibility();
      }
      if (before.surfaceFabrics !== state().surfaceFabrics) {
        const old = [...fabrics.values()].flatMap(faces => [...faces.values()]);
        restoreFabrics();
        for (const fabric of [...old, ...(state().surfaceFabrics || [])]) paintFace(fabric);
        updatePanels();
      }
      if (selectedFabric && !clothEntries().some(entry => entry.id === selectedFabric)) selectedFabric = null;
      if (isolatedFabric && !clothEntries().some(entry => entry.id === isolatedFabric)) isolatedFabric = null;
      if (selectedMark && !state().drafting?.some(mark => mark.id === selectedMark) &&
          !state().modelEdges?.some(edge => edge.id === selectedMark)) selectedMark = null;
      stitchPanels = stitchPanels.filter(id => state().panels?.some(panel => panel.id === id));
      if (recalculate) {
        cancelClothCut();
        openPatterns();
        patternTools.refresh(calculated);
      }
      renderAssignedFabrics();
      updateControls();
      render();
    },
    updateHistory: updateHistoryControls,
    notify,
    busy: () => pending || patternTools.busy(),
    isVisible: () => visible,
    destroy() {
      lifecycle.abort();
      cancelAnimationFrame(hoverFrame);
      patternTools.destroy();
      cancelClothCut();
      cancelDraft();
      observer?.disconnect();
      controls?.dispose();
      disposeMeshes();
      geometryResult = null;
      updatePanels();
      renderer?.dispose();
      renderer?.forceContextLoss();
    },
  };
}
