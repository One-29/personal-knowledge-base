import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationStore } from "./conversations";
import { initTasks, submitQuestion, submitWorkflow } from "./tasks";
import { state } from "./state";

const ui = vi.hoisted(() => {
  const node = () => ({ value: "", innerHTML: "", textContent: "", className: "", isConnected: true,
    classList: { add: vi.fn(), remove: vi.fn() }, append: vi.fn(), replaceChildren: vi.fn(),
    addEventListener: vi.fn(), querySelector: vi.fn(() => null), scrollIntoView: vi.fn() });
  const nodes = new Map<string, ReturnType<typeof node>>();
  return { node, nodes, get: (id: string) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id)!; },
    notify: vi.fn(), mount: vi.fn(() => vi.fn()), busy: vi.fn() };
});
vi.mock("./dom", async importOriginal => ({ ...await importOriginal<typeof import("./dom")>(), byId: ui.get, queryAll: () => [] }));
vi.mock("./notifications", () => ({ notify: ui.notify }));
vi.mock("./evidence", () => ({ bindCitations: vi.fn() }));
vi.mock("./task-runtime", () => ({ focusLatest: vi.fn(), initComposer: vi.fn(), resizeComposer: vi.fn(), mountRunState: ui.mount, setTaskBusy: ui.busy }));

let store: ConversationStore;
beforeEach(() => {
  vi.clearAllMocks();
  ui.nodes.clear();
  state.controllers.ask = null;
  state.controllers.trace = null;
  state.askKbId = null;
  vi.stubGlobal("document", { createElement: ui.node });
  vi.stubGlobal("window", { requestAnimationFrame: () => 1, cancelAnimationFrame: vi.fn(), setTimeout: () => 1, clearTimeout: vi.fn() });
  store = new ConversationStore(null, () => "test");
  initTasks(store);
});
afterEach(() => vi.unstubAllGlobals());

describe("task submission boundaries", () => {
  it.each(["中".repeat(501), "😀".repeat(501), " \t\u3000", "a\0b"])("preserves invalid input without a request or a task", async raw => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const ensure = vi.spyOn(store, "ensure");
    for (const [id, submit] of [["ask-input", submitQuestion], ["trace-input", submitWorkflow]] as const) {
      ui.get(id).value = raw;
      await submit();
      expect(ui.get(id).value).toBe(raw);
    }
    expect(ensure).not.toHaveBeenCalled();
    expect(ui.mount).not.toHaveBeenCalled();
    expect(ui.busy).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.notify).toHaveBeenCalledWith(expect.any(String), true);
  });
  it("sends 500 emoji through real SSE parsing and saves only the final result", async () => {
    const question = "😀".repeat(500);
    ui.get("ask-input").value = question;
    const result = { question, content: "完成", status: "answered", session_id: null, search_query: question,
      refused: false, refusal_reason: null, evidence: null, citations: [], possible_sources: [] };
    const body = `event: metadata\ndata: ${JSON.stringify({ question, search_query: question })}\n\nevent: result\ndata: ${JSON.stringify(result)}\n\n`;
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetchMock);
    await submitQuestion();
    expect(ui.get("ask-input").value).toBe("");
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1].body).question).toBe(question);
    expect(store.active()?.turns[0]?.answerText).toBe("完成");
    expect(state.controllers.ask).toBeNull();
  });
  it("validates a normal workflow response before storing it", async () => {
    ui.get("trace-input").value = "比较极限";
    ui.get("trace-kb").value = "12";
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ task: "比较极限", answer: "结果", steps: [], citations: [] })));
    vi.stubGlobal("fetch", fetchMock);
    await submitWorkflow();
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1].body).kb_id).toBe(12);
    expect(store.active()?.turns[0]?.kind).toBe("workflow");
    expect(ui.get("trace-input").value).toBe("");
  });
  it("restores input and does not save malformed workflow responses", async () => {
    ui.get("trace-input").value = "比较极限";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ answer: "缺少字段" }))));
    await submitWorkflow();
    expect(ui.get("trace-input").value).toBe("比较极限");
    expect(store.active()?.turns).toEqual([]);
    expect(ui.get("trace-flow").append).toHaveBeenCalledOnce();
  });
});
