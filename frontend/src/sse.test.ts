import { describe, expect, it } from "vitest";

import { SseDecoder } from "./sse";

describe("SseDecoder", () => {
  it("handles fragmented CRLF frames, comments, and multiline data", () => {
    const decoder = new SseDecoder();
    expect(decoder.push("event: meta\r")).toEqual([]);
    expect(decoder.push("\ndata: {\"first\":")).toEqual([]);
    expect(decoder.push("true}\r\n\r\n: ping\n\nevent: delta\ndata: line one\ndata: line two\n\n"))
      .toEqual([
        { event: "meta", data: '{"first":true}' },
        { event: "delta", data: "line one\nline two" },
      ]);
    expect(decoder.finish()).toEqual([]);
  });

  it("flushes a final unterminated frame without inventing empty events", () => {
    const decoder = new SseDecoder();
    expect(decoder.push("event: result\ndata: {}")).toEqual([]);
    expect(decoder.finish()).toEqual([{ event: "result", data: "{}" }]);
    expect(decoder.finish()).toEqual([]);
  });
});
