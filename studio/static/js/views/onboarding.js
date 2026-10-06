// Primeiro acesso: 3 ecrãs curtos e logo "Criar vídeo". Sem tutoriais longos.
import { h, icon } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { state, track } from "../state.js";

const SLIDES = [
  { title: "Add your clips", body: "Any videos or photos from your phone.", art: () => art("you") },
  { title: "Pick a reference", body: "An edit you like. The AI reads its cuts, rhythm and effects.", art: () => art("ref") },
  { title: "Get your edit", body: "Same style, your content. Fix anything in the editor.", art: () => flow() },
];

function art(kind) {
  return h("div", { class: "flow", style: { display: "flex" } },
    h("div", { class: `mini-phone ${kind}`, style: { width: "112px" } }, h("i"), h("b", { text: kind === "ref" ? "REF" : "YOU" })));
}
function flow() {
  return h("div", { class: "flow", style: { display: "flex" } },
    h("div", { class: "mini-phone ref" }, h("i"), h("b", { text: "REF" })), h("span", { class: "arrow" }, icon("right")),
    h("div", { class: "ai", text: "AI" }), h("span", { class: "arrow" }, icon("right")),
    h("div", { class: "mini-phone you" }, h("i"), h("b", { text: "YOU" })));
}

export function render(ctx) {
  let i = 0;
  const next = ctx.query.get("next");
  const finish = async (to) => {
    state.me.onboarded = true;
    track("onboarding_done");
    try { await api("/api/me", { method: "PATCH", body: { onboarded: true } }); } catch {}
    ctx.navigate(to, { replace: true });
  };
  const artBox = h("div", { class: "art" });
  const title = h("h1");
  const body = h("p", { class: "muted", style: { maxWidth: "34ch", margin: "0 auto" } });
  const dots = h("div", { class: "dots" }, SLIDES.map(() => h("i")));
  const nextBtn = h("button", { class: "btn btn-primary btn-lg btn-block" });
  const paint = () => {
    const s = SLIDES[i];
    artBox.replaceChildren(s.art());
    title.textContent = t(s.title);
    body.textContent = t(s.body);
    [...dots.children].forEach((d, k) => d.classList.toggle("on", k === i));
    nextBtn.textContent = i < SLIDES.length - 1 ? t("Next") : t("Create video");
  };
  nextBtn.onclick = () => { if (i < SLIDES.length - 1) { i++; paint(); } else finish("/create"); };
  paint();
  return h("div", { class: "onb" },
    h("span", { class: "subtle", text: t("Welcome to AwlsVelocity") }),
    h("div", { class: "slides stack" }, artBox, title, body), dots,
    h("div", { class: "stack-sm", style: { width: "min(420px,100%)" } }, nextBtn,
      h("button", { class: "btn btn-ghost btn-block", onclick: () => finish(next && next.startsWith("/") ? next : "/home"), text: t("Skip") })));
}
