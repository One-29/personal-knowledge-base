import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

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

    await expect(api<{ value: number }>("/example", {
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

    await expect(api<null>("/upload", { method: "POST", body: form })).resolves.toBeUndefined();
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).has("Content-Type")).toBe(false);
  });

  it("preserves API details and maps connection failures", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(
      JSON.stringify({ detail: "输入不合法" }),
      { status: 422, statusText: "Unprocessable Entity" },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api("/broken")).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "输入不合法",
    });

    fetchMock.mockRejectedValueOnce(new TypeError("network down"));
    await expect(api("/offline")).rejects.toMatchObject({
      status: 0,
      detail: "无法连接本机服务，请确认启动窗口仍在运行",
    });
  });
});
