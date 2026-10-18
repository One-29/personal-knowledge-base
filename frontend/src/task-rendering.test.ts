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
    expect(html).toContain('data-source-scope="citations"');
    expect(html).toContain('data-source-scope="candidates"');
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
    expect(html).toContain('data-source-scope="step-0"');
    expect(html).toContain('data-source-scope="step-0-candidates"');
  });
});

describe("workflow source presentation", () => {
  it("explains source reuse without showing an inherited similarity score", () => {
    const result: WorkflowResponse = {
      task: "列出数列定义出处",
      steps: [{
        index: 2, goal: "数列出处", query: "数列定义的出处", status: "answered",
        kind: "source_lookup", depends_on: [1], evidence_origin: "previous_step",
        search_query: null, conclusion: "定义原文。[1]", note: null,
        citations: [candidate], possible_sources: [],
        evidence: {
          band: "sufficient", candidate_count: 1, max_vector_similarity: 0.999,
          refusal_threshold: 0.45, answer_threshold: 0.55,
        },
      }],
      answer: "定义原文。[1]", citations: [candidate],
    };
    const html = renderWorkflowEntry(result.task, result);
    expect(html).toContain("来源核对问题：数列定义的出处");
    expect(html).toContain("使用第 1 步的原文核对出处 · 未重复检索");
    expect(html).not.toContain("最高向量相似度");
    expect(html).toContain('data-chunk="42"');
  });

  it("distinguishes model evidence insufficiency from retrieval rejection", () => {
    const result: WorkflowResponse = {
      task: "回答原文未覆盖的问题",
      steps: [{
        index: 1, goal: "知识问题", query: "问题", search_query: "实际问题",
        status: "insufficient", refusal_reason: "generation_insufficient",
        conclusion: null, note: "已有原文，但不支持本问题。",
        citations: [], possible_sources: [],
        evidence: {
          band: "sufficient", candidate_count: 2, max_vector_similarity: 0.8,
          refusal_threshold: 0.45, answer_threshold: 0.55,
        },
      }],
      answer: "本步证据不足。", citations: [],
    };
    const html = renderWorkflowEntry(result.task, result);
    expect(html).toContain("证据不足");
    expect(html).toContain("原文不足以支持回答");
    expect(html).toContain("实际检索：实际问题");
    expect(html).toContain("最高向量相似度 0.800");
    expect(html).not.toContain("generation_insufficient");
    expect(html).not.toContain("没有材料");
  });

  it("shows step details on demand and binds inline citations by chunk", () => {
    const result: WorkflowResponse = {
      task: "比较数列极限和函数极限",
      steps: [
        {
          index: 1, goal: "比较定义", query: "数列和函数极限定义", status: "answered",
          conclusion: "两者均描述趋近。[1]", note: null,
          citations: [{ ...candidate, index: 1 }], possible_sources: [],
        },
      ],
      answer: "**1. 比较定义**\n\n两者均描述趋近。[1]",
      citations: [{ ...candidate, index: 1 }],
    };

    const html = renderWorkflowEntry(result.task, result);

    expect(html).toContain('<details class="trace-card">');
    expect(html).toContain('class="cite" data-index="1" data-chunk="42"');
    expect(html).not.toContain("trace-cites");
    expect(html.match(/class="source-document"/g)).toHaveLength(1);
  });
});
