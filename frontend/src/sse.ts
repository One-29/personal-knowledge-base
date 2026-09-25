/** A decoded Server-Sent Event frame. */
export interface SseMessage {
  event: string;
  data: string;
}

/**
 * Incremental SSE parser. Network chunks may split CRLF pairs, UTF-8 text, or
 * fields at arbitrary positions; UTF-8 decoding happens before this parser.
 */
export class SseDecoder {
  private buffer = "";
  private eventName = "message";
  private dataLines: string[] = [];

  push(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const messages: SseMessage[] = [];
    for (;;) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) break;
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      this.consumeLine(line, messages);
    }
    return messages;
  }

  finish(): SseMessage[] {
    const messages: SseMessage[] = [];
    if (this.buffer.length > 0) {
      const line = this.buffer.endsWith("\r") ? this.buffer.slice(0, -1) : this.buffer;
      this.consumeLine(line, messages);
      this.buffer = "";
    }
    this.dispatch(messages);
    return messages;
  }

  private consumeLine(line: string, messages: SseMessage[]): void {
    if (line === "") {
      this.dispatch(messages);
      return;
    }
    if (line.startsWith(":")) return;

    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.eventName = value || "message";
    else if (field === "data") this.dataLines.push(value);
  }

  private dispatch(messages: SseMessage[]): void {
    if (this.dataLines.length > 0) {
      messages.push({
        event: this.eventName,
        data: this.dataLines.join("\n"),
      });
    }
    this.eventName = "message";
    this.dataLines = [];
  }
}
