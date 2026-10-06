// Landing: se a pessoa já tem sessão, os CTAs levam direto à app; conta visitantes (anónimo).
import { anonId, track } from "./state.js";

track("page_view", { page: "landing", ref: document.referrer ? new URL(document.referrer).hostname : "" });
document.querySelectorAll("[data-cta]").forEach((a) => a.addEventListener("click", () => track("landing_cta")));

fetch("/api/me", { credentials: "same-origin" }).then((r) => r.json()).then((me) => {
  if (!me.logged_in) return;
  document.querySelectorAll("[data-cta]").forEach((a) => { a.href = "/create"; });
  document.querySelectorAll("[data-guest]").forEach((a) => { a.href = "/home"; a.textContent = "Open app"; });
}).catch(() => {});
anonId();
