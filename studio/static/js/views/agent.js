// Conversa com o agente: é aqui que se edita. O utilizador escreve, junta ficheiros e vê o agente a
// trabalhar; o estado vem sempre do servidor (`seq` crescente), por isso sair e voltar não perde nada.
import { h, icon, fill, toast, fmtDur, fmtDate, menuSheet, confirmSheet, spinner } from "../ui.js";
import { t } from "../i18n.js";
import { api, uploadTo, probeMedia } from "../api.js";
import { state, setCredits, track } from "../state.js";
import { videoFormat, renameSheet } from "./common.js";
import { openBuySheet } from "./buy.js";
import { shareSheet } from "./project.js";

const TOOL_TEXT = {
  analyze_reference: "Studying the reference", analyze_clips: "Looking through your videos", extract_captions: "Reading the captions", analyze_audio: "Measuring the music",
  view_frames: "Looking at frames", compare_frames: "Comparing with the reference", build_edit: "Building the edit", get_plan: "Reviewing the plan",
  update_plan: "Adjusting the edit", render: "Rendering", verify_output: "Checking the result", present_version: "Preparing your video",
  restore_version: "Going back", remember: "Saving your preference", forget: "Forgetting a preference", set_file_role: "Organizing your files",
};
const STAGE_TEXT = { "na fila": "in the queue", "a analisar": "analyzing", "a preparar": "preparing", "a renderizar": "rendering", "a aplicar efeitos": "applying effects", "a terminar": "finishing", "feito": "done" };
const ROLE_ICON = { reference: "layers", source: "film", music: "music" };
const ROLE_TEXT = { reference: "Reference", source: "My video", music: "Music" };
const SUGGESTIONS = [
  "Recreate the style of this reference with my clips",
  "Use only the reference's music and captions. Don't touch my footage",
  "Cut my videos to the beat of this song",
  "Make it more dynamic, like a fast TikTok edit",
];
const POLL_ACTIVE = 900, POLL_HIDDEN = 2500;

// markdown mínimo e seguro: tudo entra como texto; negrito, listas e parágrafos viram elementos
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(tok.startsWith("**") ? h("strong", { text: tok.slice(2, -2) }) : h("code", { text: tok.slice(1, -1) }));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function richText(text) {
  const box = h("div", { class: "msg-text" });
  let list = null;
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trimEnd();
    const li = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      if (!list) { list = h(/^\s*\d/.test(line) ? "ol" : "ul"); box.append(list); }
      list.append(h("li", {}, inline(li[1])));
      continue;
    }
    list = null;
    if (line.trim()) box.append(h("p", {}, inline(line)));
  }
  return box;
}

export async function render(ctx) {
  const me = state.me;
  const L = me.limits;
  let pid = ctx.params.id || null;
  let project = null;
  let agentCfg = { enabled: true };
  let status = "idle";
  let lastRev = 0;
  let live = null;
  let versions = {};
  let timer = null;
  let stopped = false;
  const uploads = [];
  const nodes = new Map();  // seq -> {sig, el}
  let hasMessages = false;

  const log = h("div", { class: "agent-log", role: "log", "aria-live": "polite" });
  const liveBox = h("div", { class: "agent-live" });
  const scroller = h("div", { class: "agent-scroll" }, log, liveBox);
  const filesRow = h("div", { class: "agent-files" });
  const input = h("textarea", { class: "agent-input", rows: 1, maxlength: 8000, placeholder: t("Tell me what you want…"), "aria-label": t("Message") });
  const sendBtn = h("button", { class: "agent-send", "aria-label": t("Send") }, icon("right"));
  const attachBtn = h("button", { class: "agent-attach", "aria-label": t("Add files"), onclick: () => chooseRole() }, icon("plus"));
  const composer = h("div", { class: "agent-composer" }, filesRow, h("div", { class: "agent-box" }, attachBtn, input, sendBtn));
  const titleEl = h("button", { class: "agent-title", onclick: () => project && renameSheet(project, async () => { await refresh(); }) });
  const creditsEl = h("a", { class: "credits-pill", href: "/credits" }, h("span", { class: "mono" }, h("span", { dataset: { credits: "" }, text: me.credits ?? 0 }), ` ${t("credits")}`));
  const menuBtn = h("button", { class: "btn btn-icon btn-sm btn-ghost", "aria-label": t("Options"), onclick: () => openMenu() }, icon("more", "icon-sm"));
  const top = h("div", { class: "agent-top" }, titleEl, h("div", { class: "row" }, creditsEl, menuBtn));
  const el = h("div", { class: "agent" }, top, scroller, composer);

  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
  const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };

  // ---------------------------------------------------------------- estado vazio
  function emptyState(recent) {
    const tile = (kind, ic, title, sub) => h("button", { class: "agent-tile", onclick: () => pick(kind) }, h("span", { class: "ico" }, icon(ic)), h("b", { text: t(title) }), h("span", { class: "subtle", text: t(sub) }));
    return h("div", { class: "agent-empty" },
      h("h1", { text: t("What are we editing today?") }),
      h("p", { class: "muted", text: t("Show me a reference, send your clips and tell me what you want. I do the editing, check the result and fix what you ask.") }),
      h("div", { class: "agent-tiles" },
        tile("reference", "layers", "Reference video", "The edit you want to follow"),
        tile("source", "film", "Your videos", "The clips to use in the edit"),
        tile("music", "music", "Music", "Optional")),
      h("div", { class: "chips agent-sugs" }, SUGGESTIONS.map((s) => h("button", { class: "chip", text: t(s), onclick: () => { input.value = t(s); autosize(); input.focus(); } }))),
      recent && recent.length ? h("div", { class: "agent-recent" },
        h("div", { class: "section-title" }, h("h2", { text: t("Recent conversations") })),
        recent.slice(0, 6).map((r) => h("a", { class: "agent-recent-row", href: `/agent/${r.project_id}` },
          r.thumbnail_url ? h("img", { src: r.thumbnail_url, alt: "", loading: "lazy" }) : h("span", { class: "ph" }, icon("film", "icon-sm")),
          h("span", { class: "grow name", text: r.name }), h("span", { class: "subtle", text: fmtDate(r.updated_at) })))) : null,
      h("p", { class: "subtle agent-guided" }, t("Prefer to go step by step?"), " ", h("a", { href: "/create", text: t("Use the guided mode") })));
  }

  // -------------------------------------------------------------------- eventos
  function workBlock(prev) {
    if (prev && prev.classList.contains("msg-work")) return prev;
    const w = h("div", { class: "msg-work" });
    log.append(w);
    return w;
  }

  function toolLine(ev) {
    const running = ev.status === "running";
    const ico = running ? spinner() : ev.status === "done" ? icon("check", "icon-sm") : icon(ev.status === "cancelled" ? "x" : "alert", "icon-sm");
    const label = t(TOOL_TEXT[ev.name] || ev.label || ev.name);
    const head = h("div", { class: `tool-line ${ev.status}` }, h("span", { class: "tool-ico" }, ico), h("span", { class: "grow", text: label }),
      ev.seconds != null ? h("span", { class: "subtle mono", text: ev.seconds >= 60 ? fmtDur(ev.seconds) : `${Math.round(ev.seconds)}s` }) : null);
    if (running) head.dataset.toolId = ev.id;
    if (!ev.images || !ev.images.length) return head;
    const shots = h("div", { class: "tool-images" }, ev.images.map((im) => h("a", { href: im.url, target: "_blank", rel: "noopener", title: im.label || "" },
      h("img", { src: im.url, alt: im.label || "", loading: "lazy" }))));
    return h("details", { class: "tool-more" }, h("summary", {}, head), shots);
  }

  function analysisCard(s) {
    if (!s) return null;
    const stat = (v, label) => v == null ? null : h("div", { class: "stat" }, h("b", { class: "mono", text: v }), h("span", { text: t(label) }));
    return h("div", { class: "card card-tight msg-card" },
      h("div", { class: "subtle", text: t("What I found in the reference") }),
      h("div", { class: "kv" }, stat(fmtDur(s.duration_s), "Duration"), stat(s.n_shots, "shots"), stat(s.cuts_per_second, "cuts/sec"), stat(s.bpm, "BPM")),
      s.tags && s.tags.length ? h("div", { class: "chips" }, s.tags.map((x) => h("span", { class: "badge badge-accent", text: t(x) }))) : null);
  }

  function videoCard(ev) {
    const v = versions[ev.version];
    if (!v || !v.url) return h("div", { class: "msg-note subtle", text: t("This version is no longer stored.") });
    const fmt = videoFormat(v.resolution);
    const frame = h("div", { class: `msg-video ${fmt.wide ? "wide" : ""}`, style: { aspectRatio: fmt.ratio } },
      h("video", { src: v.url, poster: v.thumbnail_url || null, controls: true, playsinline: true, preload: "metadata" }));
    return h("div", { class: "msg-card msg-video-card" }, frame,
      ev.note || v.note ? h("p", { class: "muted", text: ev.note || v.note }) : null,
      h("div", { class: "subtle mono", text: [fmtDur(v.duration_s), (v.resolution || "").replace("x", "×"), fmt.label, v.variant === "no_music" ? t("without the music") : null].filter(Boolean).join(" · ") }),
      h("div", { class: "row-wrap" },
        h("a", { class: "btn btn-primary btn-sm", href: v.download, download: "" }, icon("download", "icon-sm"), t("Download")),
        h("button", { class: "btn btn-sm", onclick: () => project && shareSheet(project) }, icon("share", "icon-sm"), t("Share")),
        project && project.can_edit ? h("a", { class: "btn btn-sm", href: `/edit/${pid}` }, icon("edit", "icon-sm"), t("Edit by hand")) : null));
  }

  function eventNode(ev, prev) {
    switch (ev.type) {
      case "user": return h("div", { class: "msg msg-user" }, h("div", { class: "bubble" }, richText(ev.text)));
      case "assistant": return h("div", { class: "msg msg-ai" }, richText(ev.text));
      case "analysis": return analysisCard(ev.summary);
      case "video": return videoCard(ev);
      case "need_credits":
        return h("div", { class: "notice msg-card" }, icon("info", "icon-sm"), h("span", { class: "grow", text: t("You need {n} more credits", { n: Math.max(1, (ev.cost || 1) - (ev.balance || 0)) }) }),
          h("button", { class: "btn btn-primary btn-sm", text: t("Get credits"), onclick: () => openBuySheet({ onPurchased: () => setCredits(state.me.credits) }) }));
      case "error": return h("div", { class: "msg-error" }, icon("alert", "icon-sm"), h("span", { text: t(ev.message) }));
      case "cancelled": return h("div", { class: "msg-note subtle", text: t("Stopped. Tell me how you want to continue.") });
      default: return null;
    }
  }

  function upsert(ev) {
    const sig = JSON.stringify([ev.status, ev.seconds, ev.error, ev.images && ev.images.length, ev.version && versions[ev.version] && 1]);
    const cur = nodes.get(ev.seq);
    if (cur && cur.sig === sig) return;
    let node;
    if (ev.type === "tool") node = toolLine(ev);
    else if (ev.type === "status") node = h("div", { class: "tool-note", text: ev.text });
    else node = eventNode(ev);
    if (cur) {
      if (node) cur.el.replaceWith(node);
      nodes.set(ev.seq, { sig, el: node || cur.el });
      return;
    }
    if (!node) { nodes.set(ev.seq, { sig, el: null }); return; }
    if (ev.type === "tool" || ev.type === "status") workBlock(log.lastElementChild).append(node);
    else log.append(node);
    nodes.set(ev.seq, { sig, el: node });
  }

  function paintLive() {
    const kids = [];
    if (status === "running") {
      const tool = live && live.tool;
      if (live && live.status) kids.push(h("div", { class: "tool-note live", text: live.status }));
      if (live && live.text) kids.push(h("div", { class: "msg msg-ai" }, richText(live.text)));
      const pct = tool && tool.progress != null ? Math.round(tool.progress * 100) : null;
      kids.push(h("div", { class: "agent-working" }, spinner(),
        h("span", { text: tool ? `${t(TOOL_TEXT[tool.name] || tool.label)}${tool.stage ? ` · ${t(STAGE_TEXT[tool.stage] || tool.stage)}` : ""}` : t("Thinking…") }),
        pct ? h("span", { class: "subtle mono", text: `${pct}%` }) : null));
      if (pct) kids.push(h("div", { class: "progress agent-bar" }, h("i", { style: { width: `${pct}%` } })));
    }
    fill(liveBox, kids);
  }

  function paintComposer() {
    const running = status === "running";
    sendBtn.replaceChildren(icon(running ? "x" : "right"));
    sendBtn.setAttribute("aria-label", running ? t("Stop") : t("Send"));
    sendBtn.classList.toggle("stop", running);
    const busyUp = uploads.some((u) => u.state === "uploading");
    sendBtn.toggleAttribute("disabled", !running && (busyUp || (!input.value.trim())));
    attachBtn.toggleAttribute("disabled", running);
    const files = (project && project.files) || [];
    fill(filesRow,
      files.filter((f) => f.uploaded).map((f) => h("span", { class: "file-chip" }, icon(ROLE_ICON[f.kind] || "film", "icon-sm"),
        h("span", { class: "fc-role", text: t(ROLE_TEXT[f.kind] || f.kind) }), h("span", { class: "fc-name", text: f.name }),
        !running ? h("button", { "aria-label": t("Delete"), onclick: () => removeFile(f) }, icon("x", "icon-sm")) : null)),
      uploads.filter((u) => u.state !== "done").map((u) => h("span", { class: `file-chip ${u.state}` }, icon(ROLE_ICON[u.kind] || "film", "icon-sm"),
        h("span", { class: "fc-name", text: u.file.name }),
        u.state === "failed" ? h("span", { class: "fc-role", text: t("Failed") }) : h("span", { class: "subtle mono", text: `${Math.round((u.progress || 0) * 100)}%` }))));
  }

  // ------------------------------------------------------------------ servidor
  function paintTitle() {
    fill(titleEl, h("span", { class: "name", text: project ? project.name : t("New edit") }), project ? icon("edit", "icon-sm") : null);
  }

  function apply(data) {
    const th = data.thread;
    if (data.project) project = data.project;
    if (typeof data.credits === "number") setCredits(data.credits);
    versions = { ...versions, ...(th.versions || {}) };
    const keep = nearBottom();
    const hadNone = nodes.size === 0;
    for (const ev of th.events) upsert(ev);
    hasMessages = hasMessages || th.events.length > 0;
    lastRev = Math.max(lastRev, th.last_rev || 0);
    status = th.status;
    live = th.live;
    paintLive();
    paintComposer();
    paintTitle();
    if (nodes.size && log.querySelector(".agent-empty")) log.querySelector(".agent-empty").remove();
    if (keep || hadNone) toBottom();
  }

  async function refresh(light = false) {
    if (!pid) return;
    apply(await api(`/api/agent/threads/${pid}?after=${lastRev}${light ? "&light=1" : ""}`));
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped || status !== "running") return;
    timer = setTimeout(tick, document.hidden ? POLL_HIDDEN : POLL_ACTIVE);
  }
  async function tick() {
    const was = status;
    try { await refresh(true); } catch (e) { if (e.status === 404) { ctx.navigate("/agent", { replace: true }); return; } }
    if (was === "running" && status !== "running") {
      refresh().catch(() => {});  // o turno acabou: projeto e créditos atualizados
      track("agent_turn_done");
    }
    schedule();
  }

  async function ensureProject() {
    if (pid) return pid;
    const r = await api("/api/agent/threads", { method: "POST", body: {} });
    project = r.project;
    pid = project.id;
    history.replaceState({}, "", `/agent/${pid}`);
    paintTitle();
    return pid;
  }

  // ------------------------------------------------------------------- ações
  function send() {
    const text = input.value.trim();
    if (status === "running") {
      api(`/api/agent/threads/${pid}/cancel`, { method: "POST" }).catch((e) => toast(e.message, "err"));
      return;
    }
    if (!text) return;
    (async () => {
      try {
        await ensureProject();
        const first = !hasMessages;
        input.value = "";
        autosize();
        const r = await api(`/api/agent/threads/${pid}/messages?after=${lastRev}`, { method: "POST", body: { text } });
        apply(r);
        toBottom();
        schedule();
        if (first) api(`/api/projects/${pid}`, { method: "PATCH", body: { name: text.slice(0, 48) } }).then((x) => { project = { ...project, name: x.project.name }; paintTitle(); }).catch(() => {});
      } catch (e) {
        input.value = input.value || text;
        autosize();
        if (e.code === "agent_busy") { status = "running"; schedule(); }
        toast(e.message, "err");
      }
    })();
  }

  function pick(kind) {
    const accept = kind === "music" ? "audio/*" : kind === "reference" ? "video/*" : "video/*,image/*";
    const inp = h("input", { type: "file", accept, multiple: kind === "source" || null, class: "hidden" });
    inp.addEventListener("change", () => { for (const f of inp.files) enqueue(f, kind); inp.remove(); });
    document.body.append(inp);
    inp.click();
  }

  function chooseRole() {
    menuSheet([
      { icon: "layers", label: t("Reference video"), onclick: () => pick("reference") },
      { icon: "film", label: t("Your videos"), onclick: () => pick("source") },
      { icon: "music", label: t("Music"), onclick: () => pick("music") },
    ]);
  }

  async function enqueue(file, kind) {
    if (file.size > L.max_upload_mb * 1024 * 1024) { toast(`${file.name}: ${t("must be under {n} MB", { n: L.max_upload_mb })}`, "err"); return; }
    const u = { file, kind, state: "uploading", progress: 0 };
    uploads.push(u);
    paintComposer();
    try {
      await ensureProject();
      const meta = await probeMedia(file);
      const r = await api(`/api/projects/${pid}/files`, { method: "POST", body: { kind, name: file.name, size: file.size, duration_s: meta.duration || 0, width: meta.width, height: meta.height } });
      await uploadTo(r.upload, file, (f) => { u.progress = f; paintComposer(); });
      const done = await api(`/api/projects/${pid}/files/${r.file.id}/complete`, { method: "POST" });
      project = done.project;
      u.state = "done";
    } catch (e) {
      u.state = "failed";
      toast(e.message, "err");
    }
    paintComposer();
  }

  async function removeFile(f) {
    try { project = (await api(`/api/projects/${pid}/files/${f.id}`, { method: "DELETE" })).project; paintComposer(); }
    catch (e) { toast(e.message, "err"); }
  }

  function openMenu() {
    if (!project) { ctx.navigate("/projects"); return; }
    menuSheet([
      { icon: "plus", label: t("New edit"), onclick: () => ctx.navigate("/agent") },
      { icon: "grid", label: t("Projects"), onclick: () => ctx.navigate("/projects") },
      { icon: "type", label: t("Rename"), onclick: () => renameSheet(project, refresh) },
      project.can_edit ? { icon: "edit", label: t("Edit by hand"), onclick: () => ctx.navigate(`/edit/${pid}`) } : null,
      { icon: "trash", label: t("Delete"), danger: true, onclick: async () => {
        if (!(await confirmSheet({ title: t("Delete this project?"), body: t("Its videos and files will be removed."), ok: t("Delete"), danger: true }))) return;
        try { await api(`/api/projects/${pid}`, { method: "DELETE" }); ctx.navigate("/agent", { replace: true }); } catch (e) { toast(e.message, "err"); }
      } },
    ]);
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
    paintComposer();
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener("click", send);

  // ------------------------------------------------------------------- arranque
  try { agentCfg = await api("/api/agent/config"); } catch {}
  if (!agentCfg.enabled) {
    composer.replaceChildren(h("p", { class: "notice" }, icon("info", "icon-sm"), h("span", { text: t("The AI agent isn't switched on yet on this server. You can still create videos in the guided mode.") }),
      h("a", { class: "btn btn-sm", href: "/create", text: t("Guided mode") })));
  }
  paintTitle();
  if (pid) {
    try { await refresh(); }
    catch { return { el: h("div", { class: "error-card" }, h("p", { text: t("This conversation doesn't exist.") }), h("a", { class: "btn", href: "/agent", text: t("New edit") })), title: t("Edit") }; }
    if (!nodes.size) log.append(emptyState(null));
    setTimeout(() => { scroller.style.scrollBehavior = "auto"; toBottom(); scroller.style.scrollBehavior = ""; }, 60);  // só depois de estar na página
    schedule();
  } else {
    let recent = [];
    try { recent = (await api("/api/agent/threads")).threads; } catch {}
    log.append(emptyState(recent));
    paintComposer();
  }
  const onVis = () => { if (!document.hidden && status === "running") { clearTimeout(timer); tick(); } };
  document.addEventListener("visibilitychange", onVis);
  return { el, full: true, title: project ? project.name : t("New edit"), cleanup: () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", onVis); } };
}
