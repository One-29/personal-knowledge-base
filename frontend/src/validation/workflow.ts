import type { WorkflowResponse, WorkflowStep } from "../types";
import { array, enumeration, id, nullable, record, string, unique, ValidationError } from "./base";
import { consistentSources, parseCandidates, parseCitations, parseRetrievalEvidence, sameSource } from "./evidence";

export function parseWorkflowStep(value: unknown, path = "step"): WorkflowStep {
  const v = record(value, path);
  return { index: id(v.index, `${path}.index`), goal: string(v.goal, `${path}.goal`), query: string(v.query, `${path}.query`),
    status: enumeration(v.status, ["answered", "needs_review", "insufficient", "error"], `${path}.status`),
    conclusion: nullable(v.conclusion, string, `${path}.conclusion`), note: nullable(v.note, string, `${path}.note`),
    citations: parseCitations(v.citations, `${path}.citations`), possible_sources: parseCandidates(v.possible_sources, `${path}.possible_sources`),
    kind: enumeration(v.kind, ["research", "source_lookup"], `${path}.kind`), depends_on: unique(array(v.depends_on, id, `${path}.depends_on`), n => n, `${path}.depends_on`),
    source_scope: enumeration(v.source_scope, ["topic", "all"], `${path}.source_scope`), search_query: nullable(v.search_query, string, `${path}.search_query`),
    evidence: nullable(v.evidence, parseRetrievalEvidence, `${path}.evidence`), refusal_reason: nullable(v.refusal_reason, string, `${path}.refusal_reason`),
    evidence_origin: nullable(v.evidence_origin, (item, p = path) => enumeration(item, ["retrieval", "previous_step"], p), `${path}.evidence_origin`) };
}

export function parseWorkflow(value: unknown, path = "workflow"): WorkflowResponse {
  const v = record(value, path);
  const citations = parseCitations(v.citations, `${path}.citations`);
  const steps = unique(array(v.steps, parseWorkflowStep, `${path}.steps`), step => step.index, `${path}.steps`);
  const previous = new Set<number>();
  const globals = new Map(citations.map(citation => [citation.index, citation]));
  for (const step of steps) {
    if (step.depends_on?.some(index => !previous.has(index) || index >= step.index)) throw new ValidationError(`${path}.steps.depends_on`, "只能引用已存在的前序步骤");
    for (const citation of step.citations) {
      const global = globals.get(citation.index);
      if (!global || !sameSource(global, citation)) throw new ValidationError(`${path}.steps.citations`, "与全局引用不一致");
    }
    previous.add(step.index);
  }
  consistentSources([...citations, ...steps.flatMap(step => [...step.citations, ...step.possible_sources])], `${path}.sources`);
  return { task: string(v.task, `${path}.task`), answer: string(v.answer, `${path}.answer`), citations, steps };
}
