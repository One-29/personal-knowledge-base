import type { Citation, CitationDetail, DocumentImage, EvidenceCandidate, RetrievalEvidence } from "../types";
import { array, count, enumeration, id, interval, nullable, number, record, string, unique, ValidationError } from "./base";

export const similarity = (value: unknown, path = "similarity"): number => number(value, path, -1, 1);

export function parseImage(value: unknown, path = "image"): DocumentImage {
  const v = record(value, path);
  const content_url = string(v.content_url, `${path}.content_url`);
  if (!/^\/api\/v1\/documents\/[1-9]\d*\/versions\/[1-9]\d*\/images\/[1-9]\d*$/.test(content_url)) {
    throw new ValidationError(`${path}.content_url`, "必须是本机文档版本的图片接口路径");
  }
  const content_hash = string(v.content_hash, `${path}.content_hash`);
  if (!/^[a-f0-9]{64}$/.test(content_hash)) throw new ValidationError(`${path}.content_hash`, "必须是 SHA-256 摘要");
  return {
    ordinal: id(v.ordinal, `${path}.ordinal`),
    source_reference: string(v.source_reference, `${path}.source_reference`),
    alt_text: string(v.alt_text, `${path}.alt_text`), ...interval(v, path), content_hash,
    mime_type: enumeration(v.mime_type, ["image/png", "image/jpeg", "image/webp"], `${path}.mime_type`),
    file_size: id(v.file_size, `${path}.file_size`), width: id(v.width, `${path}.width`), height: id(v.height, `${path}.height`), content_url,
  };
}

export function parseImages(value: unknown, path: string, start: number, end: number, intersecting = false): DocumentImage[] {
  const images = unique(array(value, parseImage, path), image => image.ordinal, path);
  let previousEnd = 0;
  for (const image of images) {
    const inRange = intersecting
      ? image.char_start < end && image.char_end > start
      : image.char_start >= start && image.char_end <= end;
    if (image.char_start < previousEnd || !inRange) throw new ValidationError(path, "图片区间必须有序、不重叠且对应原文区间");
    previousEnd = image.char_end;
  }
  return images;
}

export function parseCitationDetail(value: unknown, path = "citation"): CitationDetail {
  const v = record(value, path);
  const range = interval(v, path);
  return { doc_id: id(v.doc_id, `${path}.doc_id`), doc_title: string(v.doc_title, `${path}.doc_title`),
    chunk_text: string(v.chunk_text, `${path}.chunk_text`), ...range,
    // Backend includes any image intersecting a citation, including legacy chunks
    // whose boundary falls inside Markdown image syntax.
    images: parseImages(v.images, `${path}.images`, range.char_start, range.char_end, true) };
}

export function parseCitation(value: unknown, path = "citation"): Citation {
  const v = record(value, path);
  return { index: id(v.index, `${path}.index`), chunk_id: id(v.chunk_id, `${path}.chunk_id`), ...parseCitationDetail(v, path) };
}

export function parseCitations(value: unknown, path: string): Citation[] {
  return consistentSources(unique(array(value, parseCitation, path), citation => citation.index, path), path);
}

export function parseEvidenceCandidate(value: unknown, path = "candidate"): EvidenceCandidate {
  const v = record(value, path);
  return { ...parseCitation(v, path), vector_similarity: nullable(v.vector_similarity, similarity, `${path}.vector_similarity`),
    rrf_score: number(v.rrf_score, `${path}.rrf_score`, 0),
    vector_rank: nullable(v.vector_rank, id, `${path}.vector_rank`), keyword_rank: nullable(v.keyword_rank, id, `${path}.keyword_rank`) };
}

export function parseCandidates(value: unknown, path: string): EvidenceCandidate[] {
  return consistentSources(unique(array(value, parseEvidenceCandidate, path), citation => citation.index, path), path);
}

function consistentSources<T extends Citation>(sources: T[], path: string): T[] {
  const chunks = new Map<number, Citation>();
  for (const source of sources) {
    const previous = chunks.get(source.chunk_id);
    if (previous && !sameSource(previous, source)) throw new ValidationError(path, "同一引用 ID 对应不同来源快照");
    chunks.set(source.chunk_id, source);
  }
  return sources;
}

export function parseRetrievalEvidence(value: unknown, path = "evidence"): RetrievalEvidence {
  const v = record(value, path);
  const refusal_threshold = similarity(v.refusal_threshold, `${path}.refusal_threshold`);
  const answer_threshold = similarity(v.answer_threshold, `${path}.answer_threshold`);
  if (refusal_threshold >= answer_threshold) throw new ValidationError(`${path}.answer_threshold`, "必须大于 refusal_threshold");
  return { band: enumeration(v.band, ["sufficient", "borderline", "insufficient"], `${path}.band`),
    candidate_count: count(v.candidate_count, `${path}.candidate_count`),
    max_vector_similarity: nullable(v.max_vector_similarity, similarity, `${path}.max_vector_similarity`), refusal_threshold, answer_threshold };
}

/** A shared chunk ID is not enough to merge conflicting saved snapshots. */
export function sameSource(left: Citation, right: Citation): boolean {
  return left.chunk_id === right.chunk_id && left.doc_id === right.doc_id
    && left.char_start === right.char_start && left.char_end === right.char_end
    && left.doc_title === right.doc_title && left.chunk_text === right.chunk_text
    && JSON.stringify(left.images) === JSON.stringify(right.images);
}
