import type {
  ActivityOutcome,
  KnowledgeBase,
  KnowledgeDocument,
  TaskScope,
  ViewName,
} from "./types";

interface TimerState {
  ask: number | null;
  trace: number | null;
}

interface ControllerState {
  ask: AbortController | null;
  trace: AbortController | null;
}

export interface AppState {
  kbs: KnowledgeBase[];
  docs: KnowledgeDocument[];
  docsKbId: number | null;
  askKbId: number | null;
  activeView: ViewName;
  docsPoll: number | null;
  activityTimers: TimerState;
  activityResetTimers: TimerState;
  controllers: ControllerState;
}

export const state: AppState = {
  kbs: [],
  docs: [],
  docsKbId: null,
  askKbId: null,
  activeView: "ask",
  docsPoll: null,
  activityTimers: { ask: null, trace: null },
  activityResetTimers: { ask: null, trace: null },
  controllers: { ask: null, trace: null },
};

export function controllerFor(scope: TaskScope): AbortController | null {
  return state.controllers[scope];
}

export function setController(scope: TaskScope, controller: AbortController | null): void {
  state.controllers[scope] = controller;
}

export function clearActivityTimers(scope: TaskScope): void {
  const activityTimer = state.activityTimers[scope];
  if (activityTimer !== null) window.clearInterval(activityTimer);
  state.activityTimers[scope] = null;
  const resetTimer = state.activityResetTimers[scope];
  if (resetTimer !== null) window.clearTimeout(resetTimer);
  state.activityResetTimers[scope] = null;
}

export function isActivityOutcome(value: string): value is ActivityOutcome {
  return ["idle", "done", "error", "stopped"].includes(value);
}
