import type { Citation, WorkflowResponse } from "./types";

function remapText(text: string, indexes: ReadonlyMap<number, number>): string {
  return text.replace(/\[(\d+)\]/g, (marker, rawIndex: string) => {
    const index = indexes.get(Number(rawIndex));
    return index === undefined ? marker : `[${index}]`;
  });
}

/**
 * Older saved workflows used local numbers in each step and could assign several
 * global numbers to the same chunk. Normalize both shapes without losing the
 * stored citation snapshots used when a document has since been replaced.
 */
export function normalizeWorkflowCitations(result: WorkflowResponse): WorkflowResponse {
  const byChunk = new Map<number, Citation>();
  const citations: Citation[] = [];
  const oldGlobalIndexes = new Map<number, number>();
  const canonical = (citation: Citation): Citation => {
    const previous = byChunk.get(citation.chunk_id);
    if (previous !== undefined) return previous;
    const value = { ...citation, index: citations.length + 1 };
    byChunk.set(citation.chunk_id, value);
    citations.push(value);
    return value;
  };

  for (const citation of result.citations) {
    oldGlobalIndexes.set(citation.index, canonical(citation).index);
  }

  const steps = result.steps.map((step) => {
    if (step.status !== "answered") return step;
    const localIndexes = new Map<number, number>();
    const seen = new Set<number>();
    const stepCitations: Citation[] = [];
    for (const citation of step.citations) {
      const resolved = canonical(citation);
      localIndexes.set(citation.index, resolved.index);
      if (!seen.has(resolved.chunk_id)) {
        stepCitations.push(resolved);
        seen.add(resolved.chunk_id);
      }
    }
    return {
      ...step,
      conclusion: step.conclusion === null
        ? null
        : remapText(step.conclusion, localIndexes),
      citations: stepCitations,
    };
  });

  return {
    ...result,
    steps,
    answer: remapText(result.answer, oldGlobalIndexes),
    citations,
  };
}
