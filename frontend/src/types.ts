export type ViewName = "ask" | "trace" | "map" | "docs" | "kbs";
export type TaskScope = "ask" | "trace";
export type ActivityOutcome = "idle" | "done" | "error" | "stopped";
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface KnowledgeBase {
  id: number;
  name: string;
  description: string | null;
  doc_count: number;
  created_at: string;
}

export interface KnowledgeDocument {
  id: number;
  kb_id: number;
  title: string;
  status: DocumentStatus;
  char_count: number;
  chunk_count: number;
  image_count: number;
  last_error_code: string | null;
  last_error_message: string | null;
  processed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentImage {
  ordinal: number;
  source_reference: string;
  alt_text: string;
  char_start: number;
  char_end: number;
  content_hash: string;
  mime_type: "image/png" | "image/jpeg" | "image/webp";
  file_size: number;
  width: number;
  height: number;
  content_url: string;
}

export interface Citation {
  index: number;
  chunk_id: number;
  doc_id: number;
  doc_title: string;
  chunk_text: string;
  char_start: number;
  char_end: number;
  images: DocumentImage[];
}

export interface CitationDetail {
  doc_id: number;
  doc_title: string;
  chunk_text: string;
  char_start: number;
  char_end: number;
  images: DocumentImage[];
}

export interface DocumentContent {
  title: string;
  content: string;
  images: DocumentImage[];
}

export interface AnswerResponse {
  question: string;
  content: string;
  session_id: string | null;
  search_query: string | null;
  citations: Citation[];
  refused: boolean;
  refusal_reason: string | null;
}

export type WorkflowStepStatus = "answered" | "insufficient" | "error";

export interface WorkflowStep {
  index: number;
  goal: string;
  query: string;
  status: WorkflowStepStatus;
  conclusion: string | null;
  note: string | null;
  citations: Citation[];
}

export interface WorkflowResponse {
  task: string;
  steps: WorkflowStep[];
  answer: string;
  citations: Citation[];
}

export interface GraphNode {
  doc_id: number;
  title: string;
  chunks: number;
  chars: number;
}

export interface GraphEdge {
  source: number;
  target: number;
  weight: number;
  links: number;
  similarity: number;
}

export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface HistoryTurn {
  question: string;
  answer: string;
}

interface ConversationTurnBase {
  question: string;
  answerText: string;
  durationMs: number | null;
  at: number;
}

export interface AskConversationTurn extends ConversationTurnBase {
  kind: "ask";
  answer: AnswerResponse;
}

export interface WorkflowConversationTurn extends ConversationTurnBase {
  kind: "workflow";
  result: WorkflowResponse;
}

export type ConversationTurn = AskConversationTurn | WorkflowConversationTurn;

export interface Conversation {
  id: string;
  title: string;
  at: number;
  turns: ConversationTurn[];
}

export interface UploadResult {
  document: KnowledgeDocument;
  content_changed: boolean;
}
