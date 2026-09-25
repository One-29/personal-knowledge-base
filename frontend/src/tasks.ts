import { api } from "./api";
import { streamAnswer } from "./ask-stream";
import { ConversationStore } from "./conversations";
import { byId, isAbortError, queryAll } from "./dom";
import { bindCitations } from "./evidence";
import { escapeHtml, friendlyError, questionHeader, renderRichText } from "./format";
import { notify } from "./notifications";
import { controllerFor, setController, state } from "./state";
import { focusLatest, initComposer, mountRunState, resizeComposer, setTaskBusy } from "./task-runtime";
import {
  renderAnswerEntry,
  renderStreamingAnswerEntry,
  renderWorkflowEntry,
} from "./task-rendering";
import type {
  ActivityOutcome,
  AskStreamMetadata,
  WorkflowResponse,
} from "./types";
let conversations: ConversationStore;
let savedTimer: number | null = null;

function flashSaved(): void {
  const status = byId("ask-saved");
  status.textContent = "已保存到本地";
  if (savedTimer !== null) window.clearTimeout(savedTimer);
  savedTimer = window.setTimeout(() => { status.textContent = ""; }, 1800);
}

function renderConversationSelect(): void {
  const select = byId<HTMLSelectElement>("ask-conv");
  const items = conversations.load();
  select.innerHTML = items.map((conversation) => (
    `<option value="${escapeHtml(conversation.id)}">${escapeHtml(conversation.title || "新会话")} · ${conversation.turns.length} 轮</option>`
  )).join("");
  const active = conversations.active();
  if (active !== null) select.value = active.id;
  else if (items[0] !== undefined) select.value = items[0].id;
}

function useConversation(id: string): void {
  if (!conversations.use(id)) return;
  renderConversation();
  renderConversationSelect();
}

function dropConversation(id: string): void {
  conversations.drop(id);
  renderConversation();
  renderConversationSelect();
}

function rememberTurn(
  conversationId: string,
  turn: Parameters<ConversationStore["remember"]>[1],
): boolean {
  const saved = conversations.remember(conversationId, turn);
  if (saved) {
    renderConversationSelect();
    flashSaved();
  }
  return saved;
}

export function renderConversation(): void {
  const flow = byId("ask-flow");
  const welcome = byId("ask-welcome");
  const conversation = conversations.active();
  flow.replaceChildren();
  if (conversation === null || conversation.turns.length === 0) {
    welcome.classList.remove("hidden");
    return;
  }
  welcome.classList.add("hidden");
  for (const turn of conversation.turns) {
    const entry = document.createElement("article");
    entry.className = "entry";
    if (turn.kind === "workflow") {
      entry.innerHTML = renderWorkflowEntry(turn.question, turn.result, turn.durationMs);
      bindCitations(entry, turn.result.citations);
    } else {
      entry.innerHTML = renderAnswerEntry(turn.question, turn.answer, turn.durationMs);
      bindCitations(entry, turn.answer.citations);
    }
    flow.append(entry);
  }
  window.requestAnimationFrame(() => {
    flow.querySelector(".entry:last-child")?.scrollIntoView({ block: "end" });
  });
}

async function submitQuestion(): Promise<void> {
  if (controllerFor("ask") !== null) {
    notify("上一个提问还在进行中", true);
    return;
  }
  const input = byId<HTMLTextAreaElement>("ask-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  resizeComposer("ask-input");

  const conversation = conversations.ensure();
  const flow = byId("ask-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  byId("ask-welcome").classList.add("hidden");
  flow.append(entry);
  const stopRunState = mountRunState(entry, question, "ask");
  focusLatest(entry);

  const controller = new AbortController();
  setController("ask", controller);
  setTaskBusy("ask", true);
  const startedAt = Date.now();
  let outcome: ActivityOutcome = "done";
  let metadata: AskStreamMetadata | null = null;
  let streamedContent = "";
  let streamMounted = false;
  let streamFrame: number | null = null;

  const flushStream = (): void => {
    streamFrame = null;
    if (!entry.isConnected || !streamMounted) return;
    const content = entry.querySelector<HTMLElement>("[data-stream-content]");
    if (content !== null) content.innerHTML = renderRichText(streamedContent, false);
  };
  const queueStreamRender = (): void => {
    if (streamFrame === null) streamFrame = window.requestAnimationFrame(flushStream);
  };
  try {
    const answer = await streamAnswer(
      {
        question,
        kb_id: state.askKbId,
        history: conversations.history(conversation),
      },
      {
        signal: controller.signal,
        handlers: {
          onMetadata: (value) => {
            metadata = value;
            const phase = entry.querySelector<HTMLElement>("[data-run-phase]");
            if (phase !== null) phase.textContent = "已完成检索，正在连接回答生成…";
          },
          onDelta: (content) => {
            streamedContent += content;
            if (!streamMounted) {
              stopRunState();
              streamMounted = true;
              entry.innerHTML = renderStreamingAnswerEntry(
                question,
                metadata?.search_query ?? null,
                streamedContent,
              );
              focusLatest(entry);
              return;
            }
            queueStreamRender();
          },
        },
      },
    );
    if (streamFrame !== null) {
      window.cancelAnimationFrame(streamFrame);
      streamFrame = null;
    }
    const durationMs = Date.now() - startedAt;
    entry.innerHTML = renderAnswerEntry(question, answer, durationMs);
    bindCitations(entry, answer.citations);
    const saved = rememberTurn(conversation.id, {
      kind: "ask",
      question,
      answer,
      answerText: answer.content,
      durationMs,
      at: Date.now(),
    });
    if (!saved) {
      notify("发起提问的会话已被删除，本次结果未保存", true);
    } else if (conversations.active()?.id === conversation.id && !entry.isConnected) {
      renderConversation();
    }
    if (entry.isConnected) focusLatest(entry);
  } catch (error) {
    if (streamFrame !== null) {
      window.cancelAnimationFrame(streamFrame);
      streamFrame = null;
    }
    if (isAbortError(error)) {
      outcome = "stopped";
      entry.innerHTML = `${questionHeader(question)}
        <div class="inline-state stopped"><b>已停止等待这次回答</b>问题没有保存到会话。已经发出的服务端模型调用可能仍会执行完。</div>`;
    } else {
      outcome = "error";
      entry.innerHTML = `${questionHeader(question)}
        <div class="inline-state error"><b>没有取得回答</b>${escapeHtml(friendlyError(error, "问答"))}。你可以检查服务后重新发送。</div>`;
    }
    if (!input.value) {
      input.value = question;
      resizeComposer("ask-input");
    }
  } finally {
    stopRunState();
    setController("ask", null);
    setTaskBusy("ask", false, outcome);
  }
}

async function submitWorkflow(): Promise<void> {
  if (controllerFor("trace") !== null) {
    notify("上一个任务还在进行中", true);
    return;
  }
  const input = byId<HTMLTextAreaElement>("trace-input");
  const task = input.value.trim();
  if (!task) return;
  input.value = "";
  resizeComposer("trace-input");

  const conversation = conversations.ensure();
  const flow = byId("trace-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  byId("trace-welcome").classList.add("hidden");
  flow.append(entry);
  const stopRunState = mountRunState(entry, task, "trace");
  focusLatest(entry);

  const knowledgeBase = byId<HTMLSelectElement>("trace-kb").value;
  const controller = new AbortController();
  setController("trace", controller);
  setTaskBusy("trace", true);
  const startedAt = Date.now();
  let outcome: ActivityOutcome = "done";
  try {
    const result = await api<WorkflowResponse>("/workflow", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({ task, kb_id: knowledgeBase ? Number(knowledgeBase) : null }),
    });
    const durationMs = Date.now() - startedAt;
    entry.innerHTML = renderWorkflowEntry(task, result, durationMs);
    bindCitations(entry, result.citations);
    const saved = rememberTurn(conversation.id, {
      kind: "workflow",
      question: task,
      result,
      answerText: result.answer,
      durationMs,
      at: Date.now(),
    });
    if (!saved) notify("发起任务的会话已被删除，本次结果未保存", true);
    focusLatest(entry);
  } catch (error) {
    if (isAbortError(error)) {
      outcome = "stopped";
      entry.innerHTML = `${questionHeader(task)}
        <div class="inline-state stopped"><b>已停止等待这个工作流</b>任务没有保存到会话。已经发出的服务端模型调用可能仍会执行完。</div>`;
    } else {
      outcome = "error";
      entry.innerHTML = `${questionHeader(task)}
        <div class="inline-state error"><b>工作流没有完成</b>${escapeHtml(friendlyError(error, "执行工作流"))}。你可以检查服务后重新执行。</div>`;
    }
    if (!input.value) {
      input.value = task;
      resizeComposer("trace-input");
    }
  } finally {
    stopRunState();
    setController("trace", null);
    setTaskBusy("trace", false, outcome);
  }
}

export function initTasks(store: ConversationStore): void {
  conversations = store;
  initComposer("ask-input", "ask-form");
  initComposer("trace-input", "trace-form");
  conversations.ensure();
  renderConversationSelect();
  renderConversation();

  const askKnowledgeBase = byId<HTMLSelectElement>("ask-kb");
  askKnowledgeBase.addEventListener("change", () => {
    state.askKbId = askKnowledgeBase.value ? Number(askKnowledgeBase.value) : null;
  });
  const conversationSelect = byId<HTMLSelectElement>("ask-conv");
  conversationSelect.addEventListener("change", () => {
    if (conversationSelect.value) useConversation(conversationSelect.value);
  });
  byId("ask-new").addEventListener("click", () => {
    conversations.start();
    renderConversation();
    renderConversationSelect();
    notify("已新建会话");
  });
  byId("ask-drop").addEventListener("click", () => {
    const conversation = conversations.active();
    if (conversation === null) {
      notify("还没有会话", true);
      return;
    }
    if (!window.confirm(`删除会话「${conversation.title || "新会话"}」？本机保存的记录会一并清除。`)) return;
    dropConversation(conversation.id);
    notify("会话已删除");
  });
  byId("ask-stop").addEventListener("click", () => {
    controllerFor("ask")?.abort();
    if (controllerFor("ask") !== null) notify("浏览器已停止等待；服务端可能仍在完成已经发出的模型调用");
  });
  byId("trace-stop").addEventListener("click", () => {
    controllerFor("trace")?.abort();
    if (controllerFor("trace") !== null) notify("浏览器已停止等待；服务端可能仍在完成已经发出的模型调用");
  });
  queryAll<HTMLButtonElement>("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = byId<HTMLTextAreaElement>("ask-input");
      input.value = button.dataset.prompt ?? "";
      resizeComposer("ask-input");
      input.focus();
    });
  });
  byId<HTMLFormElement>("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitQuestion();
  });
  byId<HTMLFormElement>("trace-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitWorkflow();
  });
}
