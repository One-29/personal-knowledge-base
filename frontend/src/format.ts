import { ApiError } from "./api";
import { asError } from "./dom";
import type { Citation } from "./types";

export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character] ?? character);
}

export function renderInline(content: unknown, includeCitations = true): string {
  const held: string[] = [];
  const hold = (html: string): string => `\uE000${held.push(html) - 1}\uE001`;
  let source = String(content ?? "");
  source = source.replace(/`([^`\n]+)`/g, (_match, code: string) => (
    hold(`<code>${escapeHtml(code)}</code>`)
  ));
  source = source.replace(/\$([^$\n]+)\$/g, (_match, math: string) => (
    hold(`<span class="math-inline">${escapeHtml(math)}</span>`)
  ));
  let safe = escapeHtml(source)
    .replace(/(?:\*{2})([^*]+)(?:\*{2})/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>");
  if (includeCitations) {
    safe = safe.replace(/\[(\d+)\]/g, (_match, index: string) => (
      `<span class="cite" data-index="${index}" role="button" tabindex="0" aria-label="打开引用 ${index}">[${index}]</span>`
    ));
  }
  return safe.replace(/\uE000(\d+)\uE001/g, (_match, index: string) => (
    held[Number(index)] ?? ""
  ));
}

export function renderRichText(content: unknown, includeCitations = true): string {
  const lines = String(content ?? "").replace(/\r\n?/g, "\n").split("\n");
  const html: string[] = [];
  const special = (line: string): boolean => (
    /^```/.test(line)
    || /^\s*\$\$/.test(line)
    || /^#{1,3}\s+/.test(line)
    || /^\s*[-*]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || /^\s*>\s?/.test(line)
  );

  for (let index = 0; index < lines.length;) {
    const line = lines[index] ?? "";
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (/^```/.test(line)) {
      const language = line.slice(3).trim();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index] ?? "")) {
        code.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) index += 1;
      html.push(`<pre${language ? ` data-language="${escapeHtml(language)}"` : ""}><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*\$\$/.test(line)) {
      const singleLineMath = line.trim();
      if (singleLineMath.length > 4 && singleLineMath.endsWith("$$")) {
        html.push(`<span class="math-block">${escapeHtml(singleLineMath.slice(2, -2).trim())}</span>`);
        index += 1;
        continue;
      }
      const math = [line.replace(/^\s*\$\$/, "")];
      index += 1;
      while (index < lines.length && !/\$\$\s*$/.test(lines[index] ?? "")) {
        math.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) {
        math.push((lines[index] ?? "").replace(/\$\$\s*$/, ""));
        index += 1;
      }
      html.push(`<span class="math-block">${escapeHtml(math.join("\n").trim())}</span>`);
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading?.[1] !== undefined && heading[2] !== undefined) {
      const level = Math.min(4, heading[1].length + 1);
      html.push(`<h${level}>${renderInline(heading[2], includeCitations)}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index] ?? "")) {
        items.push(`<li>${renderInline((lines[index] ?? "").replace(/^\s*[-*]\s+/, ""), includeCitations)}</li>`);
        index += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index] ?? "")) {
        items.push(`<li>${renderInline((lines[index] ?? "").replace(/^\s*\d+\.\s+/, ""), includeCitations)}</li>`);
        index += 1;
      }
      html.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index] ?? "")) {
        quoted.push((lines[index] ?? "").replace(/^\s*>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${quoted.map((part) => renderInline(part, includeCitations)).join("<br>")}</blockquote>`);
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (
      index < lines.length
      && (lines[index] ?? "").trim()
      && !special(lines[index] ?? "")
    ) {
      paragraph.push(lines[index] ?? "");
      index += 1;
    }
    html.push(`<p>${paragraph.map((part) => renderInline(part, includeCitations)).join("<br>")}</p>`);
  }
  return html.join("") || "<p>没有返回可显示的内容。</p>";
}

export function withCitations(content: unknown): string {
  return renderRichText(content, true);
}

export function sourceSummary(citations: Citation[]): string {
  const unique: Citation[] = [];
  const seen = new Set<number>();
  for (const citation of citations) {
    if (seen.has(citation.chunk_id)) continue;
    seen.add(citation.chunk_id);
    unique.push(citation);
  }
  if (unique.length === 0) return "";
  return `<div class="source-summary"><span>引用来源</span>${unique.map((citation) => `
    <button type="button" class="source-pill" data-chunk="${citation.chunk_id}" data-index="${citation.index}">
      <b>[${citation.index}]</b>${escapeHtml(citation.doc_title)}
    </button>`).join("")}</div>`;
}

export function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatCount(value: unknown): string {
  return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
}

export function friendlyError(error: unknown, action = "完成操作"): string {
  if (error instanceof ApiError) {
    if ([0, 400, 404, 409, 413, 422].includes(error.status)) return error.detail;
    return `${action}失败：${error.detail}`;
  }
  return `${action}失败：${asError(error).message || "发生未知错误"}`;
}

export function questionHeader(question: string): string {
  return `<div class="entry-question"><h3 class="entry-q">${escapeHtml(question)}</h3></div>`;
}
