// Compra de créditos. O preço vem SEMPRE do servidor (pacotes); o browser só escolhe o pacote.
import { h, icon, sheet, toast, fmtUsd, setBusy } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { state, setCredits } from "../state.js";

let paypalLoading = null;
function loadPayPal(clientId) {
  if (window.paypal) return Promise.resolve(window.paypal);
  if (!paypalLoading) {
    paypalLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture&components=buttons`;
      s.onload = () => resolve(window.paypal);
      s.onerror = () => { paypalLoading = null; reject(new Error("PayPal couldn't load")); };
      document.head.append(s);
    });
  }
  return paypalLoading;
}

export function buyPanel({ onPurchased } = {}) {
  const me = state.me;
  const pay = me.payments || {};
  const packs = pay.packs || [];
  let selected = packs[0] && packs[0].id;
  const usdPer = me.pricing ? me.pricing.credit_usd_value : 0.5;
  const status = h("p", { class: "form-error", role: "status" });
  const packsEl = h("div", { class: "packs", role: "radiogroup" });
  const paintPacks = () => packsEl.replaceChildren(...packs.map((p) => h("button", {
    class: "pack", role: "radio", "aria-checked": String(p.id === selected), type: "button",
    onclick: () => { selected = p.id; paintPacks(); },
  }, h("b", { text: fmtUsd(p.price_cents) }), h("span", { text: `${p.credits} ${t("credits")}` }),
    h("span", { class: "per", text: `${fmtUsd(Math.round(p.price_cents / p.credits))} / generation` }))));
  paintPacks();

  const ppBox = h("div", { style: { minHeight: "50px" } });
  if (pay.paypal && pay.paypal.client_id) {
    ppBox.append(h("div", { class: "skeleton", style: { height: "48px", borderRadius: "999px" } }));
    loadPayPal(pay.paypal.client_id).then((paypal) => {
      ppBox.replaceChildren();
      paypal.Buttons({
        style: { layout: "vertical", shape: "pill", label: "pay", height: 48 },
        createOrder: async () => {
          status.textContent = "";
          const r = await api("/api/payments/paypal/create-order", { method: "POST", body: { pack_id: selected } });
          return r.order_id;
        },
        onApprove: async (data) => {
          status.style.color = "var(--text-2)";
          status.textContent = "Confirming payment…";
          try {
            const r = await api(`/api/payments/paypal/capture/${data.orderID}`, { method: "POST" });
            setCredits(r.balance);
            status.style.color = "var(--ok)";
            status.textContent = "Payment confirmed. Credits added.";
            toast("Payment confirmed. Credits added.");
            onPurchased && onPurchased(r.balance);
          } catch (e) {
            status.style.color = "var(--danger)";
            status.textContent = e.message;
          }
        },
        onCancel: () => { status.textContent = ""; },
        onError: () => { status.style.color = "var(--danger)"; status.textContent = "Payment failed. No credits were added."; },
      }).render(ppBox);
    }).catch(() => { ppBox.replaceChildren(h("p", { class: "muted", text: "Card payments are unavailable right now." })); });
  } else {
    ppBox.append(h("p", { class: "notice" }, icon("info", "icon-sm"), h("span", { text: "Card payments aren't enabled yet. Use e-Mola below or a promo code." })));
  }

  // e-Mola (Moçambique): declaração + confirmação manual pelo operador. Nunca pede PIN.
  const emolaBody = h("div", { class: "stack hidden" });
  if (pay.emola_number) {
    const amount = h("input", { class: "input", type: "number", min: 1, inputmode: "numeric", placeholder: "200" });
    const ref = h("input", { class: "input", maxlength: 120, placeholder: "SMS reference (optional)" });
    const send = h("button", { class: "btn btn-block", type: "button", text: "I've paid — notify" });
    send.onclick = async () => {
      setBusy(send, true);
      try {
        await api("/api/payments/emola/declare", { method: "POST", body: { amount_mt: amount.value, reference: ref.value } });
        toast("Thanks! We'll add your credits after checking the transfer.");
        amount.value = ""; ref.value = "";
      } catch (e) { toast(e.message, "err"); } finally { setBusy(send, false); }
    };
    emolaBody.append(
      h("p", { class: "muted", text: "Send the payment from your own e-Mola app (or *898#). We never ask for your PIN." }),
      h("div", { class: "spread card card-tight" }, h("b", { class: "mono", style: { fontSize: "20px" }, text: pay.emola_number }),
        h("button", { class: "btn btn-sm", type: "button", "aria-label": "Copy number",
          onclick: () => { navigator.clipboard?.writeText(pay.emola_number); toast("Number copied"); } }, icon("copy", "icon-sm"))),
      h("label", { class: "field" }, h("span", { text: "Amount paid (MT)" }), amount),
      h("label", { class: "field" }, h("span", { text: "Reference" }), ref), send,
      h("p", { class: "subtle", text: "Manual confirmation — usually within a few hours." }));
  }

  const code = h("input", { class: "input", placeholder: "CODE", maxlength: 40, style: { textTransform: "uppercase" } });
  const redeem = h("button", { class: "btn", type: "button", text: t("Redeem") });
  redeem.onclick = async () => {
    if (!code.value.trim()) return;
    setBusy(redeem, true);
    try {
      const r = await api("/api/credits/redeem", { method: "POST", body: { code: code.value } });
      setCredits(r.balance);
      toast(`+${r.credits_added} ${t("credits")}`);
      code.value = "";
      onPurchased && onPurchased(r.balance);
    } catch (e) { toast(e.message, "err"); } finally { setBusy(redeem, false); }
  };

  return h("div", { class: "stack" },
    h("div", { class: "spread" },
      h("div", { class: "balance" }, h("b", { class: "mono", dataset: { credits: "" }, text: me.credits ?? 0 }), h("span", { class: "muted", text: t("credits") })),
      h("span", { class: "badge badge-accent", text: `1 ${t("credit")} = $${Number(usdPer).toFixed(2)}` })),
    h("p", { class: "muted", text: t("Each generation costs {n} credit ({usd}).", { n: (me.pricing?.credits_per_generation?.standard) || 1, usd: `$${Number(usdPer).toFixed(2)}` }) }),
    packs.length ? packsEl : null,
    h("div", { class: "stack-sm" }, h("h3", { text: t("Pay with card or PayPal") }), ppBox, status),
    pay.emola_number ? h("div", { class: "stack-sm" },
      h("button", { class: "btn btn-ghost", type: "button", style: { justifySelf: "start", paddingLeft: 0 },
        onclick: (e) => { emolaBody.classList.toggle("hidden"); } }, icon("card", "icon-sm"), t("Pay with e-Mola (Mozambique)")),
      emolaBody) : null,
    h("div", { class: "stack-sm" }, h("h3", { text: t("Have a code?") }), h("div", { class: "row" }, code, redeem)));
}

export function openBuySheet({ reason, onPurchased } = {}) {
  return sheet((close) => [
    h("h2", { text: t("Buy credits") }),
    reason ? h("p", { class: "notice warn", style: { margin: "10px 0 6px" } }, icon("info", "icon-sm"), h("span", { text: reason })) : null,
    h("div", { style: { marginTop: "14px" } }, buyPanel({ onPurchased: (b) => { onPurchased && onPurchased(b); setTimeout(close, 700); } })),
  ]);
}
