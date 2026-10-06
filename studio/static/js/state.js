import { api } from "./api.js";

const listeners = new Set();
export const state = { me: null };

export async function loadMe() {
  try { state.me = await api("/api/me"); } catch { state.me = state.me || { logged_in: false }; }
  listeners.forEach((fn) => fn(state.me));
  return state.me;
}
export function setCredits(n) {
  if (state.me && typeof n === "number") {
    state.me.credits = n;
    listeners.forEach((fn) => fn(state.me));
  }
}
export function onMe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function costFor(profile) {
  const c = state.me && state.me.pricing && state.me.pricing.credits_per_generation;
  return (c && c[profile]) || 1;
}

// tema: system | dark | light
const TKEY = "awls.theme";
export function getTheme() { try { return localStorage.getItem(TKEY) || "system"; } catch { return "system"; } }
export function applyTheme(mode = getTheme()) {
  const dark = mode === "dark" || (mode === "system" && !matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? "#0a0a0d" : "#f5f5f7";
}
export function setTheme(mode) {
  try { localStorage.setItem(TKEY, mode); } catch {}
  applyTheme(mode);
}
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => applyTheme());

// id anónimo só para contar visitantes (sem dados pessoais)
export function anonId() {
  try {
    let id = localStorage.getItem("awls.anon");
    if (!id) { id = Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem("awls.anon", id); }
    return id;
  } catch { return null; }
}
export function track(name, props = {}) {
  fetch("/api/events", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify({ name, props, anon_id: anonId() }) }).catch(() => {});
}
