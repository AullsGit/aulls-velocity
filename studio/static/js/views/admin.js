// Admin simples: negócio (receita, custo, lucro, conversão), operação (filas, falhas) e preço.
import { h, icon, toast, fmtDate, fmtDur, setBusy, fill } from "../ui.js";
import { api } from "../api.js";

const money = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
const num = (v, d = 0) => (v == null ? "—" : Number(v).toFixed(d));

export async function render() {
  const el = h("div", { class: "stack" });
  const load = async () => {
    const [{ stats: s, provider, pricing, packs }, { users }, { jobs }, { payments }] = await Promise.all([
      api("/api/admin/stats"), api("/api/admin/users"), api("/api/admin/jobs"), api("/api/admin/payments/pending")]);
    const tile = (label, value) => h("div", { class: "tile" }, h("b", { class: "mono", text: value }), h("span", { text: label }));
    const statusFilter = h("select", { class: "select", style: { width: "auto", height: "36px" } },
      ["", "QUEUED", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"].map((x) => h("option", { value: x, text: x || "All jobs" })));
    const jobsBody = h("tbody");
    const paintJobs = (rows) => jobsBody.replaceChildren(...rows.map((j) => h("tr", {},
      h("td", { text: fmtDate(j.created_at) }), h("td", { text: j.kind }),
      h("td", {}, h("span", { class: `badge ${j.status === "COMPLETED" ? "badge-ok" : j.status === "FAILED" ? "badge-danger" : j.status === "CANCELLED" ? "" : "badge-warn"}`, text: j.status })),
      h("td", { text: j.stage || "" }), h("td", { class: "mono", text: fmtDur(j.queue_time_s) }), h("td", { class: "mono", text: fmtDur(j.processing_time_s) }),
      h("td", { class: "mono", text: j.est_cost_usd != null ? `$${Number(j.est_cost_usd).toFixed(4)}` : "—" }),
      h("td", { text: `${j.credits_used}${j.refunded ? " ↩" : ""}` }), h("td", { text: j.provider || "" }),
      h("td", { class: "wrap", text: j.error_detail ? j.error_detail.slice(0, 220) : "" }))));
    paintJobs(jobs);
    statusFilter.onchange = async () => paintJobs((await api(`/api/admin/jobs${statusFilter.value ? `?status=${statusFilter.value}` : ""}`)).jobs);

    const cfg = (key, label, hint) => {
      const input = h("input", { class: "input", value: JSON.stringify(pricing[key]), style: { fontFamily: "monospace" } });
      const save = h("button", { class: "btn btn-sm", text: "Save" });
      save.onclick = async () => {
        let value;
        try { value = JSON.parse(input.value); } catch { toast("Invalid value (JSON)", "err"); return; }
        setBusy(save, true);
        try { await api(`/api/admin/config/${key}`, { method: "PUT", body: { value } }); toast("Saved"); }
        catch (e) { toast(e.message, "err"); } finally { setBusy(save, false); }
      };
      return h("label", { class: "field" }, h("span", { text: label }), h("div", { class: "row" }, input, save), hint ? h("span", { class: "subtle", text: hint }) : null);
    };

    const promoCode = h("input", { class: "input", placeholder: "LAUNCH10" });
    const promoCredits = h("input", { class: "input", type: "number", min: 1, value: 1, style: { width: "90px" } });
    const promoUses = h("input", { class: "input", type: "number", min: 1, value: 50, style: { width: "90px" } });

    fill(el,
      h("div", { class: "topbar" }, h("h1", { text: "Admin" }), h("span", { class: "badge", text: `GPU provider: ${provider}` })),
      h("div", { class: "tiles" },
        tile("Users", num(s.users)), tile("Visitors", num(s.visitors)), tile("Videos completed", num(s.completed)), tile("Failed", num(s.failed)),
        tile("Revenue (USD)", money(s.revenue_usd)), tile("Revenue (MT)", num(s.revenue_mt)), tile("Compute cost", money(s.compute_cost_usd)), tile("Est. profit", money(s.est_profit_usd)),
        tile("Cost / video", s.avg_cost_per_video_usd != null ? `$${Number(s.avg_cost_per_video_usd).toFixed(4)}` : "—"),
        tile("Avg processing", fmtDur(s.avg_processing_s)), tile("Avg queue", fmtDur(s.avg_queue_s)), tile("In queue / running", `${s.queued} / ${s.processing}`),
        tile("Conversion to paid", s.conversion_to_paid != null ? `${(s.conversion_to_paid * 100).toFixed(1)}%` : "—"),
        tile("Videos / user", num(s.videos_per_user, 2)), tile("Time to 1st video", fmtDur(s.avg_time_to_first_video_s)),
        tile("Returning users", num(s.returning_users)), tile("Revenue / user", s.revenue_per_user_usd != null ? `$${Number(s.revenue_per_user_usd).toFixed(3)}` : "—"),
        tile("Credits spent / refunded", `${s.credits_spent} / ${s.credits_refunded}`)),
      payments.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: `e-Mola to confirm (${payments.length})` })),
        h("div", { class: "list" }, payments.map((p) => {
          const n = h("input", { class: "input", type: "number", min: 1, value: 1, style: { width: "80px", height: "36px" } });
          const ok = h("button", { class: "btn btn-sm btn-primary", text: "Confirm" });
          ok.onclick = async () => { setBusy(ok, true); try { await api(`/api/admin/payments/${p.id}/confirm`, { method: "POST", body: { credits: Number(n.value) } }); load(); } catch (e) { toast(e.message, "err"); setBusy(ok, false); } };
          const no = h("button", { class: "btn btn-sm btn-ghost btn-danger", text: "Reject", onclick: async () => { await api(`/api/admin/payments/${p.id}/reject`, { method: "POST" }); load(); } });
          return h("div", { class: "item" }, h("div", {}, h("strong", { text: `${p.amount_mt} MT` }),
            h("div", { class: "subtle", text: `${p.email || p.user_id} · ${p.reference || "no ref"} · ${fmtDate(p.created_at)}` })),
            h("div", { class: "row" }, n, h("span", { class: "subtle", text: "credits" }), ok, no));
        }))) : null,
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Jobs" }), statusFilter),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["When", "Kind", "Status", "Stage", "Queue", "Processing", "Cost", "Credits", "Provider", "Error"].map((x) => h("th", { text: x })))), jobsBody))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: `Users (${users.length})` })),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["Email", "Joined", "Credits", "Generations", "Completed", "Spent", ""].map((x) => h("th", { text: x })))),
          h("tbody", {}, users.map((u) => {
            const give = h("button", { class: "btn btn-sm", text: "+ credits" });
            give.onclick = async () => {
              const n = Number(prompt(`Credits to add to ${u.email}`, "1"));
              if (!n) return;
              try { await api("/api/admin/credits/grant", { method: "POST", body: { user_id: u.id, credits: n } }); load(); } catch (e) { toast(e.message, "err"); }
            };
            return h("tr", {}, h("td", { text: u.email || u.id }), h("td", { text: fmtDate(u.created_at) }), h("td", { class: "mono", text: u.paid_credits }),
              h("td", { class: "mono", text: u.generations }), h("td", { class: "mono", text: u.completed }), h("td", { class: "mono", text: u.credits_spent }), h("td", {}, give));
          }))))),
      h("div", { class: "card stack" }, h("h2", { text: "Pricing" }),
        cfg("credits_per_generation", "Credits per generation (by quality)", 'e.g. {"fast":1,"standard":1,"hd":2}'),
        cfg("credits_per_edit_export", "Credits per editor export", "0 = free"),
        cfg("credit_usd_value", "Value of 1 credit (USD, shown to users)"),
        cfg("compute_cost_per_second_usd", "Compute cost per second (USD)", "From your RunPod GPU price — used to estimate cost per video"),
        h("div", { class: "divider" }),
        h("h3", { text: "Credit packs" }),
        h("div", { class: "list" }, packs.map((p) => h("div", { class: "item" },
          h("span", { text: `${p.id}: ${p.credits} credits — $${(p.price_cents / 100).toFixed(2)} ${p.active ? "" : "(inactive)"}` }),
          h("button", { class: "btn btn-sm", text: p.active ? "Deactivate" : "Activate", onclick: async () => {
            await api(`/api/admin/packs/${p.id}`, { method: "PUT", body: { ...p, active: !p.active } }); load();
          } })))),
        h("div", { class: "divider" }),
        h("h3", { text: "Promo code" }),
        h("div", { class: "row-wrap" }, promoCode, promoCredits, h("span", { class: "subtle", text: "credits ·" }), promoUses, h("span", { class: "subtle", text: "uses" }),
          h("button", { class: "btn btn-primary btn-sm", text: "Create", onclick: async () => {
            try { await api("/api/admin/promos", { method: "POST", body: { code: promoCode.value, credits: Number(promoCredits.value), max_uses: Number(promoUses.value) } }); toast("Promo created"); promoCode.value = ""; }
            catch (e) { toast(e.message, "err"); }
          } }))),
    );
  };
  await load();
  return { el, title: "Admin" };
}
