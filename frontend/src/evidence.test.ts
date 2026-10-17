import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Citation } from "./types";

const ui = vi.hoisted(() => {
  const node = () => {
    const listeners = new Map<string, (event?: unknown) => void>();
    return { dataset: {} as Record<string, string>, innerHTML: "", textContent: "", children: [] as unknown[],
      classList: { toggle: vi.fn(), add: vi.fn(), remove: vi.fn() }, scrollIntoView: vi.fn(), setAttribute: vi.fn(),
      append(...items: unknown[]) { this.children.push(...items); },
      addEventListener(name: string, handler: (event?: unknown) => void) { listeners.set(name, handler); },
      click() { listeners.get("click")?.(); } };
  };
  const nodes = new Map<string, ReturnType<typeof node>>();
  return { node, nodes, buttons: [] as ReturnType<typeof node>[], notify: vi.fn(),
    get(id: string) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id)!; } };
});
vi.mock("./dom", () => ({ byId: ui.get, queryOne: ui.node, queryAll: () => ui.buttons }));
vi.mock("./notifications", () => ({ notify: ui.notify }));

const snapshot: Citation = { index: 3, chunk_id: 30, doc_id: 4, doc_title: "已更新的笔记.md",
  chunk_text: "回答时保存的原文", char_start: 10, char_end: 19, images: [] };

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  ui.nodes.clear();
  ui.buttons = [];
  vi.stubGlobal("document", { createElement: ui.node, createTextNode: (text: string) => ({ text }) });
});
afterEach(() => vi.unstubAllGlobals());

async function clickCitation(citations: Citation[] = [snapshot]): Promise<void> {
  const { bindCitations } = await import("./evidence");
  const button = ui.node();
  button.dataset.chunk = "30";
  button.dataset.index = "3";
  ui.buttons = [button];
  bindCitations({} as ParentNode, citations);
  button.click();
}

describe("citation detail and snapshot recovery", () => {
  it("uses the validated saved snapshot after a missing live citation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "引用不存在" }), { status: 404 })));
    await clickCitation();
    await vi.waitFor(() => expect(ui.get("margin-list").children).toHaveLength(1));
    const item = ui.get("margin-list").children[0] as ReturnType<typeof ui.node>;
    expect(item.innerHTML).toContain("回答时引用快照");
    expect(item.innerHTML).toContain(snapshot.doc_title);
    expect(item.children).toEqual([expect.objectContaining({ children: [{ text: snapshot.chunk_text }] })]);
    expect(ui.notify).toHaveBeenCalledWith("当前原文已更新或删除，已显示回答时保存的引用快照");
  });
  it("reports a malformed live response instead of quietly substituting a snapshot", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...snapshot, doc_id: "4" }))));
    await clickCitation();
    await vi.waitFor(() => expect(ui.notify).toHaveBeenCalledWith(expect.stringContaining("doc_id"), true));
    expect(ui.get("margin-list").children).toEqual([]);
  });
  it("does not derive a request ID from an unbound DOM citation", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await clickCitation([]);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.notify).toHaveBeenCalledWith("这条编号不在本次引用列表中", true);
  });
  it("clips a validated intersecting image without moving the following text", async () => {
    const { renderSourceWithImages } = await import("./evidence");
    const host = ui.node();
    renderSourceWithImages(host as unknown as HTMLElement, "余图文之后", [{ ordinal: 1,
      source_reference: "a.png", alt_text: "图", char_start: 0, char_end: 12, content_hash: "a".repeat(64),
      mime_type: "image/png", file_size: 100, width: 10, height: 10,
      content_url: "/api/v1/documents/4/versions/1/images/1" }], 10);
    expect(host.children).toHaveLength(3);
    expect(host.children[0]).toEqual({ text: "" });
    expect(host.children[2]).toEqual({ text: "文之后" });
  });
});
