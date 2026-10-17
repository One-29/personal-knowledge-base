import type { Conversation, HistoryTurn } from "../types";
import { parseQuestion } from "../validation/input";

/** Preparing a request is a separate boundary from preserving readable history. */
export function buildHistory(conversation: Conversation): HistoryTurn[] {
  const history: HistoryTurn[] = [];
  for (const turn of conversation.turns) {
    const usable = turn.kind === "ask"
      ? turn.answer.status === "answered" && !turn.answer.unavailable_sources?.length
      : !turn.result.unavailable_sources?.length
        && !turn.result.steps.some(step => step.unavailable_sources?.length)
        && turn.result.steps.some(step => step.status === "answered");
    if (!usable) continue;
    try {
      const question = parseQuestion(turn.question);
      const answer = Array.from(turn.answerText).slice(0, 1500).join("").trim();
      if (answer && !answer.includes("\0")) history.push({ question, answer });
    } catch {
      // Old questions can exceed today's request limit. Keep them on screen,
      // but do not make an otherwise valid new request fail validation.
    }
  }
  return history.slice(-6);
}
