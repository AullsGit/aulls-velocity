import { h, icon, setBusy } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { loadMe } from "../state.js";

function googleButton(providers) {
  if (!providers || !providers.google) return null;  // sem botões falsos: só aparece se estiver configurado
  return [
    h("div", { class: "oauth" },
      h("a", { class: "btn btn-lg btn-block", href: providers.google_url },
        h("span", { "aria-hidden": "true", style: { fontWeight: 800 } }, "G"), t("Continue with Google"))),
    h("div", { class: "or", text: t("or") }),
  ];
}

export async function render(ctx) {
  if (ctx.view === "callback") return callback(ctx);
  const isSignup = ctx.view === "signup";
  let providers = null;
  try { providers = await api("/api/auth/providers"); } catch {}
  const next = ctx.query.get("next") || "/home";
  const err = h("div", { class: "form-error", role: "alert" });
  const email = h("input", { class: "input", type: "email", autocomplete: "email", required: true, placeholder: "you@email.com", id: "email" });
  const pw = h("input", { class: "input", type: "password", required: true, minlength: isSignup ? 8 : null,
    autocomplete: isSignup ? "new-password" : "current-password", placeholder: isSignup ? t("At least 8 characters") : "••••••••", id: "password" });
  const submit = h("button", { class: "btn btn-primary btn-lg btn-block", type: "submit", text: isSignup ? t("Create account") : t("Log in") });
  const form = h("form", { class: "stack", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    setBusy(submit, true);
    try {
      const r = await api(isSignup ? "/api/auth/signup" : "/api/auth/login", { method: "POST", body: { email: email.value, password: pw.value } });
      if (r.confirm_email) {
        err.style.color = "var(--ok)";
        err.textContent = t("Check your email to confirm your account, then log in.");
        return;
      }
      await loadMe();
      ctx.navigate(next.startsWith("/") ? next : "/home", { replace: true });
    } catch (e2) {
      err.textContent = e2.message;
    } finally { setBusy(submit, false); }
  } },
    h("label", { class: "field" }, h("span", { text: t("Email") }), email),
    h("label", { class: "field" }, h("span", { text: t("Password") }), pw),
    err, submit);

  const el = h("div", { class: "auth-page" },
    h("div", { class: "auth-card" },
      h("a", { class: "brand-mark", href: "/" }, h("img", { src: "/static/brand/logo.svg", width: 34, height: 34, alt: "" }), "AwlsVelocity"),
      h("div", { class: "card stack", style: { padding: "26px" } },
        h("div", { class: "stack-sm" },
          h("h1", { style: { fontSize: "26px" }, text: isSignup ? t("Create account") : t("Log in") }),
          h("p", { class: "muted", text: isSignup ? t("Show the AI how your video should look. It recreates the edit with your own clips.") : "" })),
        googleButton(providers), form),
      h("p", { class: "muted", style: { textAlign: "center", fontSize: "14px" } },
        isSignup ? t("Already have an account?") : t("New to AwlsVelocity?"), " ",
        h("a", { href: `${isSignup ? "/login" : "/signup"}${next !== "/home" ? `?next=${encodeURIComponent(next)}` : ""}`,
          style: { color: "var(--accent-text)", fontWeight: 600 }, text: isSignup ? t("Log in") : t("Sign up") }))));
  setTimeout(() => email.focus(), 50);
  return { el, title: isSignup ? t("Sign up") : t("Log in") };
}

async function callback(ctx) {
  // Volta do Google (via Supabase): tokens no fragmento do URL -> sessão httponly no servidor.
  const hash = new URLSearchParams(location.hash.slice(1));
  const el = h("div", { class: "auth-page" }, h("div", { class: "stack", style: { justifyItems: "center" } },
    h("span", { class: "spinner" }), h("p", { class: "muted", text: "Signing you in…" })));
  history.replaceState({}, "", "/auth/callback");  // os tokens não ficam no histórico
  try {
    await api("/api/auth/session", { method: "POST", body: { access_token: hash.get("access_token"), refresh_token: hash.get("refresh_token") } });
    await loadMe();
    setTimeout(() => ctx.navigate("/home", { replace: true }), 0);
  } catch (e) {
    el.replaceChildren(h("div", { class: "auth-card stack" },
      h("div", { class: "error-card" }, icon("alert"), h("p", { text: e.message })),
      h("a", { class: "btn btn-primary btn-block", href: "/login", text: t("Log in") })));
  }
  return el;
}
