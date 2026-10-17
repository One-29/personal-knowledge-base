import { id } from "./base";

export const QUESTION_MAX_CODE_POINTS = 500;

/** Match Python str.strip(), not JavaScript trim() or HTML UTF-16 maxlength. */
export function parseQuestion(value: string): string {
  const question = value.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, "");
  if (!question) throw new Error("问题不能为空，请输入内容");
  if (question.includes("\0")) throw new Error("问题不能包含 NUL 字符");
  if (Array.from(question).length > QUESTION_MAX_CODE_POINTS) throw new Error("问题最多 500 个字符（按 Unicode 码点计数），请缩短后再发送");
  return question;
}

/** Only DOM controls use string IDs. JSON response parsers never call this. */
export function parseDomId(value: string | undefined): number {
  if (value === undefined || !/^[1-9]\d*$/.test(value)) throw new Error("请选择有效的编号");
  return id(Number(value), "选择的编号");
}
