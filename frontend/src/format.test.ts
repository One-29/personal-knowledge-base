import { describe, expect, it } from "vitest";

import { escapeHtml, renderRichText, sourceSummary, withCitations } from "./format";
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

  it("groups different chunks from one document under a single title", () => {
    const output = sourceSummary([
      citation(1, 10, "极限.md"),
      { ...citation(2, 20, "极限.md"), doc_id: 10 },
    ]);

    expect(output.match(/<strong>极限\.md<\/strong>/g)).toHaveLength(1);
    expect(output.match(/class="source-document"/g)).toHaveLength(1);
    expect(output.match(/class="source-pill"/g)).toHaveLength(2);
  });

  it("keeps punctuation before citations and joins soft paragraph lines", () => {
    const output = withCitations("数列极限[1]\n。函数极限[2]。", [
      citation(1, 10, "极限.md"),
      citation(2, 20, "极限.md"),
    ]);

    expect(output).toContain('数列极限。&#8288;<span class="cite" data-index="1" data-chunk="10"');
    expect(output).toContain('函数极限。&#8288;<span class="cite" data-index="2" data-chunk="20"');
    expect(output).not.toContain("<br>");
    expect(output).not.toContain("<p>。");
  });

  it("leaves unknown markers unlinked and preserves inline code", () => {
    const output = withCitations("`a[1]。` 正文。[1] 未知[9]。", [
      citation(1, 10, "极限.md"),
    ]);

    expect(output).toContain("<code>a[1]。</code>");
    expect(output).toContain('data-chunk="10"');
    expect(output).toContain("未知。[9]");
    expect(output).not.toContain('data-index="9"');
  });

  it("collapses adjacent copies of the same source marker", () => {
    const output = withCitations("同一依据。[1][1] [1]", [citation(1, 10, "极限.md")]);

    expect(output.match(/class="cite"/g)).toHaveLength(1);
  });
});
