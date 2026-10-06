import { h, icon } from "../ui.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { projectCard, watchProcessing } from "./common.js";

const FILTERS = [["all", "All"], ["completed", "Completed"], ["processing", "Processing"], ["draft", "Drafts"], ["failed", "Failed"]];

export async function render(ctx) {
  let filter = ctx.query.get("f") || "all";
  let projects = [];
  let stopWatch = () => {};
  const grid = h("div", { class: "grid-projects" });
  const chips = h("div", { class: "chips", style: { marginBottom: "16px" } });
  const paint = () => {
    stopWatch();
    chips.replaceChildren(...FILTERS.map(([k, label]) => h("button", { class: "chip", "aria-pressed": String(k === filter),
      onclick: () => { filter = k; history.replaceState({}, "", k === "all" ? "/projects" : `/projects?f=${k}`); paint(); } },
      t(label), h("span", { class: "subtle", text: ` ${k === "all" ? projects.length : projects.filter((p) => p.status === k).length}` }))));
    const list = filter === "all" ? projects : projects.filter((p) => p.status === filter);
    grid.replaceChildren(...(list.length ? list.map((p) => projectCard(p, { navigate: ctx.navigate, onChange: load }))
      : [h("div", { class: "empty", style: { gridColumn: "1 / -1" } }, h("div", { class: "ico" }, icon("grid")),
          h("strong", { text: t("No projects yet") }), h("span", { text: t("Your edits will appear here.") }),
          h("a", { class: "btn btn-primary", href: "/create" }, icon("spark"), t("Create video")))]));
    stopWatch = watchProcessing(grid, list, load);
  };
  const load = async () => { projects = (await api("/api/projects")).projects; paint(); };
  await load();
  const el = h("div", {},
    h("div", { class: "topbar" }, h("h1", { text: t("Projects") }),
      h("a", { class: "btn btn-primary hide-mobile", href: "/create" }, icon("plus"), t("Create video"))),
    chips, grid);
  return { el, title: t("Projects"), cleanup: () => stopWatch() };
}
