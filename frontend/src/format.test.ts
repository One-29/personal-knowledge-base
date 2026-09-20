import { describe, expect, it } from "vitest";

import { escapeHtml, renderRichText, sourceSummary } from "./format";
import type { Citation } from "./types";

function citation(index: number, chunkId: number, title: string): Citation {
  return {
    index,
    chunk_id: chunkId,
    doc_id: chunkId,
    doc_title: title,
    chunk_text: "原文",
    char_start: 0,
    char_end: 2,
    images: [],
  };
}

describe("rich text rendering", () => {
  it("escapes model HTML before adding controlled markup", () => {
    const output = renderRichText("# 标题\n\n<img src=x onerror=alert(1)> [2]");

    expect(output).toContain("<h2>标题</h2>");
    expect(output).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(output).not.toContain("<img src=x");
    expect(output).toContain('class="cite" data-index="2"');
  });

  it("escapes attribute-sensitive characters", () => {
    expect(escapeHtml(`&<>'"`)).toBe("&amp;&lt;&gt;&#39;&quot;");
  });

  it("deduplicates source pills by chunk while keeping first-seen order", () => {
    const output = sourceSummary([
      citation(1, 20, "第二章.md"),
      citation(2, 10, "第一章.md"),
      citation(3, 20, "重复项.md"),
    ]);

    expect(output.match(/class="source-pill"/g)).toHaveLength(2);
    expect(output.indexOf("第二章.md")).toBeLessThan(output.indexOf("第一章.md"));
    expect(output).not.toContain("重复项.md");
  });
});
