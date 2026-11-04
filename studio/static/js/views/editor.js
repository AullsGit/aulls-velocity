// Editor leve pós-geração: a IA criou → o utilizador corrige → exporta (sem gerar tudo de novo).
// O browser só edita um "documento" simples (lista de planos, textos, look); quem reconstrói a
// timeline final e reaplica os efeitos da IA é o backend (velocity/edit/timeline_ops.py).
import { h, icon, toast, fmtDur, confirmSheet, setBusy, fill } from "../ui.js";
import { t } from "../i18n.js";
import { fitFrame } from "./common.js";
import { api, uid, uploadTo, probeMedia } from "../api.js";
import { state, setCredits, track } from "../state.js";

const TRANSITIONS = [["keep", "AI"], ["cut", "Cut"], ["flash", "Flash"], ["dip_black", "Dip"], ["blur", "Blur"], ["glitch", "Glitch"]];
const SPEEDS = [[null, "AI"], [0.5, "0.5×"], [0.75, "0.75×"], [1, "1×"], [1.5, "1.5×"], [2, "2×"], [3, "3×"]];
const FILTERS = [["none", "None"], ["vivid", "Vivid"], ["punch", "Punch"], ["mono", "Mono"], ["fade", "Fade"]];
const PX_PER_S = 64;
const TR_SHORT = { flash: "FL", dip_black: "DIP", blur: "BL", glitch: "GL" };

export async function render(ctx) {
  const id = ctx.params.id;
  let st;
  try { st = await api(`/api/projects/${id}/editor`); }
  catch (e) {
    toast(e.message, "err");
    setTimeout(() => ctx.navigate(`/projects/${id}`, { replace: true }), 0);
    return h("div");
  }
  track("editor_opened");
  const project = st.project;
  const fileUrl = (fid) => project.file_urls && project.file_urls[fid];
  const files = project.files.filter((f) => f.uploaded);
  const fileDur = Object.fromEntries(files.map((f) => [f.id, f.duration_s || 0]));
  const base = st.shots;  // metadados dos planos da timeline atual (ficheiro, início na fonte, transição)
  const initial = JSON.stringify(st.doc);
  let doc = JSON.parse(initial);
  const undoStack = [], redoStack = [];
  let sel = 0, tool = null, playing = false, time = 0, raf = 0;

  // -------------------------------------------------------------- modelo
  const srcFile = (s) => s.file || (s.orig != null ? base[s.orig].file : null);
  const srcStart = (s) => (s.src_start_s != null ? s.src_start_s : (s.orig != null ? base[s.orig].src_start_s : 0) + (s.trim_in_s || 0));
  const speedOf = (s) => s.speed || 1;
  const total = () => doc.shots.reduce((a, s) => a + s.duration_s, 0);
  const startOf = (i) => doc.shots.slice(0, i).reduce((a, s) => a + s.duration_s, 0);
  const changed = () => JSON.stringify(doc) !== initial;
  function commit(mut) {
    undoStack.push(JSON.stringify(doc));
    if (undoStack.length > 100) undoStack.shift();
    redoStack.length = 0;
    mut();
    sel = Math.max(0, Math.min(sel, doc.shots.length - 1));
    time = Math.min(time, total());
    paint();
  }
  function undo() { if (!undoStack.length) return; redoStack.push(JSON.stringify(doc)); doc = JSON.parse(undoStack.pop()); sel = Math.min(sel, doc.shots.length - 1); paint(); }
  function redo() { if (!redoStack.length) return; undoStack.push(JSON.stringify(doc)); doc = JSON.parse(redoStack.pop()); paint(); }

  // ------------------------------------------------------- pré-visualização
  const finalVideo = h("video", { src: project.video_url, playsinline: true, muted: false, preload: "auto" });
  const roughVideo = h("video", { playsinline: true, muted: true, preload: "auto", crossorigin: "anonymous" });
  const textLayer = h("div", { style: { position: "absolute", inset: 0, pointerEvents: "none" } });
  const frame = h("div", { class: "phone-frame" }, finalVideo, roughVideo, textLayer);
  fitFrame(frame, project.resolution);
  const note = h("span", { class: "badge note hidden", text: t("Preview without effects — export to see the final video.") });
  const timeLabel = h("span", { class: "ed-time mono" });
  const playBtn = h("button", { class: "btn btn-icon", "aria-label": "Play" }, icon("play"));

  function shotAt(tt) {
    let acc = 0;
    for (let i = 0; i < doc.shots.length; i++) { if (tt < acc + doc.shots[i].duration_s) return [i, tt - acc]; acc += doc.shots[i].duration_s; }
    return [doc.shots.length - 1, doc.shots[doc.shots.length - 1].duration_s];
  }
  let roughShot = -1;
  function seekRough(tt) {
    const [i, off] = shotAt(tt);
    const s = doc.shots[i];
    const url = fileUrl(srcFile(s));
    if (!url) return;
    if (roughVideo.dataset.src !== url) { roughVideo.src = url; roughVideo.dataset.src = url; }
    const z = s.zoom || 1, fl = s.flip ? -1 : 1;
    roughVideo.style.transform = `scale(${fl * z}, ${z})`;
    roughVideo.playbackRate = Math.max(0.25, Math.min(4, speedOf(s)));
    const target = srcStart(s) + off * speedOf(s);
    if (Math.abs(roughVideo.currentTime - target) > 0.15 || roughShot !== i) roughVideo.currentTime = target;
    roughShot = i;
  }
  function paintTexts() {
    textLayer.replaceChildren(...doc.texts.filter((x) => time >= x.start_s && time < x.end_s).map((x) =>
      h("div", { class: "text-preview", style: { top: `${x.y * 100}%`, fontSize: `${x.size * frame.clientHeight}px`, transform: "translateY(-50%)" }, text: x.text })));
  }
  function usingFinal() { return !changed(); }
  function syncMode() {
    const fin = usingFinal();
    finalVideo.style.display = fin ? "" : "none";
    roughVideo.style.display = fin ? "none" : "";
    textLayer.style.display = fin ? "none" : "";
    note.classList.toggle("hidden", fin);
  }
  function setTime(tt, { fromPlayback } = {}) {
    time = Math.max(0, Math.min(tt, total()));
    if (usingFinal()) { if (!fromPlayback) finalVideo.currentTime = time; }
    else if (!fromPlayback) seekRough(time);
    paintTexts();
    paintPlayhead();
    timeLabel.textContent = `${fmtDur(time)} / ${fmtDur(total())}`;
  }
  function loop() {
    if (!playing) return;
    if (usingFinal()) {
      time = finalVideo.currentTime;
      if (finalVideo.ended) return pause();
    } else {
      const [i] = shotAt(time);
      const s = doc.shots[i];
      time = startOf(i) + Math.max(0, (roughVideo.currentTime - srcStart(s)) / speedOf(s));
      if (time >= startOf(i) + s.duration_s - 0.02) {
        if (i >= doc.shots.length - 1) return pause();
        time = startOf(i + 1);
        seekRough(time);
      }
    }
    setTime(time, { fromPlayback: true });
    const [ci] = shotAt(time);
    if (ci !== sel && !tool) { sel = ci; paintTrackSel(); }
    raf = requestAnimationFrame(loop);
  }
  function play() {
    if (time >= total() - 0.05) setTime(0);
    playing = true;
    playBtn.replaceChildren(icon("pause"));
    const v = usingFinal() ? finalVideo : roughVideo;
    if (!usingFinal()) seekRough(time);
    v.play().catch(() => {});
    raf = requestAnimationFrame(loop);
  }
  function pause() {
    playing = false;
    cancelAnimationFrame(raf);
    finalVideo.pause(); roughVideo.pause();
    playBtn.replaceChildren(icon("play"));
  }
  playBtn.onclick = () => (playing ? pause() : play());

  // --------------------------------------------------------------- timeline
  const trackEl = h("div", { class: "track", role: "listbox", "aria-label": "Timeline" });
  const playhead = h("div", { class: "playhead" });
  const timelineEl = h("div", { class: "timeline" }, trackEl, playhead);
  const thumbCache = new Map();
  const grabbers = new Map();
  function grabber(fid) {
    if (!grabbers.has(fid)) {
      const v = document.createElement("video");
      v.muted = true; v.preload = "auto"; v.crossOrigin = "anonymous"; v.src = fileUrl(fid) || "";
      grabbers.set(fid, { v, queue: [], busy: false });
    }
    return grabbers.get(fid);
  }
  function thumbnail(fid, at, canvas) {
    const key = `${fid}@${at.toFixed(1)}`;
    const draw = (img) => { const c = canvas.getContext("2d"); c.drawImage(img, 0, 0, canvas.width, canvas.height); };
    if (thumbCache.has(key)) { const img = new Image(); img.onload = () => draw(img); img.src = thumbCache.get(key); return; }
    const g = grabber(fid);
    g.queue.push({ at, canvas, key });
    const next = () => {
      if (g.busy || !g.queue.length) return;
      g.busy = true;
      const job = g.queue.shift();
      const done = () => {
        try {
          const c = document.createElement("canvas"); c.width = 64; c.height = 112;
          c.getContext("2d").drawImage(g.v, 0, 0, 64, 112);
          const url = c.toDataURL("image/jpeg", 0.6);
          thumbCache.set(job.key, url);
          const img = new Image(); img.onload = () => job.canvas.getContext("2d").drawImage(img, 0, 0, job.canvas.width, job.canvas.height); img.src = url;
        } catch {}
        g.busy = false; next();
      };
      const seek = () => { g.v.onseeked = done; g.v.currentTime = Math.max(0, job.at + 0.05); };
      if (g.v.readyState >= 1) seek(); else g.v.onloadedmetadata = seek;
      setTimeout(() => { if (g.busy) { g.busy = false; next(); } }, 4000);
    };
    next();
  }
  function paintTrack() {
    trackEl.replaceChildren(...doc.shots.map((s, i) => {
      const cv = h("canvas", { width: 64, height: 112 });
      const tr = i > 0 ? (s.transition === "keep" ? (base[s.orig] || {}).transition : s.transition) : null;
      const el = h("div", { class: `shot ${i === sel ? "sel" : ""}`, role: "option", "aria-selected": String(i === sel), tabindex: 0,
        style: { width: `${Math.max(34, s.duration_s * PX_PER_S)}px` }, dataset: { i } },
        cv, tr && tr !== "cut" ? h("span", { class: "tr", title: tr, text: TR_SHORT[tr] || tr.slice(0, 2) }) : null,
        h("span", { class: "d mono", text: `${s.duration_s.toFixed(1)}s` }));
      const fid = srcFile(s);
      if (fid && fileUrl(fid)) thumbnail(fid, srcStart(s), cv);
      attachDrag(el, i);
      return el;
    }));
    paintPlayhead();
  }
  function paintTrackSel() {
    [...trackEl.children].forEach((c, i) => { c.classList.toggle("sel", i === sel); c.setAttribute("aria-selected", String(i === sel)); });
  }
  function paintPlayhead() {
    const tiles = [...trackEl.children];
    if (!tiles.length) return;
    const [i, off] = shotAt(time);
    const tile = tiles[i];
    if (!tile) return;
    const x = tile.offsetLeft - trackEl.scrollLeft + (off / doc.shots[i].duration_s) * tile.offsetWidth + 12;
    playhead.style.left = `${x}px`;
    if (playing && (x < 30 || x > trackEl.clientWidth - 30)) trackEl.scrollLeft = tile.offsetLeft - 40;
  }
  trackEl.addEventListener("scroll", paintPlayhead, { passive: true });

  // arrastar para reordenar: rato = arrastar logo; toque = toque longo (não estraga o scroll)
  function attachDrag(el, i) {
    let startX = 0, dragging = false, timer = null, pid = null, target = null;
    el.addEventListener("pointerdown", (e) => {
      startX = e.clientX; pid = e.pointerId;
      if (e.pointerType === "mouse") return;
      timer = setTimeout(() => { dragging = true; el.classList.add("dragging"); el.setPointerCapture(pid); navigator.vibrate && navigator.vibrate(15); }, 320);
    });
    el.addEventListener("pointermove", (e) => {
      if (e.pointerType === "mouse" && e.buttons === 1 && !dragging && Math.abs(e.clientX - startX) > 6) {
        dragging = true; el.classList.add("dragging"); el.setPointerCapture(pid);
      }
      if (!dragging) { if (Math.abs(e.clientX - startX) > 8) clearTimeout(timer); return; }
      e.preventDefault();
      const tiles = [...trackEl.children];
      tiles.forEach((c) => c.classList.remove("drop-before"));
      target = tiles.findIndex((c) => e.clientX < c.getBoundingClientRect().left + c.offsetWidth / 2);
      if (target === -1) target = tiles.length;
      if (tiles[target]) tiles[target].classList.add("drop-before");
    });
    const end = () => {
      clearTimeout(timer);
      if (dragging && target != null && target !== i && target !== i + 1) {
        commit(() => { const [m] = doc.shots.splice(i, 1); doc.shots.splice(target > i ? target - 1 : target, 0, m); sel = target > i ? target - 1 : target; });
      } else if (dragging) paintTrack();
      dragging = false; target = null;
    };
    el.addEventListener("pointerup", (e) => {
      if (!dragging) { sel = i; pause(); setTime(startOf(i) + 0.01); paintTrackSel(); paintPanel(); }
      end();
    });
    el.addEventListener("pointercancel", end);
    el.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  // ------------------------------------------------------------- ferramentas
  const panel = h("div", { class: "panel hidden" });
  const toolsEl = h("div", { class: "ed-tools", role: "toolbar" });
  function toolBtn(key, ic, label, onclick) {
    return h("button", { class: `tool ${tool === key ? "active" : ""}`, onclick: onclick || (() => { tool = tool === key ? null : key; pause(); paintTools(); paintPanel(); }) },
      icon(ic), h("span", { text: label }));
  }
  function paintTools() {
    fill(toolsEl,
      toolBtn("split", "split", t("Split"), splitShot),
      toolBtn("trim", "scissors", t("Trim")),
      toolBtn("speed", "speed", t("Speed")),
      toolBtn("transition", "layers", t("Transition")),
      toolBtn("zoom", "zoom", t("Zoom")),
      toolBtn("replace", "refresh", t("Replace")),
      toolBtn("dup", "copy", t("Duplicate"), () => commit(() => { doc.shots.splice(sel + 1, 0, { ...doc.shots[sel] }); sel += 1; })),
      toolBtn("del", "trash", t("Delete"), () => {
        if (doc.shots.length <= 1) return toast("Keep at least one clip", "err");
        commit(() => { doc.shots.splice(sel, 1); });
      }),
      h("span", { style: { width: "1px", background: "var(--line)", margin: "6px 4px" } }),
      toolBtn("text", "type", t("Text")),
      toolBtn("music", "music", t("Music")),
      toolBtn("filters", "sliders", t("Filters")),
      st.ai ? toolBtn("reset", "wand", t("Reset to AI version"), async () => {
        if (!(await confirmSheet({ title: t("Reset to AI version") + "?", body: "Your changes will be replaced by the AI's original edit.", ok: t("Reset to AI version") }))) return;
        commit(() => { doc = JSON.parse(JSON.stringify(st.ai.doc)); base.splice(0, base.length, ...st.ai.shots); });
      }) : null);
  }
  function splitShot() {
    const s = doc.shots[sel];
    const [i, off] = shotAt(time);
    const at = i === sel && off > 0.15 && off < s.duration_s - 0.15 ? off : s.duration_s / 2;
    if (s.duration_s < 0.3) return toast("This clip is too short to split", "err");
    commit(() => {
      const first = { ...s, duration_s: +at.toFixed(3) };
      const second = { ...s, duration_s: +(s.duration_s - at).toFixed(3), src_start_s: +(srcStart(s) + at * speedOf(s)).toFixed(3), trim_in_s: 0, transition: "keep" };
      if (first.src_start_s == null) delete first.src_start_s;
      doc.shots.splice(sel, 1, first, second);
      sel += 1;
    });
  }
  const label = (txt) => h("span", { class: "subtle", style: { fontWeight: 600 }, text: txt });
  function chipRow(items, current, onPick) {
    return h("div", { class: "seg" }, items.map(([v, lab]) => h("button", { class: "chip", "aria-pressed": String(v === current), onclick: () => onPick(v) }, lab)));
  }
  function slider(min, max, step, value, onInput, onChange) {
    return h("input", { class: "range", type: "range", min, max, step, value, oninput: (e) => onInput(Number(e.target.value)), onchange: onChange ? (e) => onChange(Number(e.target.value)) : null });
  }
  function paintPanel() {
    if (!tool) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");
    const s = doc.shots[sel];
    const close = h("button", { class: "btn btn-icon btn-sm btn-ghost", "aria-label": "Close", onclick: () => { tool = null; paintTools(); paintPanel(); } }, icon("x", "icon-sm"));
    const headRow = (title) => h("div", { class: "panel-head" }, h("h3", { text: title }), close);
    let body = [];
    if (tool === "trim") {
      const fd = fileDur[srcFile(s)] || 0;
      const start0 = srcStart(s);
      const maxDur = Math.max(0.2, fd ? (fd - start0) / speedOf(s) : s.duration_s * 2);
      const durOut = h("span", { class: "mono", text: `${s.duration_s.toFixed(2)}s` });
      let pending = null;
      body = [headRow(t("Trim")),
        label("Duration"), h("div", { class: "row" }, slider(0.1, Math.min(10, +maxDur.toFixed(2)), 0.05, s.duration_s,
          (v) => { durOut.textContent = `${v.toFixed(2)}s`; pending = v; }, () => commit(() => { doc.shots[sel].duration_s = +pending.toFixed(3); })), durOut),
        label("Start in clip"), h("div", { class: "row" }, slider(0, Math.max(0.1, +(fd || start0 + 2).toFixed(2)), 0.05, start0,
          () => {}, (v) => commit(() => { const x = doc.shots[sel]; x.src_start_s = +v.toFixed(3); x.trim_in_s = 0; })),
          h("span", { class: "mono", text: `${start0.toFixed(2)}s` }))];
    } else if (tool === "speed") {
      body = [headRow(t("Speed")), chipRow(SPEEDS, s.speed ?? null, (v) => commit(() => { doc.shots[sel].speed = v; })),
        h("p", { class: "subtle", text: "\"AI\" keeps the speed ramps the AI copied from your reference." })];
    } else if (tool === "transition") {
      body = [headRow(t("Transition")), sel === 0 ? h("p", { class: "subtle", text: "The first clip has no transition in." })
        : chipRow(TRANSITIONS, s.transition || "keep", (v) => commit(() => { doc.shots[sel].transition = v; }))];
    } else if (tool === "zoom") {
      const zOut = h("span", { class: "mono", text: `${(s.zoom || 1).toFixed(2)}×` });
      body = [headRow(t("Zoom")),
        h("div", { class: "row" }, slider(1, 3, 0.05, s.zoom || 1, (v) => { zOut.textContent = `${v.toFixed(2)}×`; },
          (v) => commit(() => { doc.shots[sel].zoom = v; })), zOut),
        h("div", { class: "row-wrap" },
          h("button", { class: "chip", "aria-pressed": String(!!s.flip), onclick: () => commit(() => { doc.shots[sel].flip = !s.flip; }) }, icon("flip", "icon-sm"), "Flip"),
          h("button", { class: "chip", onclick: () => commit(() => { const x = doc.shots[sel]; x.zoom = null; x.center = null; x.flip = null; }) }, "AI framing"))];
    } else if (tool === "replace") {
      const vids = files.filter((f) => f.kind === "source");
      body = [headRow(t("Replace")), h("p", { class: "subtle", text: "Use another of your clips for this shot." }),
        h("div", { class: "files" }, vids.map((f) => {
          const on = srcFile(s) === f.id;
          return h("button", { class: "file-tile", style: { border: on ? "2px solid var(--accent)" : null, padding: 0, cursor: "pointer" },
            onclick: () => commit(() => { const x = doc.shots[sel]; x.file = f.id; x.src_start_s = 0; x.trim_in_s = 0; }) },
            fileUrl(f.id) ? h("video", { src: `${fileUrl(f.id)}#t=0.5`, muted: true, playsinline: true, preload: "metadata" }) : null,
            h("div", { class: "info", text: f.name }));
        }))];
    } else if (tool === "text") {
      body = [headRow(t("Text")),
        ...doc.texts.map((x, k) => {
          const inp = h("input", { class: "input", value: x.text, maxlength: 80, onchange: (e) => commit(() => { doc.texts[k].text = e.target.value || "TEXT"; }) });
          return h("div", { class: "card card-tight stack-sm" }, h("div", { class: "row" }, inp,
            h("button", { class: "btn btn-icon btn-sm btn-ghost", "aria-label": t("Delete"), onclick: () => commit(() => { doc.texts.splice(k, 1); }) }, icon("trash", "icon-sm"))),
            h("div", { class: "row" }, label(`${fmtDur(x.start_s)}–${fmtDur(x.end_s)}`),
              h("button", { class: "chip", onclick: () => commit(() => { const d = x.end_s - x.start_s; doc.texts[k].start_s = +time.toFixed(2); doc.texts[k].end_s = +Math.min(total(), time + d).toFixed(2); }) }, "Start at playhead")),
            chipRow([[0.2, "Top"], [0.5, "Middle"], [0.82, "Bottom"]], x.y, (v) => commit(() => { doc.texts[k].y = v; })),
            chipRow([["pop", "Pop"], ["fade", "Fade"], ["none", "None"]], x.entry, (v) => commit(() => { doc.texts[k].entry = v; })));
        }),
        doc.texts.length < 20 ? h("button", { class: "btn", onclick: () => commit(() => {
          doc.texts.push({ text: "YOUR TEXT", start_s: +time.toFixed(2), end_s: +Math.min(total(), time + 2).toFixed(2), x: 0.5, y: 0.82, size: 0.06, entry: "pop" });
        }) }, icon("plus", "icon-sm"), "Add text at playhead") : null];
    } else if (tool === "music") {
      const vOut = h("span", { class: "mono", text: `${Math.round(doc.music_volume * 100)}%` });
      const music = files.filter((f) => f.kind === "music");
      const upInput = h("input", { type: "file", accept: "audio/*", class: "sr-only", onchange: (e) => uploadMusic(e.target.files[0]) });
      body = [headRow(t("Music")), label(t("Volume")),
        h("div", { class: "row" }, slider(0, 2, 0.05, doc.music_volume, (v) => { vOut.textContent = `${Math.round(v * 100)}%`; },
          (v) => commit(() => { doc.music_volume = v; })), vOut),
        chipRow([[null, "Original audio"], ...music.map((m) => [m.id, m.name])], doc.music_file ?? null,
          (v) => commit(() => { doc.music_file = v; })),
        h("label", { class: "btn btn-sm", style: { justifySelf: "start" } }, upInput, icon("upload", "icon-sm"), "Upload music")];
    } else if (tool === "filters") {
      const lk = doc.look;
      const sl = (key, lab) => h("div", { class: "stack-sm" }, label(lab), slider(-1, 1, 0.05, lk[key], () => {}, (v) => commit(() => { doc.look[key] = v; })));
      body = [headRow(t("Filters")), chipRow(FILTERS, lk.filter, (v) => commit(() => { doc.look.filter = v; })),
        sl("brightness", "Brightness"), sl("contrast", "Contrast"), sl("saturation", "Saturation"),
        h("p", { class: "subtle", text: "Filters show up in the exported video." })];
    }
    panel.replaceChildren(...body.filter(Boolean));
  }
  async function uploadMusic(file) {
    if (!file) return;
    try {
      const meta = await probeMedia(file);
      const r = await api(`/api/projects/${id}/files`, { method: "POST", body: { kind: "music", name: file.name, size: file.size, duration_s: meta.duration || 0 } });
      toast("Uploading music…");
      await uploadTo(r.upload, file);
      const done = await api(`/api/projects/${id}/files/${r.file.id}/complete`, { method: "POST" });
      project.files = done.project.files; project.file_urls = done.project.file_urls;
      files.splice(0, files.length, ...project.files.filter((f) => f.uploaded));
      commit(() => { doc.music_file = r.file.id; });
    } catch (e) { toast(e.message, "err"); }
  }

  // ---------------------------------------------------------------- export
  const exportBtn = h("button", { class: "btn btn-primary btn-sm" }, t("Export"));
  exportBtn.onclick = async () => {
    if (!changed()) return toast("No changes to export yet");
    pause();
    setBusy(exportBtn, true);
    try {
      const r = await api(`/api/projects/${id}/edit`, { method: "POST", headers: { "Idempotency-Key": uid() }, body: { doc } });
      setCredits(r.credits);
      ctx.navigate(`/projects/${id}`, { replace: true });
    } catch (e) { toast(e.message, "err"); setBusy(exportBtn, false); }
  };
  const undoBtn = h("button", { class: "btn btn-icon btn-sm btn-ghost", "aria-label": t("Undo"), onclick: undo }, icon("undo"));
  const redoBtn = h("button", { class: "btn btn-icon btn-sm btn-ghost", "aria-label": t("Redo"), onclick: redo }, icon("redo"));

  function paint() {
    undoBtn.disabled = !undoStack.length;
    redoBtn.disabled = !redoStack.length;
    exportBtn.disabled = !changed();
    syncMode();
    paintTrack();
    paintTools();
    paintPanel();
    setTime(time);
  }

  // ---------------------------------------------------------------- teclado
  const onKey = (e) => {
    if (e.target.closest("input,textarea")) return;
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === " ") { e.preventDefault(); playing ? pause() : play(); }
    else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
    else if (e.key === "Delete" || e.key === "Backspace") { if (doc.shots.length > 1) commit(() => doc.shots.splice(sel, 1)); }
    else if (e.key.toLowerCase() === "s" && !mod) splitShot();
    else if (e.key === "ArrowRight") { sel = Math.min(doc.shots.length - 1, sel + 1); setTime(startOf(sel) + 0.01); paintTrackSel(); paintPanel(); }
    else if (e.key === "ArrowLeft") { sel = Math.max(0, sel - 1); setTime(startOf(sel) + 0.01); paintTrackSel(); paintPanel(); }
  };
  document.addEventListener("keydown", onKey);
  const onLeave = (e) => { if (changed()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", onLeave);

  const back = h("a", { class: "btn btn-icon btn-sm btn-ghost", href: `/projects/${id}`, "aria-label": t("Back"), onclick: async (e) => {
    if (!changed()) return;
    e.preventDefault(); e.stopPropagation();
    if (await confirmSheet({ title: "Discard changes?", body: "Your edits haven't been exported.", ok: "Discard", danger: true })) {
      window.removeEventListener("beforeunload", onLeave);
      ctx.navigate(`/projects/${id}`);
    }
  } }, icon("left"));

  const el = h("div", { class: "editor" },
    h("header", { class: "ed-top" }, back, h("span", { class: "title", text: project.name }), undoBtn, redoBtn, exportBtn),
    h("div", { class: "ed-stage" }, note, frame),
    h("div", { class: "ed-bottom" }, h("div", { class: "ed-controls" }, playBtn, timeLabel), timelineEl, toolsEl),
    h("aside", { class: "ed-side" }, panel));
  finalVideo.addEventListener("ended", pause);
  requestAnimationFrame(paint);
  return { el, title: t("Edit"), cleanup: () => { pause(); document.removeEventListener("keydown", onKey); window.removeEventListener("beforeunload", onLeave); grabbers.forEach((g) => { g.v.src = ""; }); } };
}
