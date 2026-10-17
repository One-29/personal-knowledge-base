import type { Conversation, ConversationTurn, HistoryTurn } from "./types";
import { parseConversations } from "./history/conversations";
import { buildHistory } from "./history/context";

const CONVERSATIONS_KEY = "kb_conversations";
const ACTIVE_CONVERSATION_KEY = "kb_active_conv";
const MAX_CONVERSATIONS = 50;

type IdFactory = () => string;

function defaultId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `c-${crypto.randomUUID()}`;
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export class ConversationStore {
  private conversations: Conversation[];
  private activeConversationId: string | null;
  private storageAvailable: boolean;

  constructor(
    private readonly storage: Storage | null,
    private readonly idFactory: IdFactory = defaultId,
  ) {
    this.storageAvailable = storage !== null;
    try {
      const value: unknown = JSON.parse(this.readItem(CONVERSATIONS_KEY) ?? "[]");
      this.conversations = parseConversations(value);
    } catch {
      this.conversations = [];
    }
    this.activeConversationId = this.readItem(ACTIVE_CONVERSATION_KEY);
  }

  private readItem(key: string): string | null {
    if (!this.storageAvailable || this.storage === null) return null;
    try {
      return this.storage.getItem(key);
    } catch {
      this.storageAvailable = false;
      return null;
    }
  }

  private writeItem(key: string, value: string): boolean {
    if (!this.storageAvailable || this.storage === null) return false;
    try {
      this.storage.setItem(key, value);
      return true;
    } catch {
      this.storageAvailable = false;
      return false;
    }
  }

  private removeItem(key: string): boolean {
    if (!this.storageAvailable || this.storage === null) return false;
    try {
      this.storage.removeItem(key);
      return true;
    } catch {
      this.storageAvailable = false;
      return false;
    }
  }

  private setActive(id: string | null): void {
    this.activeConversationId = id;
    if (id === null) this.removeItem(ACTIVE_CONVERSATION_KEY);
    else this.writeItem(ACTIVE_CONVERSATION_KEY, id);
  }

  isPersistent(): boolean {
    return this.storageAvailable;
  }

  load(): Conversation[] {
    return structuredClone(this.conversations);
  }

  persist(conversations: Conversation[]): boolean {
    this.conversations = structuredClone(conversations.slice(0, MAX_CONVERSATIONS));
    return this.writeItem(CONVERSATIONS_KEY, JSON.stringify(this.conversations));
  }

  active(): Conversation | null {
    return this.load().find(
      (conversation) => conversation.id === this.activeConversationId,
    ) ?? null;
  }

  start(): Conversation {
    const conversation: Conversation = {
      id: this.idFactory(),
      title: "",
      at: Date.now(),
      turns: [],
    };
    const conversations = this.load().filter((item) => item.id !== conversation.id);
    conversations.unshift(conversation);
    this.persist(conversations);
    this.setActive(conversation.id);
    return conversation;
  }

  ensure(): Conversation {
    return this.active() ?? this.start();
  }

  use(id: string): boolean {
    if (!this.load().some((conversation) => conversation.id === id)) return false;
    this.setActive(id);
    return true;
  }

  drop(id: string): void {
    const conversations = this.load().filter((conversation) => conversation.id !== id);
    this.persist(conversations);
    if (this.activeConversationId !== id) return;
    const first = conversations[0];
    this.setActive(first?.id ?? null);
  }

  remember(conversationId: string, turn: ConversationTurn): boolean {
    const conversations = this.load();
    const index = conversations.findIndex((conversation) => conversation.id === conversationId);
    const conversation = conversations[index];
    if (index < 0 || conversation === undefined) return false;
    conversation.turns.push(turn);
    conversation.at = Date.now();
    if (!conversation.title) conversation.title = turn.question.slice(0, 24);
    conversations[index] = conversation;
    this.persist(conversations);
    return true;
  }

  history(conversation: Conversation): HistoryTurn[] {
    return buildHistory(conversation);
  }
}
