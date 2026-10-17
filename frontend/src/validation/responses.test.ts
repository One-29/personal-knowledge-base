import { describe, expect, it } from "vitest";
import { parseCitation, parseCitations, parseEvidenceCandidate, parseRetrievalEvidence } from "./evidence";
import { parseGraph } from "./graph";
import { parseKnowledgeBase, parseDocument, parseDocumentContent, parseUploadResult } from "./library";
import { parseWorkflow } from "./workflow";
import { parseDiagnostics } from "./diagnostics";
import { parseAnswer } from "./answers";

export const source = { index: 1, chunk_id: 7, doc_id: 3, doc_title: "笔记", chunk_text: "原文", char_start: 0, char_end: 2, images: [] };
export const answer = { question: "问题", content: "回答。[1]", status: "answered", session_id: null, search_query: "问题", citations: [source], possible_sources: [], evidence: null, refused: false, refusal_reason: null };
export const graph = { nodes: [1, 2].map(doc_id => ({ doc_id, title: "笔记", chunks: 2, chars: 9 })), edges: [{ source: 1, target: 2, links: 1, weight: 1, similarity: 0.8 }], truncated: false };
export const document = { id: 3, kb_id: 1, title: "笔记.md", status: "ready", char_count: 2, chunk_count: 1, image_count: 0, task: null, last_error_code: null, last_error_message: null, processed_at: null, created_at: "2026-10-17T01:00:00Z", updated_at: "2026-10-17T01:00:00Z" };
export const workflow = { task: "比较", answer: "回答。[1]", citations: [source], steps: [{ index: 1, goal: "比较", query: "比较", status: "answered", conclusion: "回答。[1]", note: null, citations: [source], possible_sources: [], kind: "research", depends_on: [], source_scope: "topic", search_query: "比较", evidence: null, refusal_reason: null, evidence_origin: "retrieval" }] };

describe("response boundaries", () => {
  it("accepts normal responses from each domain", () => {
    expect(parseCitation(source)).toEqual(source);
    expect(parseAnswer(answer)).toEqual(answer);
    expect(parseGraph(graph)).toEqual(graph);
    expect(parseWorkflow(workflow)).toEqual(workflow);
    expect(parseDocument(document)).toEqual(document);
    expect(parseUploadResult({ document, content_changed: true }).document.id).toBe(3);
    expect(parseKnowledgeBase({ id: 1, name: "笔记", description: null, doc_count: 0, created_at: document.created_at }).id).toBe(1);
    expect(parseDiagnostics({ request_id: "test", report: "摘要", log_path: null, privacy_notice: "不含原文" }).report).toBe("摘要");
  });
  it.each(["7", 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, NaN, Infinity, null, undefined])("rejects unsafe IDs in every reference field: %s", value => {
    for (const field of ["index", "chunk_id", "doc_id"]) expect(() => parseCitation({ ...source, [field]: value })).toThrow(field);
    expect(() => parseDocument({ ...document, id: value })).toThrow("id");
  });
  it.each([-1, 0.5, Number.MAX_SAFE_INTEGER + 1, "0"])("rejects invalid positions: %s", value => {
    expect(() => parseCitation({ ...source, char_start: value })).toThrow("char_start");
  });
  it("checks interval order, enums, required fields and boolean types", () => {
    expect(() => parseCitation({ ...source, char_end: 0 })).toThrow("char_end");
    expect(() => parseDocument({ ...document, status: "unknown" })).toThrow("status");
    expect(() => parseUploadResult({ document, content_changed: "yes" })).toThrow("content_changed");
    expect(() => parseDiagnostics({ report: "摘要" })).toThrow("request_id");
    expect(() => parseAnswer({ ...answer, citations: [{ ...source, images: undefined }] })).toThrow("images");
  });
  it("checks counts, ranks and score ranges without coercion", () => {
    const evidence = { band: "sufficient", candidate_count: 1, max_vector_similarity: 0.9, refusal_threshold: 0.45, answer_threshold: 0.55 };
    const candidate = { ...source, vector_similarity: null, rrf_score: 0.03, vector_rank: null, keyword_rank: 1 };
    expect(parseEvidenceCandidate(candidate)).toEqual(candidate);
    for (const value of [-1, 1.5, "1", Number.MAX_SAFE_INTEGER + 1]) expect(() => parseRetrievalEvidence({ ...evidence, candidate_count: value })).toThrow("candidate_count");
    for (const value of [0, -1, 1.5, "1"]) expect(() => parseEvidenceCandidate({ ...candidate, vector_rank: value })).toThrow("vector_rank");
    expect(() => parseEvidenceCandidate({ ...candidate, vector_similarity: 1.1 })).toThrow("vector_similarity");
    expect(() => parseEvidenceCandidate({ ...candidate, rrf_score: -0.1 })).toThrow("rrf_score");
    expect(() => parseRetrievalEvidence({ ...evidence, refusal_threshold: 0.6 })).toThrow("threshold");
  });
  it("rejects duplicate graph nodes and unknown or string endpoints", () => {
    expect(() => parseGraph({ ...graph, nodes: [graph.nodes[0], graph.nodes[0]] })).toThrow("nodes");
    for (const source of ["1", 0, 3]) expect(() => parseGraph({ ...graph, edges: [{ ...graph.edges[0], source }] })).toThrow("source");
    expect(() => parseGraph({ ...graph, edges: [{ ...graph.edges[0], weight: 1.01 }] })).toThrow("weight");
  });
  it("rejects workflow citation conflicts and illegal dependencies", () => {
    expect(() => parseWorkflow({ ...workflow, steps: [{ ...workflow.steps[0], citations: [{ ...source, doc_id: 4 }] }] })).toThrow("citations");
    expect(() => parseWorkflow({ ...workflow, steps: [{ ...workflow.steps[0], depends_on: [1] }] })).toThrow("depends_on");
  });
  it("rejects conflicting snapshots sharing one chunk ID", () => {
    expect(() => parseCitations([source, { ...source, index: 2, doc_id: 4 }], "citations")).toThrow("citations");
  });
  it("accepts intersecting citation images but bounds full-document images", () => {
    const image = { ordinal: 1, source_reference: "a.png", alt_text: "图", char_start: 0, char_end: 20,
      content_hash: "a".repeat(64), mime_type: "image/png", file_size: 100, width: 10, height: 10,
      content_url: "/api/v1/documents/3/versions/1/images/1" };
    const citation = { ...source, char_start: 10, char_end: 30, images: [image] };
    expect(parseCitation(citation).images).toEqual([image]);
    expect(parseDocumentContent({ title: "图", content: "中".repeat(20), images: [image] }).images).toEqual([image]);
    expect(() => parseDocumentContent({ title: "图", content: "中".repeat(19), images: [image] })).toThrow("images");
    expect(() => parseCitation({ ...citation, images: [{ ...image, char_end: 10 }] })).toThrow("images");
    expect(() => parseCitation({ ...citation, images: [image, { ...image, ordinal: 2 }] })).toThrow("images");
    expect(() => parseCitation({ ...citation, images: [{ ...image, content_url: "/api/v1/../private" }] })).toThrow("content_url");
  });
});
