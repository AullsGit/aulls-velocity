// Utilitários de interface. Regra: dados do utilizador entram sempre como TEXTO (textContent),
// nunca como HTML — só os ícones (SVG fixo, do próprio código) usam innerHTML.
import { t } from "./i18n.js";

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "text") el.textContent = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// replaceChildren() transformaria null/false em texto ("null"); isto ignora-os, como o h().
export function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  return el;
}

const P = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h5v-6h4v6h5V9.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.5 2.5M15.2 15.2l2.5 2.5M6.3 17.7l2.5-2.5M15.2 8.8l2.5-2.5"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  music: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="M5 12.5 10 17l9-10"/>',
  left: '<path d="M15 5l-7 7 7 7"/>', right: '<path d="M9 5l7 7-7 7"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 20h16"/>',
  share: '<path d="M12 15V3M7 8l5-5 5 5"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/>',
  edit: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/>',
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>', pause: '<path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" fill="currentColor"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>', redo: '<path d="m15 14 5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.6 7.6 20 18M8.6 16.4 20 6"/>',
  split: '<path d="M12 3v18"/><path d="M8 7H4v10h4M16 7h4v10h-4"/>',
  speed: '<path d="M12 13l4-4"/><path d="M3.5 17a9 9 0 1 1 17 0"/>',
  wand: '<path d="M4 20 15 9"/><path d="M14 4v3M20 10h-3M18.5 5.5l-2 2M11 4.5l.8 1.6M19.5 13l-1.6-.8"/>',
  type: '<path d="M5 6V4h14v2M12 4v16M9 20h6"/>',
  sliders: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  zoom: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2M11 8.5v5M8.5 11h5"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 20v-4h-4"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
  card: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h3"/>',
  gift: '<rect x="3" y="8" width="18" height="5" rx="1"/><path d="M5 13v8h14v-8M12 8v13M12 8c-2-4-6-4-6-1.5S10 8 12 8zm0 0c2-4 6-4 6-1.5S14 8 12 8z"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  flip: '<path d="M12 3v18"/><path d="M8 7 4 12l4 5V7zM16 7l4 5-4 5V7z"/>',
  layers: '<path d="M12 3 3 8l9 5 9-5z"/><path d="M3 13l9 5 9-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/>',
};
export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.innerHTML = `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[name] || ""}</svg>`;
  return span.firstChild;
}

// ------------------------------------------------------------------ toasts
let toastBox;
export function toast(msg, kind = "ok", ms = 3200) {
  if (!toastBox) toastBox = document.body.appendChild(h("div", { class: "toasts", role: "status", "aria-live": "polite" }));
  const el = h("div", { class: `toast ${kind === "err" ? "err" : ""}` }, icon(kind === "err" ? "alert" : "check", "icon-sm"), h("span", { text: msg }));
  toastBox.append(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .2s"; setTimeout(() => el.remove(), 220); }, ms);
}

// ------------------------------------------------------------------ folhas
export function sheet(build, { onClose } = {}) {
  const prevFocus = document.activeElement;
  const box = h("div", { class: "sheet", role: "dialog", "aria-modal": "true" }, h("div", { class: "grab" }));
  const ov = h("div", { class: "overlay" }, box);
  const close = () => {
    ov.remove();
    document.removeEventListener("keydown", onKey);
    onClose && onClose();
    prevFocus && prevFocus.focus && prevFocus.focus();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  document.addEventListener("keydown", onKey);
  box.append(...[].concat(build(close)));
  document.body.append(ov);
  const f = box.querySelector("input,button,[tabindex]");
  f && f.focus({ preventScroll: true });
  return close;
}

export function confirmSheet({ title, body, ok, danger }) {
  return new Promise((resolve) => {
    let result = false;
    sheet((close) => [
      h("h2", { text: title }),
      body ? h("p", { class: "muted", style: { marginBottom: "18px" }, text: body }) : null,
      h("div", { class: "row", style: { justifyContent: "flex-end" } },
        h("button", { class: "btn", onclick: close, text: t("Cancel") }),
        h("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, onclick: () => { result = true; close(); }, text: ok })),
    ], { onClose: () => resolve(result) });
  });
}

export function menuSheet(items) {
  sheet((close) => h("div", { class: "menu" }, items.filter(Boolean).map((it) =>
    h("button", { class: it.danger ? "danger" : "", onclick: () => { close(); it.onclick(); } }, icon(it.icon), h("span", { text: it.label })))));
}

// --------------------------------------------------------------- formatação
export function fmtDur(s) {
  if (s == null || isNaN(s)) return "—";
  s = Math.round(s);
  const m = Math.floor(s / 60);
  return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `0:${String(s).padStart(2, "0")}`;
}
export function fmtBytes(b) {
  if (!b) return "";
  return b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`;
}
export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60) return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
export function fmtUsd(cents) { return `$${(cents / 100).toFixed(2)}`; }

export function spinner() { return h("span", { class: "spinner", "aria-hidden": "true" }); }

export function setBusy(btn, busy) {
  if (!btn) return;
  if (busy) {
    btn.dataset.label = btn.dataset.label || btn.innerHTML;
    btn.setAttribute("aria-disabled", "true");
    btn.replaceChildren(spinner());
  } else if (btn.dataset.label) {
    btn.removeAttribute("aria-disabled");
    btn.innerHTML = btn.dataset.label;
    delete btn.dataset.label;
  }
}

export const STATUS = {
  draft: ["Draft", ""], processing: ["Processing", "badge-warn badge-live"], completed: ["Completed", "badge-ok"],
  failed: ["Failed", "badge-danger"],
};
export function statusBadge(status) {
  const [label, cls] = STATUS[status] || [status, ""];
  return h("span", { class: `badge ${cls}` }, h("span", { class: "dot" }), t(label));
}
