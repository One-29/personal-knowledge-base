import { byId, queryAll } from "./dom";
import { state } from "./state";
import type { ViewName } from "./types";

interface ViewMetadata {
  kicker: string;
  title: string;
  description: string;
}

const VIEW_METADATA: Record<ViewName, ViewMetadata> = {
  ask: {
    kicker: "ASK YOUR LIBRARY",
    title: "知识问答",
    description: "从自己的资料中获得有出处、可核对的回答。",
  },
  trace: {
    kicker: "MULTI-STEP RESEARCH",
    title: "综合工作流",
    description: "将跨文档任务拆解为清晰步骤，并逐项说明材料是否充分。",
  },
  map: {
    kicker: "KNOWLEDGE CONNECTIONS",
    title: "知识关联图",
    description: "通过语义相似度观察文档之间的关系与主题聚类。",
  },
  docs: {
    kicker: "DOCUMENT LIBRARY",
    title: "文档管理",
    description: "查看处理状态、更新原文，并确认哪些资料已经可以检索。",
  },
  kbs: {
    kicker: "LIBRARY SPACES",
    title: "知识库",
    description: "按课程、项目或主题组织彼此独立的资料集合。",
  },
};

interface ViewHandlers {
  docs: () => void | Promise<void>;
  map: () => void | Promise<void>;
}

let handlers: ViewHandlers = {
  docs: () => undefined,
  map: () => undefined,
};

export function isViewName(value: string): value is ViewName {
  return Object.hasOwn(VIEW_METADATA, value);
}

export function activateView(viewName: string, updateHash = true): void {
  const name: ViewName = isViewName(viewName) ? viewName : "ask";
  state.activeView = name;
  queryAll<HTMLButtonElement>(".views button").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("on", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  queryAll<HTMLElement>(".view").forEach((view) => view.classList.remove("on"));
  byId(`view-${name}`).classList.add("on");

  const metadata = VIEW_METADATA[name];
  byId("view-kicker").textContent = metadata.kicker;
  byId("view-title").textContent = metadata.title;
  byId("view-description").textContent = metadata.description;
  document.title = `${metadata.title} · KnowBase`;

  if (updateHash && window.location.hash !== `#${name}`) {
    window.history.replaceState(null, "", `#${name}`);
  }
  if (name !== "docs" && state.docsPoll !== null) {
    window.clearTimeout(state.docsPoll);
    state.docsPoll = null;
  }
  if (name === "docs") void handlers.docs();
  if (name === "map") void handlers.map();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

export function initNavigation(viewHandlers: ViewHandlers): void {
  handlers = viewHandlers;
  queryAll<HTMLButtonElement>(".views button").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.view ?? "ask"));
  });
  window.addEventListener("hashchange", () => {
    activateView(window.location.hash.slice(1), false);
  });
}
