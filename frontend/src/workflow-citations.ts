import type { Citation, WorkflowResponse } from "./types";
import { conflictingSourceIds } from "./validation/evidence";
import { quarantineSourceConflicts } from "./history/source-conflicts";

function remapText(text: string, indexes: ReadonlyMap<number, number>, markMissing = false): string {
  return text.replace(/\[(\d+)\]/g, (marker, rawIndex: string) => {
    const index = indexes.get(Number(rawIndex));
    return index === undefined ? (markMissing ? `[原引用 ${rawIndex} 不可用]` : marker) : `[${index}]`;
  });
}

/**
 * Older saved workflows used local numbers in each step and could assign several
 * global numbers to the same chunk. Normalize both shapes without losing the
 * stored citation snapshots used when a document has since been replaced.
 */
export function normalizeWorkflowCitations(result: WorkflowResponse): WorkflowResponse {
  const conflicts = conflictingSourceIds([...result.citations,
    ...result.steps.flatMap(step => [...step.citations, ...step.possible_sources])]);
  const cleaned = { ...quarantineSourceConflicts(result, conflicts),
    steps: result.steps.map(step => quarantineSourceConflicts(step, conflicts)) };
  // Damaged records retain each original numbering scope. Rendering uses that
  // scope's validated sources, so dropping an entry cannot retarget its marker.
  if (cleaned.unavailable_sources?.length || cleaned.steps.some(step => step.unavailable_sources?.length)) return cleaned;
  result = cleaned;
  const byChunk = new Map<number, Citation>();
  const citations: Citation[] = [];
  const oldGlobalIndexes = new Map<number, number>();
  const reserved = new Set(result.citations.map(citation => citation.index));
  for (const match of result.answer.matchAll(/\[(\d+)\]/g)) reserved.add(Number(match[1]));
  let next = 1;
  const canonical = (citation: Citation, preserveIndex = false): Citation => {
    const previous = byChunk.get(citation.chunk_id);
    if (previous !== undefined) return previous;
    while (reserved.has(next)) next += 1;
    const index = preserveIndex ? citation.index : next;
    reserved.add(index);
    const value = { ...citation, index };
    byChunk.set(citation.chunk_id, value);
    citations.push(value);
    return value;
  };

  for (const citation of result.citations) {
    oldGlobalIndexes.set(citation.index, canonical(citation, true).index);
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
        : remapText(step.conclusion, localIndexes, true),
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
