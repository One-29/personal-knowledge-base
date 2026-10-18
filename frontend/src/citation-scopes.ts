import type { AnswerResponse, Citation, WorkflowResponse } from "./types";

export type CitationScopes = ReadonlyMap<string, readonly Citation[]>;

/** Names match the nearest data-source-scope container in task rendering. */
export function answerSourceScopes(answer: AnswerResponse): CitationScopes {
  return new Map([["citations", answer.citations], ["candidates", answer.possible_sources]]);
}

export function workflowSourceScopes(result: WorkflowResponse): CitationScopes {
  const scopes = new Map<string, readonly Citation[]>([["citations", result.citations]]);
  result.steps.forEach((step, position) => {
    scopes.set(`step-${position}`, step.citations);
    scopes.set(`step-${position}-candidates`, step.possible_sources);
  });
  return scopes;
}
