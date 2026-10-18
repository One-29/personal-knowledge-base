import type { Citation, EvidenceCandidate, SourceRecovery } from "../types";

/** Quarantine every conflicting occurrence; keep each numbering scope unchanged. */
export function quarantineSourceConflicts<T extends SourceRecovery & { citations: Citation[]; possible_sources?: EvidenceCandidate[] }>(
  value: T,
  conflicts: ReadonlySet<number>,
): T {
  const removed = [...value.citations, ...value.possible_sources ?? []].filter(source => conflicts.has(source.chunk_id));
  if (!removed.length) return value;
  return { ...value,
    citations: value.citations.filter(source => !conflicts.has(source.chunk_id)),
    ...(value.possible_sources ? { possible_sources: value.possible_sources.filter(source => !conflicts.has(source.chunk_id)) } : {}),
    unavailable_sources: [...value.unavailable_sources ?? [], ...removed.map(source => ({ index: source.index, reason: "同一来源的历史快照冲突" }))],
  };
}
