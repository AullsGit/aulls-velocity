import { h, icon, toast, fmtDate, setBusy } from "../ui.js";
import { t, getLang, setLang, LANGS } from "../i18n.js";
import { api } from "../api.js";
import { state, loadMe, getTheme, setTheme } from "../state.js";
import { buyPanel } from "./buy.js";

const REASONS = { generation: "Generation", refund: "Refund", purchase: "Purchase", manual_payment: "e-Mola payment",
  promo: "Promo code", admin: "Added by support" };

export async function render(ctx) {
  const me = state.me;
  const data = await api("/api/credits");
  const credits = h("div", { class: "card", id: "credits" },
    ctx.view === "credits" ? null : h("div", { class: "card-head" }, h("h2", { text: t("Buy credits") })),
    buyPanel({ onPurchased: () => setTimeout(() => ctx.navigate(location.pathname, { replace: true }), 900) }));

  const name = h("input", { class: "input", value: me.display_name || "", maxlength: 40 });
  const saveName = h("button", { class: "btn", text: t("Save") });
  saveName.onclick = async () => {
    setBusy(saveName, true);
    try { await api("/api/me", { method: "PATCH", body: { display_name: name.value } }); await loadMe(); toast(t("Saved")); }
    catch (e) { toast(e.message, "err"); } finally { setBusy(saveName, false); }
  };
  const seg = (items, current, onPick) => h("div", { class: "chips" }, items.map(([k, label]) =>
    h("button", { class: "chip", "aria-pressed": String(k === current), onclick: (e) => {
      e.currentTarget.parentElement.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
      e.currentTarget.setAttribute("aria-pressed", "true");
      onPick(k);
    } }, label)));

  const account = h("div", { class: "card stack" },
    h("div", { class: "row" },
      h("div", { style: { width: "52px", height: "52px", borderRadius: "50%", background: "var(--accent)", color: "var(--accent-ink)",
        display: "grid", placeItems: "center", fontWeight: 800, fontSize: "20px", fontFamily: "var(--font-display)" },
        text: (me.display_name || me.email || "?").slice(0, 1).toUpperCase() }),
      h("div", { style: { minWidth: 0 } }, h("strong", { text: me.display_name || "" }), h("div", { class: "subtle", text: me.email }))),
    h("label", { class: "field" }, h("span", { text: t("Display name") }), h("div", { class: "row" }, name, saveName)),
    h("div", { class: "spread" }, h("span", { class: "muted", text: t("Plan") }), h("span", { class: "badge", text: t("Pay per generation") })),
    h("div", { class: "field" }, h("span", { text: t("Language") }), seg(LANGS, getLang(), (k) => { setLang(k); ctx.navigate(location.pathname, { replace: true }); })),
    h("div", { class: "field" }, h("span", { text: t("Theme") }), seg([["system", t("System")], ["dark", t("Dark")], ["light", t("Light")]], getTheme(), setTheme)),
    window.awlsInstall ? h("button", { class: "btn", onclick: async () => { window.awlsInstall.prompt(); window.awlsInstall = null; } }, icon("download"), "Install app") : null,
    me.is_admin ? h("a", { class: "btn", href: "/admin" }, icon("shield"), t("Admin")) : null,
    h("button", { class: "btn btn-ghost btn-danger", onclick: async () => {
      await api("/api/auth/logout", { method: "POST" });
      state.me = null;
      location.href = "/";
    } }, icon("logout"), t("Log out")));

  const tx = data.transactions;
  const history = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: t("History") })),
    tx.length ? h("div", { class: "list" }, tx.map((x) => h("div", { class: "item" },
      h("div", {}, h("div", { text: REASONS[x.reason] || x.reason }), h("div", { class: "subtle", text: fmtDate(x.created_at) })),
      h("strong", { class: "mono", style: { color: x.delta > 0 ? "var(--ok)" : "var(--text)" }, text: `${x.delta > 0 ? "+" : ""}${x.delta}` }))))
      : h("p", { class: "muted", text: "No activity yet." }));

  const pays = data.payments.filter((p) => p.provider === "emola");
  const payments = pays.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: t("Payments") })),
    h("div", { class: "list" }, pays.map((p) => h("div", { class: "item" },
      h("span", { text: `${p.amount_mt} MT${p.reference ? ` · ${p.reference}` : ""}` }),
      h("span", { class: `badge ${p.status === "confirmed" ? "badge-ok" : p.status === "rejected" ? "badge-danger" : "badge-warn"}`, text: p.status }))))) : null;

  const el = h("div", { class: "stack", style: { maxWidth: "760px" } },
    h("div", { class: "topbar" }, h("h1", { text: ctx.view === "credits" ? t("Buy credits") : t("Profile") })),
    ctx.view === "credits" ? [credits, history, payments] : [account, credits, history, payments]);
  return { el, title: t("Profile") };
}
