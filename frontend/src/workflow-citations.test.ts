import { describe, expect, it } from "vitest";

import { normalizeWorkflowCitations } from "./workflow-citations";
import type { Citation, WorkflowResponse } from "./types";

function citation(index: number, chunkId: number): Citation {
  return {
    index,
    chunk_id: chunkId,
    doc_id: 7,
    doc_title: "函数、数列与极限.md",
    chunk_text: `原文 ${chunkId}`,
    char_start: chunkId,
    char_end: chunkId + 10,
    images: [],
  };
}

describe("workflow citation compatibility", () => {
  it("converts saved local step numbers to stable global numbers", () => {
    const old: WorkflowResponse = {
      task: "比较数列极限和函数极限",
      steps: [
        {
          index: 1, goal: "数列极限", query: "q1", status: "answered",
          conclusion: "数列极限。[1]", note: null,
          citations: [citation(1, 10)], possible_sources: [],
        },
        {
          index: 2, goal: "函数极限", query: "q2", status: "answered",
          conclusion: "函数极限。[1] 与数列极限。[2]", note: null,
          citations: [citation(1, 20), citation(2, 10)], possible_sources: [],
        },
      ],
      answer: "数列极限。[1] 函数极限。[2] 与数列极限。[3]",
      citations: [citation(1, 10), citation(2, 20), citation(3, 10)],
    };

    const normalized = normalizeWorkflowCitations(old);

    expect(normalized.citations.map((source) => [source.index, source.chunk_id]))
      .toEqual([[1, 10], [2, 20]]);
    expect(normalized.steps[1]?.conclusion).toBe("函数极限。[2] 与数列极限。[1]");
    expect(normalized.steps[1]?.citations.map((source) => source.index)).toEqual([2, 1]);
    expect(normalized.answer).toBe("数列极限。[1] 函数极限。[2] 与数列极限。[1]");
    expect(normalizeWorkflowCitations(normalized)).toEqual(normalized);
  });
});
