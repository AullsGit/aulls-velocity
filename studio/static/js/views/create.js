// Criar a partir de referência: 1 Os teus vídeos · 2 Referência · 3 Análise da IA · 4 Gerar.
import { h, icon, toast, fmtDur, fmtBytes, setBusy, confirmSheet } from "../ui.js";
import { t } from "../i18n.js";
import { api, uploadTo, probeMedia, pollJob, uid } from "../api.js";
import { state, setCredits, costFor } from "../state.js";
import { openBuySheet } from "./buy.js";

const MAX_PARALLEL = 3;

export async function render(ctx) {
  const me = state.me;
  const L = me.limits;
  let project = null;
  if (ctx.params.id) {
    try { project = (await api(`/api/projects/${ctx.params.id}`)).project; }
    catch { ctx.navigate("/create", { replace: true }); return h("div"); }
    if (project.status === "processing") { ctx.navigate(`/projects/${project.id}`, { replace: true }); return h("div"); }
  }
  const local = [];  // uploads em curso / falhados (ainda não confirmados no projeto)
  const thumbs = new Map();  // id do ficheiro -> miniatura gerada no browser
  let step = initialStep();
  let profile = (project && project.settings && project.settings.profile) || "standard";
  let force = false;
  let textLines = "";
  let stopAnalysis = () => {};
  let idemKey = uid();
  let creating = null;
  const el = h("div", { class: "wizard" });
  const mainCol = h("div");
  const side = h("aside", { class: "hide-mobile" });
  el.append(mainCol, side);

  function files(kind) { return ((project && project.files) || []).filter((f) => f.kind === kind && f.uploaded); }
  function initialStep() {
    if (!project) return 1;
    if (!files("source").length) return 1;
    if (!files("reference").length) return 2;
    if (!project.reference_summary) return 3;
    return 4;
  }
  function busyUploads(kind) { return local.some((u) => u.kind === kind && !u.error); }

  async function ensureProject() {
    if (project) return project;
    if (!creating) creating = api("/api/projects", { method: "POST", body: {} }).then((r) => {
      project = r.project;
      history.replaceState({}, "", `/create/${project.id}`);
      return project;
    });
    return creating;
  }

  // --------------------------------------------------------------- uploads
  const queue = [];
  let running = 0;
  function pump() {
    while (running < MAX_PARALLEL && queue.length) {
      const u = queue.shift();
      running++;
      doUpload(u).finally(() => { running--; pump(); });
    }
  }
  async function addFiles(fileList, kind) {
    const arr = [...fileList];
    if (kind !== "source") arr.splice(1);
    for (const file of arr) {
      const u = { id: uid(), file, kind, progress: 0, error: null, thumb: null, duration: null };
      const ext = "." + (file.name.split(".").pop() || "").toLowerCase();
      const okExt = kind === "music" ? L.audio_ext : kind === "reference" ? L.video_ext : [...L.video_ext, ...L.image_ext];
      if (!okExt.includes(ext)) { toast(`${file.name}: format not supported`, "err"); continue; }
      if (file.size > L.max_upload_mb * 1024 * 1024) { toast(`${file.name}: must be under ${L.max_upload_mb} MB`, "err"); continue; }
      if (kind === "source" && files("source").length + local.filter((x) => x.kind === "source" && !x.error).length >= L.max_files) {
        toast(`Up to ${L.max_files} files`, "err"); break;
      }
      local.push(u);
      queue.push(u);
    }
    paint();
    pump();
  }
  async function doUpload(u) {
    try {
      const meta = await probeMedia(u.file);
      u.thumb = meta.thumb; u.duration = meta.duration;
      if (u.kind === "reference" && meta.duration > L.max_reference_s) throw new Error(`The reference must be under ${L.max_reference_s}s`);
      if (u.kind === "source" && meta.duration && meta.duration > L.max_source_s) throw new Error(`Each video must be under ${Math.round(L.max_source_s / 60)} min`);
      paint();
      const p = await ensureProject();
      const r = await api(`/api/projects/${p.id}/files`, { method: "POST", body: {
        kind: u.kind, name: u.file.name, size: u.file.size, duration_s: meta.duration || 0, width: meta.width, height: meta.height } });
      await uploadTo(r.upload, u.file, (f) => { u.progress = f; paintTile(u); });
      const done = await api(`/api/projects/${p.id}/files/${r.file.id}/complete`, { method: "POST" });
      project = done.project;
      if (u.thumb) thumbs.set(r.file.id, u.thumb);
      local.splice(local.indexOf(u), 1);
      if (u.kind === "reference" && done.job) watchAnalysis(done.job.id);
      paint();
    } catch (e) {
      u.error = e.message || "Upload failed";
      paint();
    }
  }
  async function removeFile(f) {
    try { project = (await api(`/api/projects/${project.id}/files/${f.id}`, { method: "DELETE" })).project; paint(); }
    catch (e) { toast(e.message, "err"); }
  }

  // ------------------------------------------------------------- análise
  function watchAnalysis(jobId) {
    stopAnalysis();
    stopAnalysis = pollJob(jobId, async (job) => {
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(job.status)) {
        project = (await api(`/api/projects/${project.id}`)).project;
        if (job.status !== "COMPLETED") project._analysisFailed = true;
        paint();
      }
    }, { min: 1500, max: 4000 });
  }
  if (project && files("reference").length && !project.reference_summary && project.settings && project.settings.analysis_job && !project.settings.analysis_error) {
    watchAnalysis(project.settings.analysis_job);
  }

  // ----------------------------------------------------------- componentes
  function preview(f) {
    // miniatura gerada no browser durante o upload; ao reabrir o projeto, o 1º frame do próprio vídeo
    const thumb = thumbs.get(f.id);
    if (thumb) return h("img", { src: thumb, alt: "" });
    const url = project && project.file_urls && project.file_urls[f.id];
    const isImg = /\.(jpe?g|png|webp)$/i.test(f.name || "");
    if (url && isImg) return h("img", { src: url, alt: "", loading: "lazy" });
    if (url && f.kind !== "music") return h("video", { src: `${url}#t=0.5`, muted: true, playsinline: true, preload: "metadata" });
    return h("div", { class: "ph", style: { display: "grid", placeItems: "center", height: "100%" } }, icon(f.kind === "music" ? "music" : "film"));
  }
  function tile(f, { remove } = {}) {
    return h("div", { class: "file-tile" },
      preview(f),
      h("div", { class: "info", text: [f.duration_s || f.duration ? fmtDur(f.duration_s || f.duration) : "", fmtBytes(f.size)].filter(Boolean).join(" · ") }),
      remove ? h("button", { class: "rm", "aria-label": "Remove", onclick: remove }, icon("x", "icon-sm")) : null);
  }
  function localTile(u) {
    const bar = h("div", { class: "bar" }, h("i", { style: { width: `${Math.round(u.progress * 100)}%` } }));
    const el2 = h("div", { class: `file-tile ${u.error ? "err" : ""}`, dataset: { up: u.id } },
      u.thumb ? h("img", { src: u.thumb, alt: "" }) : h("div", { style: { display: "grid", placeItems: "center", height: "100%" } }, h("span", { class: "spinner" })),
      u.error ? null : bar,
      h("div", { class: "info", text: u.error || `${Math.round(u.progress * 100)}%` }),
      h("button", { class: "rm", "aria-label": "Remove", onclick: () => { local.splice(local.indexOf(u), 1); paint(); } }, icon("x", "icon-sm")));
    return el2;
  }
  function paintTile(u) {
    const node = el.querySelector(`[data-up="${u.id}"]`);
    if (!node) return;
    const bar = node.querySelector(".bar > i");
    if (bar) bar.style.width = `${Math.round(u.progress * 100)}%`;
    node.querySelector(".info").textContent = `${Math.round(u.progress * 100)}%`;
  }
  function dropzone(kind, { multiple, accept, title, hint }) {
    const input = h("input", { type: "file", accept, multiple: multiple || null, class: "sr-only", onchange: (e) => { addFiles(e.target.files, kind); e.target.value = ""; } });
    const cam = h("input", { type: "file", accept: "video/*", capture: "environment", class: "sr-only", onchange: (e) => { addFiles(e.target.files, kind); e.target.value = ""; } });
    const zone = h("label", { class: "dropzone" }, input,
      h("div", { class: "ico" }, icon(kind === "music" ? "music" : "upload", "icon-lg")),
      h("strong", { class: "hide-mobile", text: title }), h("strong", { class: "hide-desktop", text: t("Tap to add videos") }),
      h("span", { class: "subtle", text: hint }));
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => addFiles(e.dataTransfer.files, kind));
    return h("div", {}, zone, kind !== "music" ? h("div", { class: "upload-actions" },
      h("button", { class: "btn", type: "button", onclick: () => input.click() }, icon("image", "icon-sm"), t("Gallery")),
      h("button", { class: "btn", type: "button", onclick: () => cam.click() }, icon("camera", "icon-sm"), t("Camera")), cam) : null);
  }

  // ---------------------------------------------------------------- passos
  function stepVideos() {
    const srcs = files("source");
    const list = local.filter((u) => u.kind === "source");
    const total = srcs.reduce((a, f) => a + (f.duration_s || 0), 0);
    return [
      head(1, t("Add the clips you want in your edit"), t("Videos or photos. More material gives the AI more to work with.")),
      dropzone("source", { multiple: true, accept: "video/*,image/*", title: t("Drop videos here or click to browse"),
        hint: `MP4, MOV, JPG, PNG · up to ${L.max_upload_mb} MB each · ${L.max_files} files` }),
      srcs.length || list.length ? h("div", { class: "files" }, srcs.map((f) => tile(f, { remove: () => removeFile(f) })), list.map(localTile)) : null,
      srcs.length ? h("p", { class: "subtle", style: { marginTop: "10px" }, text: `${srcs.length} ${srcs.length === 1 ? "file" : "files"} · ${fmtDur(total)}` }) : null,
      cta(srcs.length > 0 && !busyUploads("source"), () => go(files("reference").length ? 3 : 2)),
    ];
  }
  function stepReference() {
    const ref = files("reference")[0];
    const up = local.find((u) => u.kind === "reference");
    return [
      head(2, t("Add the reference edit"), t("The video whose style you want: cuts, rhythm, zooms, transitions. Only the style is used — never its content.")),
      ref || up ? h("div", { class: "ref-preview" }, ref ? tile(ref) : localTile(up),
        h("div", { class: "stack-sm" }, h("strong", { text: (ref && ref.name) || (up && up.file.name) }),
          ref ? h("span", { class: "subtle", text: [fmtDur(ref.duration_s), fmtBytes(ref.size)].filter(Boolean).join(" · ") }) : null,
          ref ? h("label", { class: "btn btn-sm", style: { justifySelf: "start" } },
            h("input", { type: "file", accept: "video/*", class: "sr-only", onchange: (e) => addFiles(e.target.files, "reference") }),
            icon("refresh", "icon-sm"), t("Replace")) : null))
        : dropzone("reference", { accept: "video/*", title: t("Add a reference video"), hint: `Up to ${L.max_reference_s}s · ${L.max_upload_mb} MB` }),
      cta(Boolean(ref) && !up, () => go(3)),
    ];
  }
  function stepAnalysis() {
    const s = project && project.reference_summary;
    const failed = project && ((project.settings && project.settings.analysis_error) || project._analysisFailed);
    let body;
    if (s) {
      body = h("div", { class: "stack" },
        h("div", { class: "row" }, h("span", { class: "badge badge-ok" }, icon("check", "icon-sm"), t("Style identified"))),
        h("div", { class: "style-tags" }, (s.tags || []).map((tag) => h("span", { class: "tag", text: tag }))),
        h("div", { class: "stat-row" },
          h("div", { class: "stat" }, h("b", { class: "mono", text: s.n_shots }), h("span", { text: t("shots") })),
          h("div", { class: "stat" }, h("b", { class: "mono", text: s.cuts_per_second }), h("span", { text: t("cuts/sec") })),
          h("div", { class: "stat" }, h("b", { class: "mono", text: s.bpm || "—" }), h("span", { text: t("BPM") }))),
        s.is_velocity ? null : h("div", { class: "notice warn" }, icon("alert", "icon-sm"),
          h("span", { text: t("This reference has few clear cuts or effects. The result may be simpler than expected.") })));
      force = !s.is_velocity;
    } else if (failed) {
      body = h("div", { class: "error-card" }, icon("alert"), h("p", { text: "We couldn't read this reference. Try another video." }),
        h("button", { class: "btn", onclick: () => go(2), text: t("Replace") }));
    } else {
      body = h("div", { class: "stack", style: { justifyItems: "center", textAlign: "center", padding: "26px 0" } },
        h("span", { class: "spinner", style: { width: "34px", height: "34px", color: "var(--accent)" } }),
        h("strong", { text: t("Analyzing your reference…") }),
        h("span", { class: "subtle", text: "Cuts · rhythm · transitions · zooms · effects · beat" }));
    }
    return [head(3, t("AI analysis"), ""), h("div", { class: "card" }, body), cta(Boolean(s), () => go(4))];
  }
  function stepGenerate() {
    const opts = [["fast", "Fast", "Quick preview quality"], ["standard", "Standard", "Best balance — recommended"], ["hd", "HD", "1080×1920, sharpest"]];
    const music = files("music")[0];
    const musicUp = local.find((u) => u.kind === "music");
    const cost = costFor(profile);
    const enough = (me.credits ?? 0) >= cost;
    const gen = h("button", { class: "btn btn-primary btn-lg", style: { flex: 1 } },
      icon("spark"), cost === 1 ? t("Generate — 1 credit") : t("Generate — {n} credits", { n: cost }));
    gen.onclick = () => generate(gen);
    return [
      head(4, t("Generate"), ""),
      h("div", { class: "stack" },
        h("div", { class: "stack-sm" }, h("h3", { text: t("Quality") }),
          h("div", { class: "quality", role: "radiogroup" }, opts.map(([k, label, hint]) => h("div", {
            class: "q-opt", role: "radio", tabindex: 0, "aria-checked": String(profile === k),
            onclick: () => { profile = k; paint(); }, onkeydown: (e) => { if (e.key === " " || e.key === "Enter") { profile = k; paint(); } },
          }, h("span", { class: "radio" }), h("div", {}, h("strong", { text: t(label) }), h("div", { class: "subtle", text: t(hint) })),
            h("span", { class: "cost mono", text: `${costFor(k)} ${costFor(k) === 1 ? t("credit") : t("credits")}` }))))),
        h("details", { class: "card card-tight" },
          h("summary", { style: { cursor: "pointer", fontWeight: 600 } }, t("Music (optional)")),
          h("div", { class: "stack-sm", style: { marginTop: "12px" } },
            h("p", { class: "subtle", text: t("Uses the reference's audio if you don't add one.") }),
            music ? h("div", { class: "spread" }, h("span", { text: music.name }), h("button", { class: "btn btn-sm", onclick: () => removeFile(music) }, icon("trash", "icon-sm")))
              : musicUp ? h("p", { class: "subtle", text: musicUp.error || `${Math.round(musicUp.progress * 100)}%` })
                : h("label", { class: "btn btn-sm", style: { justifySelf: "start" } },
                    h("input", { type: "file", accept: "audio/*", class: "sr-only", onchange: (e) => addFiles(e.target.files, "music") }),
                    icon("music", "icon-sm"), t("Add music")))),
        h("details", { class: "card card-tight" },
          h("summary", { style: { cursor: "pointer", fontWeight: 600 } }, t("Text on screen (optional)")),
          h("div", { class: "stack-sm", style: { marginTop: "12px" } },
            h("p", { class: "subtle", text: t("Only used where the reference shows text. One line per text.") }),
            h("textarea", { class: "textarea", rows: 2, maxlength: 200, placeholder: "NEVER STOP", oninput: (e) => { textLines = e.target.value; } }, textLines)))),
      h("div", { class: "sticky-cta" },
        h("button", { class: "btn btn-icon", "aria-label": t("Back"), onclick: () => go(3) }, icon("left")),
        enough ? gen : h("button", { class: "btn btn-primary btn-lg", style: { flex: 1 }, onclick: () => openBuySheet({
          reason: t("You need {n} more credits", { n: cost - (me.credits ?? 0) }), onPurchased: () => paint() }) }, icon("card"), t("Get credits"))),
      !enough ? h("p", { class: "subtle", style: { textAlign: "center", marginTop: "8px" }, text: `${t("Balance")}: ${me.credits ?? 0} · ${t("You need {n} more credits", { n: cost - (me.credits ?? 0) })}` }) : null,
    ];
  }

  async function generate(btn) {
    if (busyUploads("music")) { toast("Wait for the music to finish uploading", "err"); return; }
    setBusy(btn, true);
    try {
      const r = await api(`/api/projects/${project.id}/generate`, {
        method: "POST", headers: { "Idempotency-Key": idemKey },
        body: { profile, force, text: textLines.split("\n").map((s) => s.trim()).filter(Boolean) },
      });
      setCredits(r.credits);
      ctx.navigate(`/projects/${project.id}`, { replace: true });
    } catch (e) {
      setBusy(btn, false);
      idemKey = uid();
      if (e.code === "insufficient_credits") {
        openBuySheet({ reason: t("You need {n} more credits", { n: (e.extra.cost || 1) - (e.extra.balance || 0) }), onPurchased: () => paint() });
      } else toast(e.message, "err");
    }
  }

  function head(n, title, sub) {
    return h("div", { class: "step-head" },
      h("div", { class: "steps", "aria-hidden": "true" }, [1, 2, 3, 4].map((k) => h("i", { class: k < n ? "done" : k === n ? "now" : "" }))),
      h("span", { class: "step-kicker", text: t("Step {n} of {t}", { n, t: 4 }) }),
      h("h1", { text: title }), sub ? h("p", { class: "muted", text: sub }) : null);
  }
  function cta(enabled, onNext) {
    // só fica fixa no fundo quando dá para avançar; desativada não deve tapar a zona de upload
    return h("div", { class: "sticky-cta", style: enabled ? null : { position: "static" } },
      step > 1 ? h("button", { class: "btn btn-icon", "aria-label": t("Back"), onclick: () => go(step - 1) }, icon("left")) : null,
      h("button", { class: "btn btn-primary btn-lg", style: { flex: 1 }, disabled: !enabled, onclick: onNext }, t("Continue"), icon("right")));
  }
  function go(n) { step = n; paint(); window.scrollTo({ top: 0, behavior: "smooth" }); }

  function summary() {
    const ref = files("reference")[0];
    const srcs = files("source");
    return h("div", { class: "card summary stack" },
      h("h3", { text: (project && project.name) || t("Create video") }),
      h("div", {},
        h("div", { class: "line" }, h("span", { class: "muted", text: t("Your videos") }), h("span", { class: "mono", text: `${srcs.length} · ${fmtDur(srcs.reduce((a, f) => a + (f.duration_s || 0), 0))}` })),
        h("div", { class: "line" }, h("span", { class: "muted", text: t("Reference") }), h("span", { text: ref ? fmtDur(ref.duration_s) : "—" })),
        h("div", { class: "line" }, h("span", { class: "muted", text: t("Format") }), h("span", { text: "9:16 · 30 fps" })),
        h("div", { class: "line" }, h("span", { class: "muted", text: t("Balance") }), h("span", { class: "mono" }, h("span", { dataset: { credits: "" }, text: me.credits ?? 0 }), ` ${t("credits")}`))),
      project && project.reference_summary ? h("div", { class: "style-tags" }, (project.reference_summary.tags || []).slice(0, 4).map((x) => h("span", { class: "tag", text: x }))) : null);
  }

  function paint() {
    const parts = { 1: stepVideos, 2: stepReference, 3: stepAnalysis, 4: stepGenerate }[step]();
    mainCol.replaceChildren(...parts.filter(Boolean));
    side.replaceChildren(summary());
  }
  paint();

  const beforeUnload = (e) => { if (local.some((u) => !u.error)) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", beforeUnload);
  return { el, title: t("Create video"), cleanup: () => { stopAnalysis(); window.removeEventListener("beforeunload", beforeUnload); } };
}
