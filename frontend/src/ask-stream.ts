import { apiResponse, ApiError } from "./api";
import { SseDecoder, type SseMessage } from "./sse";
import type { AnswerResponse, AskStreamMetadata, HistoryTurn } from "./types";
import { isRecord, ValidationError } from "./validation/base";
import { parseAnswer, parseDelta, parseMetadata } from "./validation/answers";

interface AskStreamRequest {
  question: string;
  kb_id: number | null;
  history: HistoryTurn[];
}

interface AskStreamHandlers {
  onMetadata?: (metadata: AskStreamMetadata) => void;
  onDelta?: (content: string) => void;
}

function parseJson(message: SseMessage): unknown {
  try {
    return JSON.parse(message.data) as unknown;
  } catch {
    throw new ApiError(500, "本机服务返回了无法解析的流式数据");
  }
}

function parseResult(value: unknown): AnswerResponse {
  try {
    return parseAnswer(value);
  } catch (error) {
    if (error instanceof ValidationError) throw new ApiError(500, `本机服务返回的最终回答格式无效：${error.message}`);
    throw error;
  }
}

function streamError(value: unknown): ApiError {
  const detail = isRecord(value) && typeof value.detail === "string"
    ? value.detail : "流式回答中断，请重试";
  return new ApiError(500, detail);
}

export async function streamAnswer(
  request: AskStreamRequest,
  options: { signal: AbortSignal; handlers?: AskStreamHandlers },
): Promise<AnswerResponse> {
  const response = await apiResponse("/ask/stream", {
    method: "POST", signal: options.signal,
    headers: { Accept: "text/event-stream" }, body: JSON.stringify(request),
  });
  if (!response.headers.get("content-type")?.toLowerCase().includes("text/event-stream")) {
    throw new ApiError(500, "本机服务没有返回流式问答响应");
  }
  if (response.body === null) throw new ApiError(500, "本机服务返回了空的流式问答响应");
  const reader = response.body.getReader();
  const textDecoder = new TextDecoder("utf-8", { fatal: true });
  const eventDecoder = new SseDecoder();
  let result: AnswerResponse | null = null;
  let metadataSeen = false;

  const consume = (messages: SseMessage[]): void => {
    for (const message of messages) {
      const payload = parseJson(message);
      if (message.event === "metadata") {
        if (metadataSeen || result !== null) throw new ApiError(500, "本机服务重复或错序返回了问答元数据");
        const metadata = parseMetadata(payload);
        metadataSeen = true;
        options.handlers?.onMetadata?.(metadata);
      } else if (message.event === "delta") {
        if (!metadataSeen || result !== null) throw new ApiError(500, "本机服务错序返回了回答增量");
        const delta = parseDelta(payload);
        options.handlers?.onDelta?.(delta);
      } else if (message.event === "result") {
        if (!metadataSeen || result !== null) throw new ApiError(500, "本机服务重复或错序返回了最终回答");
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
      // A closed connection must not replace the original protocol or network error.
    }
    if (error instanceof Error && error.name === "AbortError") throw error;
    if (error instanceof ApiError) throw error;
    if (error instanceof ValidationError) throw new ApiError(500, error.message);
    throw new ApiError(0, "流式连接意外中断，请确认本机服务仍在运行");
  } finally {
    reader.releaseLock();
  }
  if (!metadataSeen || result === null) throw new ApiError(500, "流式回答在最终校验前意外结束");
  return result;
}
