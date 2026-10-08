import { afterEach, describe, expect, it, vi } from "vitest";

import { streamAnswer } from "./ask-stream";

const RESULT = {
  question: "极限是什么？",
  content: "极限描述趋近过程 [1]。",
  status: "answered",
  session_id: null,
  search_query: "极限是什么？",
  citations: [],
  possible_sources: [],
  evidence: null,
  refused: false,
  refusal_reason: null,
};

function streamedResponse(text: string, cuts: number[]): Response {
  const encoded = new TextEncoder().encode(text);
  const parts: Uint8Array[] = [];
  let start = 0;
  for (const end of cuts) {
    parts.push(encoded.slice(start, end));
    start = end;
  }
  parts.push(encoded.slice(start));
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamAnswer", () => {
  it("decodes arbitrary byte chunks and publishes metadata and deltas", async () => {
    const body = [
      `event: metadata\ndata: ${JSON.stringify({ question: RESULT.question, search_query: RESULT.search_query })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ content: "极限描述" })}\n\n`,
      `event: delta\ndata: ${JSON.stringify({ content: "趋近过程 [1]。" })}\n\n`,
      `event: result\ndata: ${JSON.stringify(RESULT)}\n\n`,
    ].join("");
    const encoded = new TextEncoder().encode(body);
    const multibyte = encoded.findIndex((byte) => byte >= 0xe0);
    expect(multibyte).toBeGreaterThan(0);
    const fetchMock = vi.fn().mockResolvedValue(streamedResponse(
      body,
      [1, 7, multibyte + 1, multibyte + 2, 119].sort((left, right) => left - right),
    ));
    vi.stubGlobal("fetch", fetchMock);
    const metadata = vi.fn();
    const delta = vi.fn();

    await expect(streamAnswer(
      { question: RESULT.question, kb_id: null, history: [] },
      {
        signal: new AbortController().signal,
        handlers: { onMetadata: metadata, onDelta: delta },
      },
    )).resolves.toEqual(RESULT);
    expect(metadata).toHaveBeenCalledWith({
      question: RESULT.question,
      search_query: RESULT.search_query,
    });
    expect(delta.mock.calls.map((call) => call[0])).toEqual(["极限描述", "趋近过程 [1]。"]);
    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/ask/stream");
    expect(new Headers(request.headers).get("Accept")).toBe("text/event-stream");
  });

  it("rejects explicit server errors and truncated streams", async () => {
    const errorBody = `event: error\ndata: ${JSON.stringify({ detail: "生成中断" })}\n\n`;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(streamedResponse(errorBody, []))
      .mockResolvedValueOnce(streamedResponse(
        [
          `event: metadata\ndata: ${JSON.stringify({ question: "问题", search_query: "问题" })}\n\n`,
          `event: delta\ndata: ${JSON.stringify({ content: "未完成" })}\n\n`,
        ].join(""),
        [],
      ));
    vi.stubGlobal("fetch", fetchMock);
    const request = { question: "问题", kb_id: null, history: [] };
    const signal = new AbortController().signal;

    await expect(streamAnswer(request, { signal })).rejects.toMatchObject({ detail: "生成中断" });
    await expect(streamAnswer(request, { signal })).rejects.toMatchObject({
      detail: "流式回答在最终校验前意外结束",
    });
  });

  it("rejects out-of-order events before exposing draft text", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamedResponse(
      `event: delta\ndata: ${JSON.stringify({ content: "错序草稿" })}\n\n`,
      [],
    ));
    vi.stubGlobal("fetch", fetchMock);
    const delta = vi.fn();

    await expect(streamAnswer(
      { question: "问题", kb_id: null, history: [] },
      {
        signal: new AbortController().signal,
        handlers: { onDelta: delta },
      },
    )).rejects.toMatchObject({ detail: "本机服务错序返回了回答增量" });
    expect(delta).not.toHaveBeenCalled();
  });

  it("accepts a gray-zone result without exposing it as a generated answer", async () => {
    const grayResult = {
      ...RESULT,
      content: "现有资料可能相关，请先核对候选原文。",
      status: "needs_review",
      refused: true,
      refusal_reason: "borderline_relevance",
      citations: [],
      possible_sources: [{
        index: 1,
        chunk_id: 12,
        doc_id: 4,
        doc_title: "函数极限",
        chunk_text: "函数在某点附近无限趋近于一个确定值。",
        char_start: 8,
        char_end: 29,
        images: [],
        vector_similarity: 0.51,
        rrf_score: 0.03,
        vector_rank: 1,
        keyword_rank: null,
      }],
      evidence: {
        band: "borderline",
        candidate_count: 1,
        max_vector_similarity: 0.51,
        refusal_threshold: 0.45,
        answer_threshold: 0.55,
      },
    };
    const body = [
      `event: metadata\ndata: ${JSON.stringify({ question: grayResult.question, search_query: grayResult.search_query })}\n\n`,
      `event: result\ndata: ${JSON.stringify(grayResult)}\n\n`,
    ].join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamedResponse(body, [])));
    const delta = vi.fn();

    await expect(streamAnswer(
      { question: grayResult.question, kb_id: null, history: [] },
      { signal: new AbortController().signal, handlers: { onDelta: delta } },
    )).resolves.toEqual(grayResult);
    expect(delta).not.toHaveBeenCalled();
  });

  it("rejects malformed nested gray-zone evidence", async () => {
    const malformed = {
      ...RESULT,
      status: "needs_review",
      refused: true,
      refusal_reason: "borderline_relevance",
      possible_sources: [{ doc_title: "缺少溯源字段" }],
      evidence: {
        band: "borderline",
        candidate_count: 1,
        max_vector_similarity: 0.51,
        refusal_threshold: 0.55,
        answer_threshold: 0.45,
      },
    };
    const body = [
      `event: metadata\ndata: ${JSON.stringify({ question: malformed.question, search_query: malformed.search_query })}\n\n`,
      `event: result\ndata: ${JSON.stringify(malformed)}\n\n`,
    ].join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamedResponse(body, [])));

    await expect(streamAnswer(
      { question: malformed.question, kb_id: null, history: [] },
      { signal: new AbortController().signal },
    )).rejects.toMatchObject({ detail: "本机服务返回的最终回答格式无效" });
  });
});
