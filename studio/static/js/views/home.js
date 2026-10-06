import { h, icon, fill } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { state } from "../state.js";
import { projectCard, watchProcessing } from "./common.js";

export async function render(ctx) {
  const me = state.me;
  const el = h("div");
  let stopWatch = () => {};
  const load = async () => {
    stopWatch();
    const [{ projects }, gallery] = await Promise.all([api("/api/projects"), api("/api/gallery").catch(() => ({ videos: [] }))]);
    const recent = projects.slice(0, 8);
    const hello = (me.display_name || "").trim();
    fill(el,
      h("div", { class: "topbar" },
        h("div", { class: "row" },
          h("a", { class: "brand-mark hide-desktop", href: "/home" }, h("img", { src: "/static/brand/logo.svg", width: 30, height: 30, alt: "" })),
          h("div", {}, h("p", { class: "subtle", text: hello ? `Hi, ${hello}` : "" }))),
        h("a", { class: "credits-pill hide-desktop", href: "/credits" },
          h("span", { class: "mono" }, h("span", { dataset: { credits: "" }, text: me.credits ?? 0 }), ` ${t("credits")}`),
          h("span", { class: "plus" }, icon("plus", "icon-sm")))),
      h("section", { class: "hero-create" },
        h("div", {},
          h("span", { class: "step-kicker", text: t("Edit from reference") }),
          h("h1", { style: { marginTop: "8px" }, text: t("Create a video from a reference") }),
          h("p", { class: "muted", text: t("Show the AI how your video should look. It recreates the edit with your own clips.") }),
          h("div", { class: "row-wrap" },
            h("a", { class: "btn btn-primary btn-lg", href: "/create" }, icon("spark"), t("Create video")),
            projects.find((p) => p.has_output && !p.sources_purged)
              ? h("a", { class: "btn btn-lg btn-ghost", href: `/edit/${projects.find((p) => p.has_output && !p.sources_purged).id}` }, icon("edit"), t("Edit"))
              : null)),
        h("div", { class: "flow" },
          h("div", { class: "mini-phone ref" }, h("i"), h("b", { text: "REF" })), h("span", { class: "arrow" }, icon("right")),
          h("div", { class: "ai", text: "AI" }), h("span", { class: "arrow" }, icon("right")),
          h("div", { class: "mini-phone you" }, h("i"), h("b", { text: "YOU" })))),
      h("div", { class: "section-title" }, h("h2", { text: t("Recent projects") }),
        projects.length > recent.length ? h("a", { href: "/projects", text: t("See all") }) : null),
      recent.length
        ? h("div", { class: "hscroll", id: "recent" }, recent.map((p) => projectCard(p, { navigate: ctx.navigate, onChange: load })))
        : h("div", { class: "empty" }, h("div", { class: "ico" }, icon("film")), h("strong", { text: t("No projects yet") }),
            h("span", { text: t("Your edits will appear here.") })),
      gallery.videos.length ? [
        h("div", { class: "section-title" }, h("h2", { text: t("From the community") })),
        h("div", { class: "hscroll" }, gallery.videos.map((v) => h("div", { class: "pcard" },
          h("div", { class: "thumb" }, h("video", { src: v.video_url, poster: v.thumbnail_url || null, muted: true, loop: true,
            playsinline: true, preload: "none", onmouseenter: (e) => e.target.play().catch(() => {}),
            onmouseleave: (e) => e.target.pause(), onclick: (e) => (e.target.paused ? e.target.play() : e.target.pause()) })),
          h("span", { class: "name", text: v.name }))))] : null,
    );
    const rc = el.querySelector("#recent");
    if (rc) stopWatch = watchProcessing(rc, recent, load);
  };
  el.append(h("div", { class: "skeleton", style: { height: "220px", borderRadius: "28px" } }));
  await load();
  return { el, title: t("Home"), cleanup: () => stopWatch() };
}
