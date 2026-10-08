import { describe, expect, it } from "vitest";

import { renderAnswerEntry, renderWorkflowEntry } from "./task-rendering";
import type { AnswerResponse, EvidenceCandidate, WorkflowResponse } from "./types";

const candidate: EvidenceCandidate = {
  index: 1,
  chunk_id: 42,
  doc_id: 7,
  doc_title: "极限.md",
  chunk_text: "数列极限使用 ε-N 语言描述。",
  char_start: 10,
  char_end: 30,
  images: [],
  vector_similarity: 0.503,
  rrf_score: 0.03,
  vector_rank: 1,
  keyword_rank: 2,
};

describe("gray-zone rendering", () => {
  it("labels candidates as review material instead of formal citations", () => {
    const answer: AnswerResponse = {
      question: "极限是什么？",
      content: "找到可能相关的内容，但证据强度处于灰区。",
      status: "needs_review",
      session_id: null,
      search_query: "数列极限定义",
      citations: [],
      possible_sources: [candidate],
      evidence: {
        band: "borderline",
        candidate_count: 3,
        max_vector_similarity: 0.503,
        refusal_threshold: 0.45,
        answer_threshold: 0.55,
      },
      refused: true,
      refusal_reason: "borderline_relevance",
    };

    const html = renderAnswerEntry(answer.question, answer, 1200);

    expect(html).toContain("灰区 · 待核对");
    expect(html).toContain("需人工核对，不是正式引用");
    expect(html).toContain("最高向量相似度 0.503");
    expect(html).toContain('data-chunk="42"');
    expect(html).not.toContain("1 条引用");
  });

  it("shows a gray workflow step separately from an answered step", () => {
    const result: WorkflowResponse = {
      task: "综合任务",
      steps: [
        {
          index: 1,
          goal: "核对材料",
          query: "边界问题",
          status: "needs_review",
          conclusion: null,
          note: "请人工核对。",
          citations: [],
          possible_sources: [candidate],
        },
      ],
      answer: "该步需人工核对。",
      citations: [],
    };

    const html = renderWorkflowEntry(result.task, result);

    expect(html).toContain("待核对");
    expect(html).toContain("可能相关的原文");
    expect(html).toContain("0/1 步完成");
  });
});
