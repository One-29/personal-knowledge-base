import "./styles/app.css";

import { ConversationStore } from "./conversations";
import { initEvidence } from "./evidence";
import { friendlyError } from "./format";
import { initGraph, loadGraph } from "./graph";
import { initLibrary, loadDocuments, loadKnowledgeBases } from "./library";
import { activateView, initNavigation, isViewName } from "./navigation";
import { notify } from "./notifications";
import { checkService, initServiceStatus, markServiceOffline } from "./service-status";
import { initTasks } from "./tasks";

async function start(): Promise<void> {
  initEvidence();
  initGraph();
  initLibrary();
  initNavigation({ docs: loadDocuments, map: loadGraph });
  initTasks(new ConversationStore(window.localStorage));
  initServiceStatus();
  void checkService();

  try {
    await loadKnowledgeBases();
    const requestedView = window.location.hash.slice(1);
    activateView(isViewName(requestedView) ? requestedView : "ask", false);
  } catch (error) {
    notify(friendlyError(error, "加载应用数据"), true);
    markServiceOffline();
  }
}

void start();
