import { describe, expect, it } from "vitest";
import { parseConversations } from "./conversations";
import { renderAnswerEntry, renderWorkflowEntry } from "../task-rendering";
import type { AskConversationTurn, WorkflowConversationTurn } from "../types";

const citation = { index: 1, chunk_id: 10, doc_id: 20, doc_title: "笔记.md", chunk_text: "旧原文", char_start: 4, char_end: 7, images: [] };
const answer = { question: "问题", content: "内容。[1]", status: "answered", refused: false,
  session_id: null, search_query: null, refusal_reason: null, evidence: null, citations: [citation], possible_sources: [] };
const step = { index: 1, goal: "定义", query: "定义", status: "answered", conclusion: "定义。[1]",
  note: null, citations: [citation], possible_sources: [] };
const workflow = { task: "出处", answer: "结论。[1]", steps: [step], citations: [citation] };

function recoverAsk(value: unknown): AskConversationTurn {
  const turn = parseConversations([{ id: "ask", turns: [{ question: "问题", answer: value }] }])[0]?.turns[0];
  if (turn?.kind !== "ask") throw new Error("Expected readable ask history");
  return turn;
}

function recoverWorkflow(value: unknown): WorkflowConversationTurn {
  const turn = parseConversations([{ id: "workflow", turns: [{ kind: "workflow", question: "出处", result: value }] }])[0]?.turns[0];
  if (turn?.kind !== "workflow") throw new Error("Expected readable workflow history");
  return turn;
}

describe("history recovery boundaries", () => {
  it.each(["10", 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, null])("quarantines damaged IDs without supplying zero: %s", bad => {
    for (const field of ["chunk_id", "doc_id", "index"]) {
      const saved = recoverAsk({ ...answer, citations: [{ ...citation, [field]: bad }] }).answer;
      expect(saved.content).toBe(answer.content);
      expect(saved.citations).toEqual([]);
      expect(saved.unavailable_sources).toHaveLength(1);
      expect(renderAnswerEntry("问题", saved)).not.toContain('class="cite"');
    }
  });
  it("preserves multiple damaged references, valid gaps and warnings across reload", () => {
    const result = recoverWorkflow({ ...workflow, answer: "坏甲。[1] 坏乙。[2] 好。[3]", steps: [],
      citations: [{ ...citation, chunk_id: "bad" }, { ...citation, index: 2, chunk_id: null }, { ...citation, index: 3 }] }).result;
    expect(result.citations.map(c => [c.index, c.chunk_id])).toEqual([[3, 10]]);
    expect(result.unavailable_sources?.map(c => c.index)).toEqual([1, 2]);
    const reloaded = recoverWorkflow(JSON.parse(JSON.stringify(result))).result;
    expect(reloaded.answer).toBe(result.answer);
    expect(reloaded.citations).toEqual(result.citations);
    expect(reloaded.unavailable_sources?.map(c => c.index)).toEqual([1, 2]);
    const html = renderWorkflowEntry("出处", reloaded);
    expect(html).toContain("部分历史引用无法恢复");
    expect(html).toContain('class="cite" data-index="3" data-chunk="10"');
    expect(html).not.toContain('class="cite" data-index="1"');
    expect(html).not.toContain('class="cite" data-index="2"');
  });
  it("does not trust an ambiguous index even when one duplicate entry is valid", () => {
    const saved = recoverAsk({ ...answer, citations: [citation, { ...citation, chunk_id: "bad" }] }).answer;
    expect(saved.citations).toEqual([]);
    expect(saved.unavailable_sources).toHaveLength(2);
  });
  it("quarantines conflicting snapshots across workflow scopes", () => {
    const result = recoverWorkflow({ ...workflow, steps: [{ ...step, citations: [{ ...citation, doc_id: 99 }] }] }).result;
    expect(result.citations).toEqual([]);
    expect(result.steps[0]?.citations).toEqual([]);
    expect(result.answer).toBe(workflow.answer);
    expect(result.steps[0]?.conclusion).toBe(step.conclusion);
    expect(result.steps[0]?.unavailable_sources).toHaveLength(1);
  });
  it("reserves unbound global numbers and leaves missing local markers inert", () => {
    const result = recoverWorkflow({ ...workflow, answer: "旧未知编号。[1] 已有来源。[3]",
      citations: [{ ...citation, index: 3 }],
      steps: [{ ...step, conclusion: "新来源。[1] 缺失。[2]", citations: [{ ...citation, chunk_id: 11 }] }] }).result;
    expect(result.answer).toBe("旧未知编号。[1] 已有来源。[3]");
    expect(result.citations.map(c => [c.index, c.chunk_id])).toEqual([[3, 10], [2, 11]]);
    expect(result.steps[0]?.conclusion).toBe("新来源。[2] 缺失。[原引用 2 不可用]");
    expect(renderWorkflowEntry("出处", result)).not.toContain('class="cite" data-index="1"');
  });
  it("quarantines conflicting candidates across steps without renumbering good sources", () => {
    const candidate = { ...citation, vector_similarity: .5, rrf_score: .03, vector_rank: 1, keyword_rank: null };
    const result = recoverWorkflow({ ...workflow, citations: [], steps: [
      { ...step, status: "needs_review", citations: [], possible_sources: [candidate] },
      { ...step, index: 2, status: "needs_review", citations: [], possible_sources: [
        { ...candidate, doc_id: 99 }, { ...candidate, index: 3, chunk_id: 11 },
      ] },
    ] }).result;
    expect(result.steps[0]?.possible_sources).toEqual([]);
    expect(result.steps[1]?.possible_sources.map(c => [c.index, c.chunk_id])).toEqual([[3, 11]]);
    expect(result.steps.map(s => s.unavailable_sources?.map(c => c.index))).toEqual([[1], [1]]);
    expect(recoverWorkflow(JSON.parse(JSON.stringify(result))).result).toEqual(result);
  });
  it("quarantines a formal/candidate collision in saved answers", () => {
    const candidate = { ...citation, doc_id: 99, vector_similarity: .5, rrf_score: .03, vector_rank: 1, keyword_rank: null };
    const saved = recoverAsk({ ...answer, possible_sources: [candidate] }).answer;
    expect(saved.citations).toEqual([]);
    expect(saved.possible_sources).toEqual([]);
    expect(saved.unavailable_sources).toHaveLength(2);
    expect(saved.content).toBe(answer.content);
  });
  it("restores evidenced legacy fields but does not repair an invalid enum into answered", () => {
    const { images: _images, ...textOnly } = citation;
    const legacy = recoverAsk({ question: "问题", content: "旧答案。[1]", citations: [textOnly], refused: false,
      session_id: null, search_query: null, refusal_reason: null }).answer;
    expect(legacy.status).toBe("answered");
    expect(legacy.citations).toEqual([citation]);
    expect(legacy.unavailable_sources).toBeUndefined();
    const corrupt = recoverAsk({ ...answer, status: "invented" }).answer;
    expect(corrupt.status).toBe("unverified");
    expect(corrupt.content).toBe(answer.content);
    expect(corrupt.citations).toEqual([citation]);
    expect(corrupt.unavailable_sources).toHaveLength(1);
  });
  it("does not invent a successful status when original required metadata is missing", () => {
    const { refused: _refused, ...missingRefused } = answer;
    const saved = recoverAsk(missingRefused).answer;
    expect(saved.content).toBe(answer.content);
    expect(saved.status).toBe("unverified");
    expect(saved.unavailable_sources).toHaveLength(1);
  });
  it("retains original versioned image snapshots with valid history", () => {
    const image = { ordinal: 1, source_reference: "a.png", alt_text: "图", char_start: 4, char_end: 7,
      content_hash: "a".repeat(64), mime_type: "image/png", file_size: 100, width: 10, height: 10,
      content_url: "/api/v1/documents/20/versions/1/images/1" };
    const withImage = { ...citation, images: [image] };
    const saved = recoverAsk({ ...answer, citations: [withImage] });
    expect(saved.answer.citations).toEqual([withImage]);
    expect(saved.answer.unavailable_sources).toBeUndefined();
    expect(saved.answerText).toBe(answer.content);
  });
});
