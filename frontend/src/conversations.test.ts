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

function answer(content = "answer"): AnswerResponse {
  return {
    question: "question",
    content,
    session_id: null,
    search_query: null,
    citations: [],
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
});
