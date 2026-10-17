import { describe, expect, it } from "vitest";
import { parseQuestion, parseDomId } from "./input";

describe("question input", () => {
  it.each(["a", "中", "😀"])("accepts 500 code points and refuses 501: %s", symbol => {
    expect(parseQuestion(` \t${symbol.repeat(500)}\n`)).toBe(symbol.repeat(500));
    expect(() => parseQuestion(symbol.repeat(501))).toThrow("500");
  });
  it("counts code points rather than graphemes or UTF-16 units", () => {
    expect(parseQuestion("e\u0301".repeat(250))).toHaveLength(500);
    expect(() => parseQuestion("e\u0301".repeat(251))).toThrow("500");
    expect(parseQuestion("👩‍💻".repeat(166))).toBe("👩‍💻".repeat(166));
  });
  it.each(["", " \t\r\n", "\u3000\u0085", "\x1c"])("rejects whitespace-only input", value => {
    expect(() => parseQuestion(value)).toThrow("不能为空");
  });
  it("does not strip BOM and rejects NUL consistently with the backend", () => {
    expect(parseQuestion("\ufeff")).toBe("\ufeff");
    expect(() => parseQuestion("a\0b")).toThrow("NUL");
  });
  it("converts only explicit DOM IDs, never response IDs", () => {
    expect(parseDomId("12")).toBe(12);
    for (const value of ["", "0", "-1", "1.1", " 1", "1e2", "9007199254740992"]) expect(() => parseDomId(value)).toThrow();
  });
});
