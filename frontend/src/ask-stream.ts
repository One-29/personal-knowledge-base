import { apiResponse, ApiError } from "./api";
import { SseDecoder, type SseMessage } from "./sse";
import type { AnswerResponse, AskStreamMetadata, HistoryTurn } from "./types";

interface AskStreamRequest {
  question: string;
  kb_id: number | null;
  history: HistoryTurn[];
}

interface AskStreamHandlers {
  onMetadata?: (metadata: AskStreamMetadata) => void;
  onDelta?: (content: string) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableFiniteNumber(value: unknown): value is number | null {
  return value === null || isFiniteNumber(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isDocumentImage(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return isFiniteNumber(value.ordinal)
    && typeof value.source_reference === "string"
    && typeof value.alt_text === "string"
    && isFiniteNumber(value.char_start)
    && isFiniteNumber(value.char_end)
    && typeof value.content_hash === "string"
    && (value.mime_type === "image/png"
      || value.mime_type === "image/jpeg"
      || value.mime_type === "image/webp")
    && isFiniteNumber(value.file_size)
    && isFiniteNumber(value.width)
    && isFiniteNumber(value.height)
    && typeof value.content_url === "string"
    && value.content_url.startsWith("/api/v1/");
}

function isCitation(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return isFiniteNumber(value.index)
    && isFiniteNumber(value.chunk_id)
    && isFiniteNumber(value.doc_id)
    && typeof value.doc_title === "string"
    && typeof value.chunk_text === "string"
    && isFiniteNumber(value.char_start)
    && isFiniteNumber(value.char_end)
    && Array.isArray(value.images)
    && value.images.every(isDocumentImage);
}

function isEvidenceCandidate(value: unknown): boolean {
  if (!isRecord(value) || !isCitation(value)) return false;
  return isNullableFiniteNumber(value.vector_similarity)
    && isFiniteNumber(value.rrf_score)
    && isNullableFiniteNumber(value.vector_rank)
    && isNullableFiniteNumber(value.keyword_rank);
}

function isRetrievalEvidence(value: unknown): boolean {
  if (!isRecord(value)) return false;
  if (
    value.band !== "sufficient"
    && value.band !== "borderline"
    && value.band !== "insufficient"
  ) return false;
  if (
    !isFiniteNumber(value.candidate_count)
    || !Number.isInteger(value.candidate_count)
    || value.candidate_count < 0
    || !isNullableFiniteNumber(value.max_vector_similarity)
    || !isFiniteNumber(value.refusal_threshold)
    || !isFiniteNumber(value.answer_threshold)
  ) return false;
  return value.refusal_threshold >= -1
    && value.refusal_threshold < value.answer_threshold
    && value.answer_threshold <= 1;
}

function parseJson(message: SseMessage): unknown {
  try {
    return JSON.parse(message.data) as unknown;
  } catch {
    throw new ApiError(500, "本机服务返回了无法解析的流式数据");
  }
}

function parseMetadata(value: unknown): AskStreamMetadata {
  if (
    !isRecord(value)
    || typeof value.question !== "string"
    || typeof value.search_query !== "string"
  ) {
    throw new ApiError(500, "本机服务返回的问答元数据格式无效");
  }
  return { question: value.question, search_query: value.search_query };
}

function parseDelta(value: unknown): string {
  if (!isRecord(value) || typeof value.content !== "string") {
    throw new ApiError(500, "本机服务返回的回答增量格式无效");
  }
  return value.content;
}

function parseResult(value: unknown): AnswerResponse {
  const statuses = new Set(["answered", "needs_review", "insufficient", "unverified", "error"]);
  if (
    !isRecord(value)
    || typeof value.question !== "string"
    || typeof value.content !== "string"
    || typeof value.status !== "string"
    || !statuses.has(value.status)
    || !isNullableString(value.session_id)
    || !(typeof value.search_query === "string" || value.search_query === null)
    || !Array.isArray(value.citations)
    || !value.citations.every(isCitation)
    || !Array.isArray(value.possible_sources)
    || !value.possible_sources.every(isEvidenceCandidate)
    || !(value.evidence === null || isRetrievalEvidence(value.evidence))
    || typeof value.refused !== "boolean"
    || !(typeof value.refusal_reason === "string" || value.refusal_reason === null)
  ) {
    throw new ApiError(500, "本机服务返回的最终回答格式无效");
  }
  return value as unknown as AnswerResponse;
}

function streamError(value: unknown): ApiError {
  const detail = isRecord(value) && typeof value.detail === "string"
    ? value.detail
    : "流式回答中断，请重试";
  return new ApiError(500, detail);
}

export async function streamAnswer(
  request: AskStreamRequest,
  options: { signal: AbortSignal; handlers?: AskStreamHandlers },
): Promise<AnswerResponse> {
  const response = await apiResponse("/ask/stream", {
    method: "POST",
    signal: options.signal,
    headers: { Accept: "text/event-stream" },
    body: JSON.stringify(request),
  });
  if (!response.headers.get("content-type")?.toLowerCase().includes("text/event-stream")) {
    throw new ApiError(500, "本机服务没有返回流式问答响应");
  }
  if (response.body === null) {
    throw new ApiError(500, "本机服务返回了空的流式问答响应");
  }

  const reader = response.body.getReader();
  const textDecoder = new TextDecoder("utf-8", { fatal: true });
  const eventDecoder = new SseDecoder();
  let result: AnswerResponse | null = null;
  let metadataSeen = false;

  const consume = (messages: SseMessage[]): void => {
    for (const message of messages) {
      const payload = parseJson(message);
      if (message.event === "metadata") {
        if (metadataSeen || result !== null) {
          throw new ApiError(500, "本机服务重复或错序返回了问答元数据");
        }
        metadataSeen = true;
        options.handlers?.onMetadata?.(parseMetadata(payload));
      } else if (message.event === "delta") {
        if (!metadataSeen || result !== null) {
          throw new ApiError(500, "本机服务错序返回了回答增量");
        }
        options.handlers?.onDelta?.(parseDelta(payload));
      } else if (message.event === "result") {
        if (!metadataSeen || result !== null) {
          throw new ApiError(500, "本机服务重复或错序返回了最终回答");
        }
        result = parseResult(payload);
      } else if (message.event === "error") {
        throw streamError(payload);
      } else {
        throw new ApiError(500, `本机服务返回了未知流式事件：${message.event}`);
      }
    }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      consume(eventDecoder.push(textDecoder.decode(value, { stream: true })));
    }
    consume(eventDecoder.push(textDecoder.decode()));
    consume(eventDecoder.finish());
  } catch (error) {
    try {
      await reader.cancel();
    } catch {
      // 连接已经关闭时无需覆盖原始协议或网络错误。
    }
    if (error instanceof Error && error.name === "AbortError") throw error;
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "流式连接意外中断，请确认本机服务仍在运行");
  } finally {
    reader.releaseLock();
  }

  if (!metadataSeen || result === null) {
    throw new ApiError(500, "流式回答在最终校验前意外结束");
  }
  return result;
}
