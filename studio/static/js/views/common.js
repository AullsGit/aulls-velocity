import { h, icon, fmtDur, fmtDate, statusBadge, menuSheet, confirmSheet, toast, sheet, setBusy } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";

// O resultado tem o formato da referência quando ela é reconstruída (16:9, 1:1...), não sempre 9:16.
export function videoFormat(resolution) {
  const m = /^(\d+)x(\d+)$/.exec(resolution || "");
  if (!m) return { label: "9:16", ratio: "9 / 16", wide: false };
  const w = +m[1], hh = +m[2];
  const known = [[9, 16], [16, 9], [1, 1], [4, 5], [5, 4], [4, 3], [3, 4], [21, 9]];
  const k = known.find(([a, b]) => Math.abs(w / hh - a / b) < 0.02);
  const gcd = (a, b) => (b ? gcd(b, a % b) : a), g = gcd(w, hh) || 1;
  return { label: k ? `${k[0]}:${k[1]}` : `${w / g}:${hh / g}`, ratio: `${w} / ${hh}`, wide: w > hh };
}

export function fitFrame(frame, resolution) {
  const f = videoFormat(resolution);
  frame.style.aspectRatio = f.ratio;
  frame.classList.toggle("wide", f.wide);
  return f;
}

export function projectHref(p) {
  if (p.status === "draft" || (p.status === "failed" && !p.has_output)) return `/create/${p.id}`;
  return `/projects/${p.id}`;
}

export function renameSheet(p, onDone) {
  sheet((close) => {
    const input = h("input", { class: "input", value: p.name, maxlength: 60 });
    const save = h("button", { class: "btn btn-primary", text: t("Save") });
    save.onclick = async () => {
      setBusy(save, true);
      try { await api(`/api/projects/${p.id}`, { method: "PATCH", body: { name: input.value } }); close(); onDone && onDone(); }
      catch (e) { toast(e.message, "err"); setBusy(save, false); }
    };
    return [h("h2", { text: t("Rename") }), h("div", { class: "stack", style: { marginTop: "14px" } }, input,
      h("div", { class: "row", style: { justifyContent: "flex-end" } }, h("button", { class: "btn", onclick: close, text: t("Cancel") }), save))];
  });
}

export function projectMenu(p, { navigate, onChange }) {
  menuSheet([
    { icon: "film", label: t("Open"), onclick: () => navigate(projectHref(p)) },
    p.has_output && !p.sources_purged ? { icon: "edit", label: t("Edit"), onclick: () => navigate(`/edit/${p.id}`) } : null,
    p.has_output ? { icon: "download", label: t("Download"), onclick: () => { location.href = `/api/projects/${p.id}/download`; } } : null,
    { icon: "type", label: t("Rename"), onclick: () => renameSheet(p, onChange) },
    !p.sources_purged ? { icon: "copy", label: t("Duplicate"), onclick: async () => {
      try { const r = await api(`/api/projects/${p.id}/duplicate`, { method: "POST" }); navigate(`/create/${r.project.id}`); }
      catch (e) { toast(e.message, "err"); }
    } } : null,
    { icon: "trash", label: t("Delete"), danger: true, onclick: async () => {
      const ok = await confirmSheet({ title: t("Delete this project?"), body: t("Its videos and files will be removed."), ok: t("Delete"), danger: true });
      if (!ok) return;
      try { await api(`/api/projects/${p.id}`, { method: "DELETE" }); onChange && onChange(); } catch (e) { toast(e.message, "err"); }
    } },
  ]);
}

export function projectCard(p, opts) {
  const thumb = h("div", { class: "thumb" },
    p.thumbnail_url ? h("img", { src: p.thumbnail_url, alt: "", loading: "lazy", decoding: "async" })
      : h("div", { class: "ph" }, icon(p.status === "draft" ? "upload" : "film", "icon-lg")),
    statusBadge(p.status),
    p.duration_s && p.status !== "processing" ? h("span", { class: "dur mono", text: fmtDur(p.duration_s) }) : null,
    p.status === "processing" ? h("div", { class: "progress-mini" }, h("i", { style: { width: "12%" }, dataset: { pid: p.id } })) : null);
  const menuBtn = h("button", { class: "btn btn-icon btn-sm menu-btn", "aria-label": "Options", onclick: (e) => {
    e.preventDefault(); e.stopPropagation(); projectMenu(p, opts);
  } }, icon("more", "icon-sm"));
  return h("a", { class: "pcard", href: projectHref(p) }, menuBtn, thumb,
    h("div", { class: "meta" },
      h("span", { class: "name", text: p.name }),
      h("span", { class: "subtle", text: [fmtDate(p.updated_at), p.reference_name ? `ref: ${p.reference_name}` : ""].filter(Boolean).join(" · ") })));
}

// Atualiza o progresso dos cartões em processamento (polling leve, pausa com o separador oculto).
export function watchProcessing(container, projects, onDone) {
  const busy = projects.filter((p) => p.status === "processing");
  if (!busy.length) return () => {};
  let stop = false;
  const tick = async () => {
    if (stop) return;
    if (!document.hidden) {
      let changed = false;
      for (const p of busy) {
        try {
          const { project } = await api(`/api/projects/${p.id}`);
          const j = project.active_job;
          const bar = container.querySelector(`[data-pid="${p.id}"]`);
          if (bar && j) bar.style.width = `${Math.max(8, Math.round((j.progress || 0) * 100))}%`;
          if (project.status !== "processing") changed = true;
        } catch {}
      }
      if (changed) { onDone(); return; }
    }
    setTimeout(tick, 4000);
  };
  setTimeout(tick, 1500);
  return () => { stop = true; };
}
