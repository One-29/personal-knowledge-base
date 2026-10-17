/** Decode persisted records once; missing legacy fields and invalid fields differ. */
import type { AnswerResponse, AnswerStatus, Conversation, ConversationTurn, WorkflowResponse, WorkflowStep } from "../types";
import { parseAnswer } from "../validation/answers";
import { isRecord, number, string } from "../validation/base";
import { parseCitation, parseEvidenceCandidate } from "../validation/evidence";
import { parseWorkflowStep } from "../validation/workflow";
import { normalizeWorkflowCitations } from "../workflow-citations";
import { recoverSources, savedWarnings, tryParse } from "./sources";

function oldStatus(refused: boolean, reason: unknown): AnswerStatus {
  if (!refused) return "answered";
  if (reason === "borderline_relevance" || reason === "context_incomplete") return "needs_review";
  if (reason === "invalid_citation" || reason === "no_citation" || reason === "invalid_source_selection") return "unverified";
  if (["llm_unavailable", "embedding_unavailable", "embedding_mismatch", "source_changed", "source_validation_failed", "source_context_limit"].includes(String(reason))) return "error";
  return "insufficient";
}

function recoverAnswer(value: unknown, question: string): AnswerResponse | null {
  if (!isRecord(value) || typeof value.content !== "string") return null;
  const citations = recoverSources(value.citations, parseCitation);
  const candidates = recoverSources(value.possible_sources === undefined ? [] : value.possible_sources, parseEvidenceCandidate);
  const unavailable_sources = [...savedWarnings(value.unavailable_sources), ...citations.unavailable_sources, ...candidates.unavailable_sources];
  // Pre-gray-zone records (9d15b8d) lack status/evidence/possible_sources;
  // refused, session_id, search_query and refusal_reason already existed.
  const normalized = {
    question, status: oldStatus(value.refused === true, value.refusal_reason), evidence: null, ...value,
    citations: citations.sources, possible_sources: candidates.sources,
  };
  const parsed = tryParse(parseAnswer, normalized);
  if (parsed) return { ...parsed, ...(unavailable_sources.length ? { unavailable_sources } : {}) };
  // Preserve readable text, but invalid status/score metadata cannot become follow-up facts.
  return { question, content: value.content, status: "unverified", refused: true, refusal_reason: "history_data_invalid",
    session_id: null, search_query: null, citations: citations.sources, possible_sources: candidates.sources, evidence: null,
    unavailable_sources: [...unavailable_sources, { index: null, reason: "历史回答字段损坏" }] };
}

function recoverStep(value: unknown): WorkflowStep | null {
  if (!isRecord(value) || typeof value.goal !== "string" || typeof value.query !== "string") return null;
  const citations = recoverSources(value.citations, parseCitation);
  const candidates = recoverSources(value.possible_sources === undefined ? [] : value.possible_sources, parseEvidenceCandidate);
  const unavailable_sources = [...savedWarnings(value.unavailable_sources), ...citations.unavailable_sources, ...candidates.unavailable_sources];
  const normalized = {
    conclusion: null, note: null, kind: "research", depends_on: [], source_scope: "topic", search_query: null,
    evidence: null, refusal_reason: null, evidence_origin: null, ...value,
    citations: citations.sources, possible_sources: candidates.sources,
  };
  const parsed = tryParse(parseWorkflowStep, normalized);
  return parsed ? { ...parsed, ...(unavailable_sources.length ? { unavailable_sources } : {}) } : null;
}

function recoverWorkflow(value: unknown, task: string): WorkflowResponse | null {
  if (!isRecord(value) || typeof value.answer !== "string") return null;
  const citations = recoverSources(value.citations, parseCitation);
  const unavailable_sources = [...savedWarnings(value.unavailable_sources), ...citations.unavailable_sources];
  const steps: WorkflowStep[] = [];
  if (Array.isArray(value.steps)) {
    for (const entry of value.steps) {
      const step = recoverStep(entry);
      if (step) steps.push(step);
      else unavailable_sources.push({ index: null, reason: "部分历史步骤字段损坏，综合正文仍保留" });
    }
  } else unavailable_sources.push({ index: null, reason: "历史步骤列表损坏" });
  return normalizeWorkflowCitations({ task: typeof value.task === "string" ? value.task : task,
    answer: value.answer, citations: citations.sources, steps,
    ...(unavailable_sources.length ? { unavailable_sources } : {}) });
}

const nonnegative = (value: unknown): number => number(value, "历史时间", 0, Number.MAX_SAFE_INTEGER);

function recoverTurn(value: unknown): ConversationTurn | null {
  if (!isRecord(value) || typeof value.question !== "string") return null;
  const common = { question: value.question, durationMs: tryParse(nonnegative, value.durationMs), at: tryParse(nonnegative, value.at) ?? 0 };
  if (value.kind === "workflow") {
    const result = recoverWorkflow(value.result, value.question);
    return result ? { ...common, kind: "workflow", result, answerText: result.answer } : null;
  }
  // Records before workflow support did not have a kind discriminator.
  if (value.kind !== undefined && value.kind !== "ask") return null;
  const answer = recoverAnswer(value.answer, value.question);
  return answer ? { ...common, kind: "ask", answer, answerText: answer.content } : null;
}

export function parseConversations(value: unknown): Conversation[] {
  if (!Array.isArray(value)) return [];
  const conversations: Conversation[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    if (!isRecord(entry) || typeof entry.id !== "string" || !entry.id || seen.has(entry.id)) continue;
    seen.add(entry.id);
    conversations.push({ id: entry.id, title: tryParse(string, entry.title) ?? "",
      at: tryParse(nonnegative, entry.at) ?? 0,
      turns: Array.isArray(entry.turns) ? entry.turns.map(recoverTurn).filter((turn): turn is ConversationTurn => turn !== null) : [] });
  }
  return conversations;
}
