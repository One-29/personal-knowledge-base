import type {
  AnswerResponse,
  Citation,
  Conversation,
  ConversationTurn,
  DocumentImage,
  HistoryTurn,
  WorkflowResponse,
  WorkflowStep,
  WorkflowStepStatus,
} from "./types";

const CONVERSATIONS_KEY = "kb_conversations";
const ACTIVE_CONVERSATION_KEY = "kb_active_conv";
const MAX_CONVERSATIONS = 50;
const HISTORY_TURNS = 6;

type IdFactory = () => string;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function parseImage(value: unknown): DocumentImage | null {
  if (!isRecord(value)) return null;
  const contentUrl = nullableString(value.content_url);
  if (contentUrl === null || !contentUrl.startsWith("/api/v1/")) return null;
  const mime = nullableString(value.mime_type);
  if (mime !== "image/png" && mime !== "image/jpeg" && mime !== "image/webp") return null;
  return {
    ordinal: finiteNumber(value.ordinal),
    source_reference: nullableString(value.source_reference) ?? "",
    alt_text: nullableString(value.alt_text) ?? "",
    char_start: finiteNumber(value.char_start),
    char_end: finiteNumber(value.char_end),
    content_hash: nullableString(value.content_hash) ?? "",
    mime_type: mime,
    file_size: finiteNumber(value.file_size),
    width: finiteNumber(value.width),
    height: finiteNumber(value.height),
    content_url: contentUrl,
  };
}

function parseCitation(value: unknown): Citation | null {
  if (!isRecord(value)) return null;
  const title = nullableString(value.doc_title);
  const chunkText = nullableString(value.chunk_text);
  if (title === null || chunkText === null) return null;
  return {
    index: finiteNumber(value.index),
    chunk_id: finiteNumber(value.chunk_id),
    doc_id: finiteNumber(value.doc_id),
    doc_title: title,
    chunk_text: chunkText,
    char_start: finiteNumber(value.char_start),
    char_end: finiteNumber(value.char_end),
    images: Array.isArray(value.images)
      ? value.images.map(parseImage).filter((image): image is DocumentImage => image !== null)
      : [],
  };
}

function parseCitations(value: unknown): Citation[] {
  return Array.isArray(value)
    ? value.map(parseCitation).filter((citation): citation is Citation => citation !== null)
    : [];
}

function parseAnswer(value: unknown, question: string): AnswerResponse | null {
  if (!isRecord(value) || typeof value.content !== "string") return null;
  return {
    question: nullableString(value.question) ?? question,
    content: value.content,
    session_id: nullableString(value.session_id),
    search_query: nullableString(value.search_query),
    citations: parseCitations(value.citations),
    refused: value.refused === true,
    refusal_reason: nullableString(value.refusal_reason),
  };
}

function parseStepStatus(value: unknown): WorkflowStepStatus {
  return value === "answered" || value === "insufficient" || value === "error"
    ? value
    : "error";
}

function parseWorkflowStep(value: unknown, index: number): WorkflowStep | null {
  if (!isRecord(value)) return null;
  const goal = nullableString(value.goal);
  const query = nullableString(value.query);
  if (goal === null || query === null) return null;
  return {
    index: finiteNumber(value.index, index + 1),
    goal,
    query,
    status: parseStepStatus(value.status),
    conclusion: nullableString(value.conclusion),
    note: nullableString(value.note),
    citations: parseCitations(value.citations),
  };
}

function parseWorkflow(value: unknown, task: string): WorkflowResponse | null {
  if (!isRecord(value) || typeof value.answer !== "string") return null;
  return {
    task: nullableString(value.task) ?? task,
    steps: Array.isArray(value.steps)
      ? value.steps
        .map(parseWorkflowStep)
        .filter((step): step is WorkflowStep => step !== null)
      : [],
    answer: value.answer,
    citations: parseCitations(value.citations),
  };
}

function parseTurn(value: unknown): ConversationTurn | null {
  if (!isRecord(value) || typeof value.question !== "string") return null;
  const question = value.question;
  const answerText = nullableString(value.answerText) ?? "";
  const durationMs = typeof value.durationMs === "number" && Number.isFinite(value.durationMs)
    ? value.durationMs
    : null;
  const at = finiteNumber(value.at, Date.now());
  if (value.kind === "workflow") {
    const result = parseWorkflow(value.result, question);
    return result === null
      ? null
      : { kind: "workflow", question, result, answerText, durationMs, at };
  }
  const answer = parseAnswer(value.answer, question);
  return answer === null
    ? null
    : { kind: "ask", question, answer, answerText, durationMs, at };
}

function parseConversation(value: unknown): Conversation | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  return {
    id: value.id,
    title: nullableString(value.title) ?? "",
    at: finiteNumber(value.at, Date.now()),
    turns: Array.isArray(value.turns)
      ? value.turns.map(parseTurn).filter((turn): turn is ConversationTurn => turn !== null)
      : [],
  };
}

function defaultId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `c-${crypto.randomUUID()}`;
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export class ConversationStore {
  constructor(
    private readonly storage: Storage,
    private readonly idFactory: IdFactory = defaultId,
  ) {}

  load(): Conversation[] {
    try {
      const parsed: unknown = JSON.parse(this.storage.getItem(CONVERSATIONS_KEY) ?? "[]");
      return Array.isArray(parsed)
        ? parsed
          .map(parseConversation)
          .filter((conversation): conversation is Conversation => conversation !== null)
        : [];
    } catch {
      return [];
    }
  }

  persist(conversations: Conversation[]): void {
    this.storage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify(conversations.slice(0, MAX_CONVERSATIONS)),
    );
  }

  active(): Conversation | null {
    const activeId = this.storage.getItem(ACTIVE_CONVERSATION_KEY);
    return this.load().find((conversation) => conversation.id === activeId) ?? null;
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
    this.storage.setItem(ACTIVE_CONVERSATION_KEY, conversation.id);
    return conversation;
  }

  ensure(): Conversation {
    return this.active() ?? this.start();
  }

  use(id: string): boolean {
    if (!this.load().some((conversation) => conversation.id === id)) return false;
    this.storage.setItem(ACTIVE_CONVERSATION_KEY, id);
    return true;
  }

  drop(id: string): void {
    const conversations = this.load().filter((conversation) => conversation.id !== id);
    this.persist(conversations);
    if (this.storage.getItem(ACTIVE_CONVERSATION_KEY) !== id) return;
    const first = conversations[0];
    if (first === undefined) this.storage.removeItem(ACTIVE_CONVERSATION_KEY);
    else this.storage.setItem(ACTIVE_CONVERSATION_KEY, first.id);
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
    return conversation.turns.slice(-HISTORY_TURNS).map((turn) => ({
      question: turn.question,
      answer: turn.answerText.slice(0, 1500),
    }));
  }
}
