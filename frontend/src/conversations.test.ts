import { describe, expect, it } from "vitest";

import { ConversationStore } from "./conversations";
import type { AnswerResponse, AskConversationTurn } from "./types";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

class FailingWriteStorage extends MemoryStorage {
  failWrites = false;

  override setItem(key: string, value: string): void {
    if (this.failWrites) throw new DOMException("quota exceeded", "QuotaExceededError");
    super.setItem(key, value);
  }
}

function answer(content = "answer"): AnswerResponse {
  return {
    question: "question",
    content,
    status: "answered",
    session_id: null,
    search_query: null,
    citations: [],
    possible_sources: [],
    evidence: null,
    refused: false,
    refusal_reason: null,
  };
}

function turn(index: number): AskConversationTurn {
  return {
    kind: "ask",
    question: `q${index}`,
    answer: answer(`a${index}`),
    answerText: `a${index}`,
    durationMs: 10,
    at: index,
  };
}

describe("ConversationStore", () => {
  it("treats corrupt or structurally invalid local data as empty", () => {
    const storage = new MemoryStorage();
    storage.setItem("kb_conversations", "not-json");
    const store = new ConversationStore(storage, () => "c-1");
    expect(store.load()).toEqual([]);

    storage.setItem("kb_conversations", JSON.stringify([{ id: 4 }, null]));
    expect(store.load()).toEqual([]);
  });

  it("does not resurrect a conversation deleted while a request is running", () => {
    const storage = new MemoryStorage();
    const store = new ConversationStore(storage, () => "c-1");
    const conversation = store.start();
    store.drop(conversation.id);
    expect(store.remember(conversation.id, turn(1))).toBe(false);
    expect(store.load()).toEqual([]);
  });

  it("returns only the latest six bounded history turns", () => {
    const storage = new MemoryStorage();
    const store = new ConversationStore(storage, () => "c-1");
    const conversation = store.start();
    for (let index = 0; index < 8; index += 1) {
      expect(store.remember(conversation.id, turn(index))).toBe(true);
    }
    const active = store.active();
    expect(active).not.toBeNull();
    expect(store.history(active!)).toEqual([
      { question: "q2", answer: "a2" },
      { question: "q3", answer: "a3" },
      { question: "q4", answer: "a4" },
      { question: "q5", answer: "a5" },
      { question: "q6", answer: "a6" },
      { question: "q7", answer: "a7" },
    ]);
  });

  it("keeps gray-zone results visible locally but out of follow-up context", () => {
    const storage = new MemoryStorage();
    const store = new ConversationStore(storage, () => "c-1");
    const conversation = store.start();
    expect(store.remember(conversation.id, turn(1))).toBe(true);
    const gray = turn(2);
    gray.answer.status = "needs_review";
    gray.answer.refused = true;
    gray.answer.refusal_reason = "borderline_relevance";
    gray.answerText = "可能有关，但没有形成回答";
    expect(store.remember(conversation.id, gray)).toBe(true);

    const active = store.active();
    expect(active?.turns).toHaveLength(2);
    expect(store.history(active!)).toEqual([{ question: "q1", answer: "a1" }]);
  });

  it("keeps a completed answer in memory when persistent storage becomes full", () => {
    const storage = new FailingWriteStorage();
    const store = new ConversationStore(storage, () => "c-1");
    const conversation = store.start();
    storage.failWrites = true;

    expect(store.remember(conversation.id, turn(1))).toBe(true);
    expect(store.isPersistent()).toBe(false);
    expect(store.active()?.turns.map((item) => item.question)).toEqual(["q1"]);
  });

  it("works as an in-memory store when local storage is unavailable", () => {
    const store = new ConversationStore(null, () => "c-1");
    const conversation = store.start();

    expect(store.remember(conversation.id, turn(1))).toBe(true);
    expect(store.isPersistent()).toBe(false);
    expect(store.load()[0]?.turns).toHaveLength(1);
  });

  it("repairs citation numbers in previously saved workflow turns", () => {
    const storage = new MemoryStorage();
    const source = (index: number, chunkId: number) => ({
      index, chunk_id: chunkId, doc_id: 1, doc_title: "极限.md",
      chunk_text: "原文", char_start: 0, char_end: 2, images: [],
    });
    storage.setItem("kb_conversations", JSON.stringify([{
      id: "old", title: "旧工作流", at: 1,
      turns: [{
        kind: "workflow", question: "比较两种极限", answerText: "旧答案", at: 1,
        result: {
          task: "比较两种极限",
          steps: [
            { index: 1, goal: "数列", query: "q1", status: "answered",
              conclusion: "数列。[1]", citations: [source(1, 10)] },
            { index: 2, goal: "函数", query: "q2", status: "answered",
              conclusion: "函数。[1]", citations: [source(1, 20)] },
          ],
          answer: "数列。[1] 函数。[2]",
          citations: [source(1, 10), source(2, 20)],
        },
      }],
    }]));

    const saved = new ConversationStore(storage).load()[0]?.turns[0];

    expect(saved?.kind).toBe("workflow");
    if (saved?.kind !== "workflow") return;
    expect(saved.result.steps[1]?.conclusion).toBe("函数。[2]");
    expect(saved.result.steps[1]?.citations[0]?.chunk_id).toBe(20);
    expect(saved.answerText).toBe(saved.result.answer);
    expect(saved.result.steps[0]?.evidence).toBeNull();
    expect(saved.result.steps[0]?.refusal_reason).toBeNull();
    expect(saved.result.steps[0]?.evidence_origin).toBeNull();
    expect(saved.result.steps[0]?.depends_on).toEqual([]);
  });

  it("preserves workflow evidence and source dependencies across a reload", () => {
    const storage = new MemoryStorage();
    const store = new ConversationStore(storage, () => "sources");
    const conversation = store.start();
    store.remember(conversation.id, {
      kind: "workflow", question: "出处", answerText: "原文不足", at: 1, durationMs: 10,
      result: {
        task: "出处", answer: "原文不足", citations: [],
        steps: [{
          index: 2, goal: "数列出处", query: "出处问题", kind: "source_lookup",
          depends_on: [1], source_scope: "topic", evidence_origin: "previous_step",
          search_query: null, evidence: null, refusal_reason: "source_not_supported",
          status: "insufficient", conclusion: null, note: "原文不足",
          citations: [], possible_sources: [],
        }],
      },
    });
    const saved = new ConversationStore(storage).load()[0]?.turns[0];
    expect(saved?.kind).toBe("workflow");
    if (saved?.kind !== "workflow") return;
    expect(saved.result.steps[0]).toMatchObject({
      kind: "source_lookup", depends_on: [1], source_scope: "topic",
      evidence_origin: "previous_step", search_query: null, evidence: null,
      refusal_reason: "source_not_supported",
    });
  });
});
