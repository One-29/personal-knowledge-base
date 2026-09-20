export function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`缺少前端挂载点 #${id}`);
  }
  return element as T;
}

export function queryOne<T extends Element = Element>(
  selector: string,
  root: ParentNode = document,
): T {
  const element = root.querySelector<T>(selector);
  if (element === null) {
    throw new Error(`缺少前端元素 ${selector}`);
  }
  return element;
}

export function queryAll<T extends Element = Element>(
  selector: string,
  root: ParentNode = document,
): T[] {
  return Array.from(root.querySelectorAll<T>(selector));
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

export function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
