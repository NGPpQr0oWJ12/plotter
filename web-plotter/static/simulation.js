"use strict";

const SHORT_CUT_LIMIT_MM = 10;
const SHORT_CUT_EPSILON_MM = 1e-6;

class CutStatistics {
  constructor(root, formatDuration, number) {
    root.append(document.getElementById("cut-statistics-template").content.cloneNode(true));
    this.fields = new Map([...root.querySelectorAll("[data-stat]")].map((field) => [field.dataset.stat, field]));
    this.progress = root.querySelector("progress");
    this.root = root;
    this.timeLabel = root.querySelector('[data-stat-label="time"]');
    this.track = root.querySelector(".cut-breakdown-track");
    this.formatDuration = formatDuration;
    this.number = number;
  }

  write(name, text) {
    const field = this.fields.get(name);
    if (field.textContent !== text) field.textContent = text;
  }

  setPlan(plan, showProgress = true) {
    if (plan === this.plan && showProgress === this.showProgress) return;
    this.plan = plan;
    this.showProgress = showProgress;
    this.root.classList.toggle("is-progress", showProgress);
    if (this.timeLabel) this.timeLabel.textContent = showProgress ? "Осталось ≈" : "Время ≈";
    const material = plan?.material;
    const percent = material?.utilizationPercent;
    this.write("utilization", Number.isFinite(percent) ? `${this.number(percent)} %` : "—");
    this.fields.get("utilization").title = material?.openContourCount
      ? "КИМ рассчитан по замкнутым контурам. Открытые линии в площадь не входят."
      : "";
    this.write("area", Number.isFinite(percent)
      ? `${this.number(material.partsAreaMm2 / 1e6)} из ${this.number(material.areaMm2 / 1e6)} м²${material.openContourCount ? " · оценка" : ""}`
      : "");
    const hasTime = Number.isFinite(plan?.estimatedWorkSeconds);
    this.write("duration", showProgress && hasTime ? `из ${this.formatDuration(Math.ceil(plan.estimatedWorkSeconds))}` : "");
    this.write("passes", plan?.pages?.length ? this.number(plan.pages.length) : "—");
    this.write("fabric-length", Number.isFinite(material?.lengthMm) ? this.number(material.lengthMm / 1000) : "—");
    if (!showProgress) this.update(null, plan?.estimatedWorkSeconds);
    const cut = plan?.cutDistanceMm;
    const travel = plan?.travelDistanceMm;
    this.write("cut", Number.isFinite(cut) ? `${this.number(cut / 1000)} м` : "—");
    this.write("travel", Number.isFinite(travel) ? `${this.number(travel / 1000)} м` : "—");
    this.track.children[0].style.flexGrow = cut || 0;
    this.track.children[1].style.flexGrow = travel || 0;
    this.track.hidden = !(cut || travel);
  }

  update(progress, remaining) {
    const known = Number.isFinite(progress);
    if (known) this.progress.value = Math.max(0, Math.min(1, progress));
    else this.progress.removeAttribute("value");
    this.write("percent", known ? `${this.number(this.progress.value * 100)} %` : "—");
    this.write("remaining", Number.isFinite(remaining) ? this.formatDuration(Math.ceil(remaining)) : "—");
  }
}

class CutSimulation {
  constructor({ projectPoint, element, number, onChange, onPageChange, onProgress, createHeadMarker, markerScale, statistics }) {
    this.projectPoint = projectPoint;
    this.element = element;
    this.number = number;
    this.onChange = onChange;
    this.onPageChange = onPageChange;
    this.onProgress = onProgress;
    this.createHeadMarker = createHeadMarker;
    this.markerScale = markerScale;
    this.dock = document.getElementById("simulation-dock");
    this.status = document.getElementById("simulation-status");
    this.toggle = document.getElementById("simulation-toggle");
    this.pageSelect = document.getElementById("simulation-page");
    this.speedSelect = document.getElementById("simulation-speed");
    this.layer = document.getElementById("simulation-layer");
    this.statistics = statistics;
    this.shortCutsToggle = document.getElementById("simulation-short-cuts");
    this.shortStatus = document.getElementById("simulation-short-status");
    this.shortCutsToggle.addEventListener("change", () => this.updateShortCutsVisibility());
    this.active = false;
    this.visiblePage = null;
    this.running = false;
    this.frame = null;

    this.toggle.addEventListener("click", () => {
      if (this.running) {
        this.advance(performance.now());
        this.pause();
      } else {
        this.play();
      }
      this.render();
    });
    document.getElementById("simulation-restart").addEventListener("click", () => this.seek(0, true));
    this.pageSelect.addEventListener("change", () => this.seek(Number(this.pageSelect.value), this.running));
    this.speedSelect.addEventListener("change", () => {
      if (this.running) this.advance(performance.now());
      this.speed = Number(this.speedSelect.value);
      this.render();
    });
    document.getElementById("simulation-close").addEventListener("click", () => this.close());
  }

  start(plan, page = 1) {
    this.buildTimeline(plan);
    this.statistics.setPlan(plan);
    this.speed = Number(this.speedSelect.value);
    const options = this.pages.map((page, index) => new Option(String(page.number), String(index)));
    this.pageSelect.replaceChildren(...options);
    this.pageSelect.disabled = this.pages.length < 2;
    this.active = true;
    this.dock.hidden = false;
    this.updateShortCutsVisibility();
    this.seek(page - 1, true);
    this.onChange();
  }

  buildTimeline(plan) {
    if (!plan?.pages?.length || ![plan.cutSpeedMmS, plan.travelSpeedMmS, plan.feedSpeedMmS]
        .every((speed) => Number.isFinite(speed) && speed > 0)) {
      throw new Error("Сначала подготовьте раскладку для симуляции.");
    }

    this.moves = [];
    this.pages = [];
    this.duration = 0;
    this.workDuration = 0;
    const addMove = (page, phase, from, to, seconds, { feed = 0, short = false } = {}) => {
      const start = this.duration;
      const end = start + seconds;
      if (end === start) return;
      this.duration = end;
      this.moves.push({ page, phase, from, to, start, end: this.duration, feed, short,
        workStart: this.workDuration });
      if (phase !== "feed") this.workDuration += seconds;
    };
    const moveTo = (page, phase, from, to, speed, options) => {
      addMove(page, phase, from, to, Math.hypot(to[0] - from[0], to[1] - from[1]) / speed, options);
    };

    for (const page of plan.pages) {
      const origin = [page.startX, 0];
      const index = this.pages.length;
      const shortCuts = [];
      this.pages.push({ number: page.number, start: this.duration, firstMove: this.moves.length, origin, shortCuts, completedShort: 0 });
      let position = origin;
      for (const path of page.paths) {
        const points = path.points;
        let length = 0;
        if (!path.closed) {
          for (let i = 1; i < points.length && length <= SHORT_CUT_LIMIT_MM + SHORT_CUT_EPSILON_MM; i++) {
            length += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
          }
        }
        const short = length > 0 && length <= SHORT_CUT_LIMIT_MM + SHORT_CUT_EPSILON_MM;
        if (short) shortCuts.push(points);
        moveTo(index, "travel", position, points[0], plan.travelSpeedMmS);
        for (let i = 1; i < points.length; i++) {
          moveTo(index, "cut", points[i - 1], points[i], plan.cutSpeedMmS, { short });
        }
        if (short) this.moves.at(-1).lastShortSegment = true;
        if (path.closed) moveTo(index, "cut", points[points.length - 1], points[0], plan.cutSpeedMmS);
        position = path.closed ? points[0] : points[points.length - 1];
      }
      addMove(index, "feed", position, position, page.feedAfterMm / plan.feedSpeedMmS, { feed: page.feedAfterMm });
      this.endPoint = position;
    }
  }

  seek(index, play) {
    if (!this.active || !this.pages[index]) return;
    this.pause();
    const page = this.pages[index];
    this.elapsed = page.start;
    this.moveIndex = page.firstMove;
    for (const [i, item] of this.pages.entries()) item.completedShort = i < index ? item.shortCuts.length : 0;
    this.traces = { cut: this.newTrace(), short: this.newTrace(), travel: this.newTrace() };
    this.shortPreview = this.element("path", { class: "simulation-short-preview", "vector-effect": "non-scaling-stroke" });
    this.shortPoints = this.element("path", { class: "simulation-short-preview-points", "vector-effect": "non-scaling-stroke" });
    this.shortMarker = this.element("circle", { class: "simulation-short-marker", "vector-effect": "non-scaling-stroke" });
    this.previewPage = null;
    this.partial = this.element("line", { "vector-effect": "non-scaling-stroke" });
    this.feed = this.element("line", { class: "simulation-feed", "vector-effect": "non-scaling-stroke" });
    this.head = this.createHeadMarker();
    this.layer.replaceChildren(this.traces.travel.group, this.shortPreview, this.shortPoints,
      this.traces.cut.group, this.traces.short.group, this.partial, this.feed, this.shortMarker, this.head);
    if (play) this.play();
    this.render();
  }

  newTrace() {
    return { group: this.element("g"), node: null, commands: [], last: null, dirty: false };
  }

  trace(move) {
    const trace = this.traces[move.short ? "short" : move.phase];
    if (!trace.node || trace.commands.length >= 256) {
      this.flushTrace(trace);
      trace.node = this.element("path", {
        class: `simulation-${move.phase}${move.short ? " simulation-short" : ""}`, "vector-effect": "non-scaling-stroke", fill: "none",
      });
      trace.group.append(trace.node);
      trace.commands = [];
      trace.last = null;
    }
    if (!trace.last || trace.last[0] !== move.from[0] || trace.last[1] !== move.from[1]) {
      trace.commands.push(`M${this.projectPoint(...move.from).join(" ")}`);
    }
    trace.commands.push(`L${this.projectPoint(...move.to).join(" ")}`);
    trace.last = move.to;
    trace.dirty = true;
  }

  flushTrace(trace) {
    if (!trace.dirty) return;
    trace.node.setAttribute("d", trace.commands.join(""));
    trace.dirty = false;
  }

  advance(now) {
    this.elapsed = Math.min(this.duration, this.elapsed + Math.max(0, now - this.lastFrame) * this.speed / 1000);
    this.lastFrame = now;
    while (this.moveIndex < this.moves.length && this.moves[this.moveIndex].end <= this.elapsed) {
      const move = this.moves[this.moveIndex++];
      if (move.phase !== "feed") this.trace(move);
      if (move.lastShortSegment) this.pages[move.page].completedShort++;
    }
    this.flushTrace(this.traces.cut);
    this.flushTrace(this.traces.short);
    this.flushTrace(this.traces.travel);
    if (this.moveIndex === this.moves.length) this.pause();
  }

  play() {
    if (!this.active || this.running || this.moveIndex === this.moves.length) return;
    this.running = true;
    this.lastFrame = performance.now();
    this.scheduleFrame();
  }

  scheduleFrame() {
    this.frame = requestAnimationFrame((now) => {
      this.frame = null;
      if (!this.running) return;
      this.advance(now);
      this.render();
      if (this.running) this.scheduleFrame();
    });
  }

  pause() {
    this.running = false;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
  }

  close() {
    if (!this.active) return;
    this.pause();
    this.active = false;
    this.visiblePage = null;
    this.dock.hidden = true;
    this.layer.replaceChildren();
    this.moves = [];
    this.pages = [];
    this.traces = null;
    this.partial = null;
    this.feed = null;
    this.head = null;
    this.onChange();
  }

  updateShortCutsVisibility() {
    this.layer.classList.toggle("show-short-cuts", this.shortCutsToggle.checked);
  }

  renderShortCuts(page) {
    if (page !== this.previewPage) {
      this.previewPage = page;
      const paths = this.pages[page].shortCuts;
      this.shortPreview.setAttribute("d", paths.map((points) => points.map((point, index) =>
        (index ? "L" : "M") + this.projectPoint(...point).join(" ")).join("")).join(""));
      this.shortPoints.setAttribute("d", paths.map(([point]) => "M" + this.projectPoint(...point).join(" ") + "h0").join(""));
    }
    const { completedShort, shortCuts } = this.pages[page];
    const status = shortCuts.length ? `Короткие резы ${completedShort} / ${shortCuts.length}` : "Короткие резы";
    if (this.shortStatus.textContent !== status) this.shortStatus.textContent = status;
  }

  render() {
    if (!this.active) return;
    const move = this.moves[this.moveIndex];
    const page = move?.page ?? this.pages.length - 1;
    if (this.visiblePage !== this.pages[page].number) {
      this.visiblePage = this.pages[page].number;
      this.pageSelect.value = String(page);
      this.onPageChange?.();
    }
    let point = this.endPoint;
    let phase = "Симуляция завершена";
    let feeding = false;
    if (move) {
      const ratio = Math.max(0, Math.min(1, (this.elapsed - move.start) / (move.end - move.start)));
      point = move.from.map((value, axis) => value + (move.to[axis] - value) * ratio);
      if (move.phase === "feed") {
        feeding = true;
        const distance = move.feed * ratio;
        const [startX, startY] = this.pages[page].origin;
        const [x1, y1] = this.projectPoint(startX, startY);
        const [x2, y2] = this.projectPoint(startX + distance, startY);
        this.feed.setAttribute("x1", x1);
        this.feed.setAttribute("y1", y1);
        this.feed.setAttribute("x2", x2);
        this.feed.setAttribute("y2", y2);
        phase = `Протяжка ${this.number(distance)} из ${this.number(move.feed)} мм`;
      } else {
        phase = move.short ? "Короткий рез" : move.phase === "cut" ? "Рез" : "Холостой ход";
        const [x1, y1] = this.projectPoint(...move.from);
        this.partial.setAttribute("class", `simulation-${move.phase}${move.short ? " simulation-short" : ""}`);
        this.partial.setAttribute("x1", x1);
        this.partial.setAttribute("y1", y1);
      }
      if (!this.running) phase = `Пауза · ${phase}`;
    }
    const [x, y] = this.projectPoint(...point);
    this.feed.setAttribute("visibility", feeding ? "visible" : "hidden");
    this.partial.setAttribute("visibility", move && !feeding ? "visible" : "hidden");
    this.partial.setAttribute("x2", x);
    this.partial.setAttribute("y2", y);
    const units = this.markerScale() || 1;
    this.head.setAttribute("transform", `translate(${x} ${y}) scale(${units})`);
    this.shortMarker.setAttribute("visibility", move?.short ? "visible" : "hidden");
    this.shortMarker.setAttribute("cx", x);
    this.shortMarker.setAttribute("cy", y);
    this.shortMarker.setAttribute("r", 8 * units);
    this.renderShortCuts(page);

    this.toggle.disabled = !move;
    this.toggle.textContent = this.running ? "Пауза" : "Продолжить";
    const workElapsed = move ? move.workStart + (feeding ? 0 : this.elapsed - move.start) : this.workDuration;
    this.statistics.update(this.workDuration ? workElapsed / this.workDuration : 1, this.workDuration - workElapsed);
    if (phase !== this.status.textContent) this.status.textContent = phase;
    this.onProgress("simulation", this.pages[page].number, this.pages.length, phase, !move);
  }
}
