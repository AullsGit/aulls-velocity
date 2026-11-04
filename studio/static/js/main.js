// Arranque da app: tema, sessão, shell (sidebar no desktop / barra inferior no telemóvel), router.
import { h, icon } from "./ui.js";
import { t } from "./i18n.js";
import { state, loadMe, onMe, applyTheme, track } from "./state.js";

applyTheme();

const ROUTES = [
  ["/login", () => import("./views/auth.js"), { public: true, bare: true, view: "login" }],
  ["/signup", () => import("./views/auth.js"), { public: true, bare: true, view: "signup" }],
  ["/auth/callback", () => import("./views/auth.js"), { public: true, bare: true, view: "callback" }],
  ["/welcome", () => import("./views/onboarding.js"), { bare: true }],
  ["/home", () => import("./views/home.js"), { tab: "home" }],
  ["/agent", () => import("./views/agent.js"), { tab: "create" }],
  ["/agent/:id", () => import("./views/agent.js"), { tab: "create" }],
  ["/create", () => import("./views/create.js"), { tab: "create" }],
  ["/create/:id", () => import("./views/create.js"), { tab: "create" }],
  ["/projects", () => import("./views/projects.js"), { tab: "projects" }],
  ["/projects/:id", () => import("./views/project.js"), { tab: "projects" }],
  ["/edit/:id", () => import("./views/editor.js"), { bare: true }],
  ["/profile", () => import("./views/profile.js"), { tab: "profile" }],
  ["/credits", () => import("./views/profile.js"), { tab: "profile", view: "credits" }],
  ["/admin", () => import("./views/admin.js"), { tab: "admin", admin: true }],
];

function match(path) {
  for (const [pattern, load, opts] of ROUTES) {
    const keys = [];
    const re = new RegExp("^" + pattern.replace(/:(\w+)/g, (_, k) => { keys.push(k); return "([^/]+)"; }) + "/?$");
    const m = path.match(re);
    if (m) return { load, opts, params: Object.fromEntries(keys.map((k, i) => [k, decodeURIComponent(m[i + 1])])) };
  }
  return null;
}

const root = document.getElementById("app");
let cleanup = null;
let navSeq = 0;

export function navigate(to, { replace = false } = {}) {
  if (to === location.pathname + location.search) return render();
  history[replace ? "replaceState" : "pushState"]({}, "", to);
  render();
}
window.awlsNavigate = navigate;

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || a.target || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/s/") || a.hasAttribute("download")) return;
  if (url.pathname === "/" ) return;
  e.preventDefault();
  navigate(url.pathname + url.search);
});
window.addEventListener("popstate", () => render());
window.addEventListener("awls:unauthorized", () => {
  state.me = { ...(state.me || {}), logged_in: false };
  navigate(`/login?next=${encodeURIComponent(location.pathname)}`, { replace: true });
});

function navLinks(active) {
  const me = state.me || {};
  const items = [
    ["home", "/home", "home", "Home"], ["create", "/agent", "plus", "Create"],
    ["projects", "/projects", "grid", "Projects"], ["profile", "/profile", "user", "Profile"],
  ];
  const side = h("aside", { class: "sidebar" },
    h("a", { class: "brand-mark", href: "/home" }, h("img", { src: "/static/brand/logo.svg", width: 30, height: 30, alt: "" }), "AwlsVelocity"),
    h("a", { class: "btn btn-primary btn-block side-create", href: "/agent" }, icon("spark"), t("Create video")),
    items.filter((i) => i[0] !== "create").map(([k, href, ic, label]) =>
      h("a", { class: `side-link ${active === k ? "active" : ""}`, href }, icon(ic), t(label))),
    me.is_admin ? h("a", { class: `side-link ${active === "admin" ? "active" : ""}`, href: "/admin" }, icon("shield"), t("Admin")) : null,
    h("div", { class: "grow" }),
    h("a", { class: "credits-pill", href: "/credits", id: "side-credits" },
      h("span", { class: "mono", text: `${me.credits ?? 0} ${t("credits")}` }), h("span", { class: "plus" }, icon("plus", "icon-sm"))),
  );
  const tabs = h("nav", { class: "tabbar", "aria-label": "Main" },
    items.map(([k, href, ic, label]) => k === "create"
      ? h("a", { class: "tab tab-create", href, "aria-label": t("Create video") }, h("span", { class: "fab" }, icon("plus")))
      : h("a", { class: `tab ${active === k ? "active" : ""}`, href }, icon(ic), h("span", { text: t(label) }))));
  return [side, tabs];
}

onMe((me) => {
  const pill = document.getElementById("side-credits");
  if (pill) pill.firstChild.textContent = `${me.credits ?? 0} ${t("credits")}`;
  document.querySelectorAll("[data-credits]").forEach((el) => { el.textContent = me.credits ?? 0; });
});

async function render() {
  const seq = ++navSeq;
  const path = location.pathname;
  const m = match(path);
  if (!m) { location.replace("/home"); return; }
  if (!state.me) await loadMe();
  const me = state.me;
  const next = encodeURIComponent(path + location.search);
  if (!m.opts.public && !me.logged_in) return navigate(`/login?next=${next}`, { replace: true });
  if (m.opts.public && me.logged_in && m.opts.view !== "callback") return navigate("/home", { replace: true });
  if (me.logged_in && !me.onboarded && path !== "/welcome" && !m.opts.public) return navigate(`/welcome?next=${next}`, { replace: true });
  if (m.opts.admin && !me.is_admin) return navigate("/home", { replace: true });

  if (cleanup) { try { cleanup(); } catch {} cleanup = null; }
  const mod = await m.load();
  if (seq !== navSeq) return;
  const ctx = { params: m.params, query: new URLSearchParams(location.search), navigate, view: m.opts.view, me };
  const out = await mod.render(ctx);
  if (seq !== navSeq) { out && out.cleanup && out.cleanup(); return; }
  cleanup = out && out.cleanup;
  const viewEl = out && out.el ? out.el : out;
  viewEl.classList && viewEl.classList.add("view-enter");
  if (m.opts.bare) root.replaceChildren(viewEl);
  else root.replaceChildren(...navLinks(m.opts.tab), h("main", { class: `main ${out.full ? "full" : ""}`, id: "main" }, viewEl));
  window.scrollTo(0, 0);
  document.title = (out && out.title ? `${out.title} — ` : "") + "AwlsVelocity";
}

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); window.awlsInstall = e; track("install_prompt"); });
window.addEventListener("appinstalled", () => track("install_accepted"));

track("app_open");
render();
