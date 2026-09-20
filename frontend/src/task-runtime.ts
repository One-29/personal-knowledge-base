import { byId, queryOne } from "./dom";
import { questionHeader } from "./format";
import { clearActivityTimers, controllerFor, state } from "./state";
import type { ActivityOutcome, TaskScope } from "./types";

const ACTIVITY_LABEL: Record<ActivityOutcome, string> = {
  done: "刚刚完成",
  error: "需要查看",
  stopped: "已停止等待",
  idle: "空闲",
};

const composerResize = new Map<string, () => void>();

export function setTaskBusy(
  scope: TaskScope,
  busy: boolean,
  outcome: ActivityOutcome = "idle",
): void {
  const isAsk = scope === "ask";
  const stop = byId<HTMLButtonElement>(isAsk ? "ask-stop" : "trace-stop");
  const send = byId<HTMLButtonElement>(isAsk ? "ask-send" : "trace-send");
  const navigationBusy = byId(isAsk ? "nav-ask-busy" : "nav-trace-busy");
  const activity = byId(isAsk ? "activity-ask" : "activity-trace");
  const activityText = queryOne<HTMLElement>("strong", activity);
  stop.classList.toggle("hidden", !busy);
  send.disabled = busy;
  navigationBusy.classList.toggle("hidden", !busy);
  clearActivityTimers(scope);

  if (busy) {
    const startedAt = Date.now();
    activity.dataset.status = "busy";
    const update = (): void => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      const minutesText = String(Math.floor(seconds / 60)).padStart(2, "0");
      const secondsText = String(seconds % 60).padStart(2, "0");
      activityText.textContent = `运行中 ${minutesText}:${secondsText}`;
    };
    update();
    state.activityTimers[scope] = window.setInterval(update, 1000);
    return;
  }

  activity.dataset.status = outcome;
  activityText.textContent = ACTIVITY_LABEL[outcome];
  if (outcome !== "idle") {
    state.activityResetTimers[scope] = window.setTimeout(() => {
      if (controllerFor(scope) === null) {
        activity.dataset.status = "idle";
        activityText.textContent = ACTIVITY_LABEL.idle;
      }
    }, 5000);
  }
}

export function focusLatest(entry: HTMLElement): void {
  window.requestAnimationFrame(() => {
    entry.scrollIntoView({ behavior: "smooth", block: "end" });
  });
}

export function mountRunState(
  entry: HTMLElement,
  question: string,
  scope: TaskScope,
): () => void {
  const isAsk = scope === "ask";
  const phases = isAsk
    ? ["正在提交问题并检索知识库…", "正在等待检索与回答生成…", "仍在处理较长的回答，请稍候…"]
    : ["正在规划任务并准备检索…", "正在逐步检索并整理材料…", "正在等待各步骤完成并汇总结论…"];
  const startedAt = Date.now();
  const update = (): void => {
    if (!entry.isConnected) return;
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    const phase = phases[seconds >= 12 ? 2 : seconds >= 3 ? 1 : 0] ?? phases[0] ?? "正在处理…";
    const phaseElement = entry.querySelector<HTMLElement>("[data-run-phase]");
    const timeElement = entry.querySelector<HTMLElement>("[data-run-time]");
    if (phaseElement !== null) phaseElement.textContent = phase;
    if (timeElement !== null) timeElement.textContent = `${seconds} 秒`;
  };
  entry.innerHTML = `${questionHeader(question)}
    <div class="run-state">
      <div class="run-state-head">
        <span class="spinner"></span>
        <strong data-run-phase>${phases[0] ?? "正在处理…"}</strong>
        <time data-run-time>0 秒</time>
      </div>
      <p>${isAsk ? "完成后会显示引用来源，期间可以切换到工作流。" : "完成后一次返回分步结果；期间可以切换到问答。"}</p>
      <div class="run-progress"></div>
    </div>`;
  const timer = window.setInterval(update, 1000);
  return () => window.clearInterval(timer);
}

export function initComposer(inputId: string, formId: string): void {
  const input = byId<HTMLTextAreaElement>(inputId);
  const resize = (): void => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 148)}px`;
  };
  composerResize.set(inputId, resize);
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      byId<HTMLFormElement>(formId).requestSubmit();
    }
  });
}

export function resizeComposer(inputId: string): void {
  composerResize.get(inputId)?.();
}
