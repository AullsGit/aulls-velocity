// Progresso da geração (estados reais) e resultado (ver / editar / descarregar / partilhar).
import { h, icon, toast, fmtDur, sheet, confirmSheet, setBusy } from "../ui.js";
import { t } from "../i18n.js";
import { api, pollJob, uid } from "../api.js";
import { state, setCredits, costFor, track } from "../state.js";
import { renameSheet, fitFrame } from "./common.js";
import { openBuySheet } from "./buy.js";

const GEN_STAGES = [["uploading", "Uploading"], ["analyzing_reference", "Analyzing reference"], ["preparing_edit", "Preparing edit"],
  ["rendering", "Rendering"], ["applying_effects", "Applying effects"], ["finalizing", "Finalizing"], ["complete", "Complete"]];
const EDIT_STAGES = [["preparing_edit", "Preparing edit"], ["rendering", "Rendering"], ["finalizing", "Finalizing"], ["complete", "Complete"]];

export async function render(ctx) {
  const id = ctx.params.id;
  const el = h("div");
  let stop = () => {};
  let timer;

  async function load() {
    stop(); clearInterval(timer);
    let project;
    try { project = (await api(`/api/projects/${id}`)).project; }
    catch { el.replaceChildren(h("div", { class: "error-card" }, h("p", { text: "This project doesn't exist." }), h("a", { class: "btn", href: "/projects", text: t("Projects") }))); return; }
    if (project.status === "draft" && !project.active_job) { ctx.navigate(`/create/${id}`, { replace: true }); return; }
    if (project.active_job) return progress(project, project.active_job);
    if (project.has_output) return result(project);
    return failed(project);
  }

  // ------------------------------------------------------------ progresso
  function progress(project, job) {
    const stages = job.kind === "render_edit" ? EDIT_STAGES : GEN_STAGES;
    const pct = h("span", { class: "pct mono", text: "0%" });
    const bar = h("i");
    const list = h("div", { class: "stages" });
    const sub = h("p", { class: "muted", text: t("You can leave this page — we'll keep working.") });
    const elapsed = h("span", { class: "subtle mono" });
    const t0 = new Date(job.created_at).getTime();
    timer = setInterval(() => { elapsed.textContent = fmtDur((Date.now() - t0) / 1000); }, 1000);
    const cancel = h("button", { class: "btn btn-ghost btn-danger", text: t("Cancel and refund") });
    cancel.onclick = async () => {
      if (!(await confirmSheet({ title: t("Cancel and refund") + "?", body: "Your credits will be returned.", ok: t("Cancel and refund"), danger: true }))) return;
      setBusy(cancel, true);
      try { const r = await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); setCredits(r.credits); load(); }
      catch (e) { toast(e.message, "err"); setBusy(cancel, false); }
    };
    const paintJob = (j) => {
      const frac = j.status === "COMPLETED" ? 1 : Math.max(0.02, j.progress || 0);
      pct.textContent = `${Math.round(frac * 100)}%`;
      bar.style.width = `${Math.round(frac * 100)}%`;
      let idx = stages.findIndex(([k]) => k === j.stage);
      if (idx < 0) idx = j.kind === "render_edit" ? 0 : 1;
      if (j.status === "COMPLETED") idx = stages.length - 1;
      const queued = j.status === "QUEUED";
      list.replaceChildren(...stages.map(([k, label], i) => h("div", { class: `stage ${i < idx || j.status === "COMPLETED" ? "done" : i === idx ? "now" : ""}` },
        h("span", { class: "s-ico" }, i < idx || j.status === "COMPLETED" ? icon("check", "icon-sm") : null),
        h("span", { text: queued && i === idx ? t("Waiting for a free engine") : t(label) }),
        i === idx && k === "rendering" && !queued ? h("span", { class: "subtle mono", text: `${Math.round(frac * 100)}%` } ) : h("span"))));
    };
    paintJob(job);
    el.replaceChildren(h("div", { class: "result-layout" },
      h("div", { class: "phone-frame gen-frame" }, h("div", { class: "gen-visual" }, pct)),
      h("div", { class: "stack" },
        h("div", { class: "stack-sm" }, h("span", { class: "step-kicker", text: project.name }),
          h("h1", { text: t("Creating your edit…") }), h("div", { class: "row" }, sub)),
        h("div", { class: "progress" }, bar), h("div", { class: "spread" }, elapsed, h("span")),
        h("div", { class: "card" }, list), cancel)));
    stop = pollJob(job.id, (j, credits) => {
      paintJob(j);
      if (typeof credits === "number") setCredits(credits);
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(j.status)) setTimeout(load, j.status === "COMPLETED" ? 600 : 0);
    });
  }

  // ------------------------------------------------------------ resultado
  function result(project) {
    const video = h("video", { src: project.video_url, poster: project.thumbnail_url || null, controls: true, playsinline: true, autoplay: true, muted: true, loop: true, preload: "auto" });
    const title = h("button", { class: "btn btn-ghost", style: { padding: 0, height: "auto", justifyContent: "flex-start" }, onclick: () => renameSheet(project, load) },
      h("h1", { text: project.name }), icon("edit", "icon-sm"));
    const lj = project.last_job;
    const frame = h("div", { class: "phone-frame" }, video);
    const fmt = fitFrame(frame, project.resolution);
    el.replaceChildren(h("div", { class: "result-layout" },
      frame,
      h("div", { class: "stack" },
        h("div", { class: "stack-sm" },
          h("span", { class: "badge badge-ok", style: { justifySelf: "start" } }, icon("check", "icon-sm"), t("Your edit is ready")),
          title, h("span", { class: "subtle" }, icon("check", "icon-sm"), " ", t("Saved in Projects"))),
        h("div", { class: "kv" },
          h("div", { class: "stat" }, h("b", { class: "mono", text: fmtDur(project.duration_s) }), h("span", { text: t("Duration") })),
          h("div", { class: "stat" }, h("b", { class: "mono", text: (project.resolution || "—").replace("x", "×") }), h("span", { text: t("Resolution") })),
          h("div", { class: "stat" }, h("b", { text: fmt.label }), h("span", { text: t("Format") }))),
        h("div", { class: "actions-grid" },
          !project.sources_purged ? h("a", { class: "btn btn-primary", href: `/agent/${project.id}` }, icon("spark"), t("Ask the AI for changes")) : null,
          project.can_edit ? h("a", { class: "btn", href: `/edit/${project.id}` }, icon("edit"), t("Edit")) : null,
          h("a", { class: "btn", href: `/api/projects/${project.id}/download`, download: "" }, icon("download"), t("Download")),
          h("button", { class: "btn", onclick: () => shareSheet(project) }, icon("share"), t("Share")),
          project.files.some((f) => f.kind === "reference" && f.uploaded) && !project.sources_purged
            ? h("button", { class: "btn", onclick: () => generateAgain(project) }, icon("refresh"), t("Generate again")) : null),
        project.sources_purged ? h("p", { class: "notice" }, icon("info", "icon-sm"), h("span", { text: "The original clips expired, so this video can't be edited anymore. The final video stays available." })) : null,
        lj && lj.processing_time_s ? h("p", { class: "subtle", text: `Created in ${fmtDur(lj.processing_time_s)}` }) : null)));
  }

  async function generateAgain(project) {
    const profile = (project.settings && project.settings.profile) || "standard";
    const cost = costFor(profile);
    if ((state.me.credits ?? 0) < cost) {
      openBuySheet({ reason: t("You need {n} more credits", { n: cost - (state.me.credits ?? 0) }) });
      return;
    }
    const ok = await confirmSheet({ title: t("Generate again"), body: `A new version with the same clips and reference. ${cost} ${cost === 1 ? t("credit") : t("credits")}.`, ok: t("Generate") });
    if (!ok) return;
    try {
      const r = await api(`/api/projects/${project.id}/generate`, { method: "POST", headers: { "Idempotency-Key": uid() }, body: { profile } });
      setCredits(r.credits);
      load();
    } catch (e) { toast(e.message, "err"); }
  }

  function failed(project) {
    const lj = project.last_job;
    el.replaceChildren(h("div", { class: "stack", style: { maxWidth: "560px", margin: "40px auto" } },
      h("div", { class: "error-card" }, h("span", { style: { color: "var(--danger)" } }, icon("alert", "icon-lg")),
        h("h2", { text: t("Something went wrong while creating your video.") }),
        h("p", { class: "muted", text: (lj && lj.error_message) || "" }),
        lj && lj.refunded ? h("span", { class: "badge badge-ok", text: "Credits refunded" }) : null),
      h("a", { class: "btn btn-primary btn-lg", href: `/create/${project.id}` }, icon("refresh"), t("Try again"))));
  }

  await load();
  return { el, title: t("Projects"), cleanup: () => { stop(); clearInterval(timer); } };
}

// --------------------------------------------------------------- partilha
export function shareSheet(project) {
  track("share_clicked");
  sheet((close) => {
    const pub = h("input", { type: "checkbox", checked: project.is_public || null, onchange: async (e) => {
      try { await api(`/api/projects/${project.id}`, { method: "PATCH", body: { is_public: e.target.checked } }); toast(t("Saved")); }
      catch (err) { e.target.checked = !e.target.checked; toast(err.message, "err"); }
    } });
    const nativeBtn = h("button", { class: "btn btn-primary btn-lg btn-block" }, icon("share"), t("Share"));
    nativeBtn.onclick = async () => {
      setBusy(nativeBtn, true);
      try {
        const blob = await (await fetch(project.video_url)).blob();
        const file = new File([blob], `${project.name || "awlsvelocity"}.mp4`, { type: "video/mp4" });
        if (navigator.canShare && navigator.canShare({ files: [file] })) await navigator.share({ files: [file], title: project.name });
        else { const url = await link(); await navigator.share({ url, title: project.name }); }
      } catch (e) { if (e.name !== "AbortError") toast("Sharing isn't available here — copy the link instead.", "err"); }
      finally { setBusy(nativeBtn, false); }
    };
    async function link() {
      const r = await api(`/api/projects/${project.id}/share`, { method: "POST" });
      return r.url;
    }
    const copyBtn = h("button", { class: "btn btn-lg btn-block" }, icon("link"), t("Copy link"));
    copyBtn.onclick = async () => {
      try { const url = await link(); await navigator.clipboard.writeText(url); toast(t("Link copied")); }
      catch (e) { toast(e.message, "err"); }
    };
    return [
      h("h2", { text: t("Share") }),
      h("p", { class: "muted", style: { margin: "4px 0 16px" }, text: "Vertical 9:16 — ready for TikTok, Instagram Reels, YouTube Shorts and WhatsApp." }),
      h("div", { class: "stack-sm" },
        navigator.share ? nativeBtn : null, copyBtn,
        h("a", { class: "btn btn-lg btn-block", href: `/api/projects/${project.id}/download`, download: "" }, icon("download"), t("Download"))),
      h("div", { class: "divider" }),
      h("label", { class: "spread" }, h("span", { text: t("Publish to community") }), pub),
    ];
  });
}
