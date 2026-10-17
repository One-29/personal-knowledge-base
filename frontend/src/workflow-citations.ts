import type { Citation, WorkflowResponse } from "./types";
import { sameSource } from "./validation/evidence";

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
  const snapshots = new Map<number, Citation>();
  const conflicts = new Set<number>();
  for (const citation of [...result.citations, ...result.steps.flatMap(step => step.citations)]) {
    const previous = snapshots.get(citation.chunk_id);
    if (previous && !sameSource(previous, citation)) conflicts.add(citation.chunk_id);
    snapshots.set(citation.chunk_id, citation);
  }
  const clean = <T extends { citations: Citation[]; unavailable_sources?: { index: number | null; reason: string }[] }>(value: T): T => {
    const removed = value.citations.filter(citation => conflicts.has(citation.chunk_id));
    return removed.length ? { ...value, citations: value.citations.filter(citation => !conflicts.has(citation.chunk_id)),
      unavailable_sources: [...value.unavailable_sources ?? [], ...removed.map(citation => ({ index: citation.index, reason: "同一来源的历史快照冲突" }))] } : value;
  };
  const cleaned = { ...clean(result), steps: result.steps.map(clean) };
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
