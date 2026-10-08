/** 浏览器可能因隐私策略、安全设置或配额耗尽而拒绝访问 localStorage。 */

export function browserLocalStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readLocalValue(key: string): string | null {
  const storage = browserLocalStorage();
  if (storage === null) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

export function writeLocalValue(key: string, value: string): boolean {
  const storage = browserLocalStorage();
  if (storage === null) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}
