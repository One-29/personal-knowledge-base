/** Shared rules for untrusted JSON; parsers never coerce response values. */
export type Parser<T> = (value: unknown, path?: string) => T;

export class ValidationError extends Error {
  constructor(path: string, expected: string) {
    super(`数据格式错误：${path} ${expected}`);
    this.name = "ValidationError";
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function record(value: unknown, path = "响应"): Record<string, unknown> {
  if (!isRecord(value)) throw new ValidationError(path, "必须是对象");
  return value;
}

export function string(value: unknown, path = "字符串"): string {
  if (typeof value !== "string") throw new ValidationError(path, "必须是字符串");
  return value;
}

export function boolean(value: unknown, path = "布尔值"): boolean {
  if (typeof value !== "boolean") throw new ValidationError(path, "必须是布尔值");
  return value;
}

export function number(value: unknown, path: string, min: number, max = Number.MAX_VALUE): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) {
    throw new ValidationError(path, `必须是 ${min} 至 ${max} 范围内的有限数字`);
  }
  return value;
}

export function count(value: unknown, path = "数量"): number {
  const result = number(value, path, 0, Number.MAX_SAFE_INTEGER);
  if (!Number.isSafeInteger(result)) throw new ValidationError(path, "必须是非负安全整数");
  return result;
}

export function id(value: unknown, path = "ID"): number {
  const result = count(value, path);
  if (result === 0) throw new ValidationError(path, "必须是正的安全整数");
  return result;
}

export function enumeration<const T extends string>(value: unknown, choices: readonly T[], path: string): T {
  for (const choice of choices) if (value === choice) return choice;
  throw new ValidationError(path, `必须是 ${choices.join(" / ")} 之一`);
}

export function nullable<T>(value: unknown, parser: Parser<T>, path: string): T | null {
  return value === null ? null : parser(value, path);
}

export function array<T>(value: unknown, parser: Parser<T>, path: string): T[] {
  if (!Array.isArray(value)) throw new ValidationError(path, "必须是数组");
  return value.map((item: unknown, index: number) => parser(item, `${path}[${index}]`));
}

export function unique<T>(values: T[], key: (value: T) => number, path: string): T[] {
  const seen = new Set<number>();
  for (const value of values) {
    if (seen.has(key(value))) throw new ValidationError(path, "包含重复编号");
    seen.add(key(value));
  }
  return values;
}

export function interval(value: Record<string, unknown>, path: string): { char_start: number; char_end: number } {
  const char_start = count(value.char_start, `${path}.char_start`);
  const char_end = count(value.char_end, `${path}.char_end`);
  if (char_end <= char_start) throw new ValidationError(`${path}.char_end`, "必须大于 char_start");
  return { char_start, char_end };
}

export function timestamp(value: unknown, path = "时间"): string {
  const result = string(value, path);
  if (!/^\d{4}-\d{2}-\d{2}T/.test(result) || !Number.isFinite(Date.parse(result))) {
    throw new ValidationError(path, "必须是有效的 ISO 日期时间");
  }
  return result;
}
