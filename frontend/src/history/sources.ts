import type { Citation, SourceRecovery, UnavailableSource } from "../types";
import { id, isRecord, ValidationError, type Parser } from "../validation/base";
import { sameSource } from "../validation/evidence";

export function tryParse<T>(parse: Parser<T>, value: unknown): T | null {
  try { return parse(value); } catch (error) {
    if (error instanceof ValidationError) return null;
    throw error;
  }
}

export function savedWarnings(value: unknown): UnavailableSource[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map(item => ({ index: tryParse(id, item.index), reason: "历史来源无法恢复" }));
}

/** Invalid entries have no chunk identity and never enter citation deduplication. */
export function recoverSources<T extends Citation>(value: unknown, parse: Parser<T>): { sources: T[] } & Required<SourceRecovery> {
  if (!Array.isArray(value)) return { sources: [], unavailable_sources: [{ index: null, reason: "来源列表格式损坏" }] };
  const entries = value.map((raw: unknown) => {
    // Image lists were added after text-only citation snapshots.
    const legacy = isRecord(raw) && raw.images === undefined ? { ...raw, images: [] } : raw;
    return { index: isRecord(raw) ? tryParse(id, raw.index) : null, source: tryParse(parse, legacy) };
  });
  const unavailable_sources: UnavailableSource[] = [];
  const sources: T[] = [];
  for (const entry of entries) {
    const source = entry.source;
    const ambiguous = source !== null && entries.some(other => other !== entry && (
      (other.index === entry.index && (other.source === null || !sameSource(source, other.source)))
      || (other.source?.chunk_id === source.chunk_id && !sameSource(source, other.source))
    ));
    if (source === null || ambiguous) {
      unavailable_sources.push({ index: entry.index, reason: ambiguous ? "来源编号或快照存在冲突" : "来源字段损坏" });
    } else if (!sources.some(saved => saved.index === source.index)) {
      sources.push(source);
    }
  }
  return { sources, unavailable_sources };
}
