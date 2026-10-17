import { afterEach, describe, expect, it, vi } from "vitest";

import { api, apiNoContent } from "./api";
import { count, record } from "./validation/base";
import { parseGraph } from "./validation/graph";

const parseExample = (value: unknown): { value: number } => ({ value: count(record(value).value, "value") });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("adds JSON headers and returns typed JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ value: 42 }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api("/example", parseExample, {
      method: "POST",
      body: JSON.stringify({ input: true }),
    })).resolves.toEqual({ value: 42 });
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/example");
    expect(new Headers(request.headers).get("Content-Type")).toBe("application/json");
  });

  it("lets the browser supply multipart boundaries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const form = new FormData();
    form.append("file", new Blob(["note"]), "note.md");

    await expect(apiNoContent("/upload", { method: "POST", body: form })).resolves.toBeUndefined();
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).has("Content-Type")).toBe(false);
  });

  it("preserves API details and maps connection failures", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(
      JSON.stringify({ detail: "输入不合法" }),
      { status: 422, statusText: "Unprocessable Entity" },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api("/broken", parseExample)).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "输入不合法",
    });

    fetchMock.mockRejectedValueOnce(new TypeError("network down"));
    await expect(api("/offline", parseExample)).rejects.toMatchObject({
      status: 0,
      detail: "无法连接本机服务，请确认启动窗口仍在运行",
    });
  });

  it("does not treat malformed JSON, wrong fields or unexpected empty bodies as typed data", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("not JSON"))
      .mockResolvedValueOnce(new Response(JSON.stringify({ value: "42" })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api("/example", parseExample)).rejects.toMatchObject({ status: 500, detail: expect.stringContaining("有效 JSON") });
    await expect(api("/example", parseExample)).rejects.toMatchObject({ detail: expect.stringContaining("value") });
    await expect(api("/example", parseExample)).rejects.toMatchObject({ detail: expect.stringContaining("无正文") });
    await expect(apiNoContent("/example", { method: "DELETE" })).rejects.toMatchObject({ detail: expect.stringContaining("204/205") });
  });

  it("surfaces a graph format error instead of silently losing edges", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      nodes: [{ doc_id: 1, title: "笔记", chunks: 1, chars: 2 }],
      edges: [{ source: "1", target: 2, weight: 1, links: 1, similarity: 0.8 }], truncated: false,
    }))));
    await expect(api("/graph", parseGraph)).rejects.toMatchObject({ detail: expect.stringContaining("edges[0].source") });
  });
});
