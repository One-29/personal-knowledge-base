import type { AnswerResponse, AskStreamMetadata } from "../types";
import { boolean, enumeration, nullable, record, string } from "./base";
import { parseCandidates, parseCitations, parseRetrievalEvidence } from "./evidence";

export const ANSWER_STATUSES = ["answered", "needs_review", "insufficient", "unverified", "error"] as const;

export function parseAnswer(value: unknown, path = "answer"): AnswerResponse {
  const v = record(value, path);
  return { question: string(v.question, `${path}.question`), content: string(v.content, `${path}.content`),
    status: enumeration(v.status, ANSWER_STATUSES, `${path}.status`),
    session_id: nullable(v.session_id, string, `${path}.session_id`), search_query: nullable(v.search_query, string, `${path}.search_query`),
    citations: parseCitations(v.citations, `${path}.citations`), possible_sources: parseCandidates(v.possible_sources, `${path}.possible_sources`),
    evidence: nullable(v.evidence, parseRetrievalEvidence, `${path}.evidence`), refused: boolean(v.refused, `${path}.refused`),
    refusal_reason: nullable(v.refusal_reason, string, `${path}.refusal_reason`) };
}

export function parseMetadata(value: unknown, path = "metadata"): AskStreamMetadata {
  const v = record(value, path);
  return { question: string(v.question, `${path}.question`), search_query: string(v.search_query, `${path}.search_query`) };
}

export function parseDelta(value: unknown, path = "delta"): string {
  return string(record(value, path).content, `${path}.content`);
}
