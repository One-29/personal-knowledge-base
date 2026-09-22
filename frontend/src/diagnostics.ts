import { api } from "./api";
import { byId } from "./dom";
import { friendlyError } from "./format";
import { notify } from "./notifications";
import type { DiagnosticsResponse } from "./types";

export function initDiagnostics(): void {
  const button = byId<HTMLButtonElement>("copy-diagnostics");
  button.addEventListener("click", () => { void copyDiagnostics(button); });
}

async function copyDiagnostics(button: HTMLButtonElement): Promise<void> {
  button.disabled = true;
  try {
    const diagnostics = await api<DiagnosticsResponse>("/diagnostics");
    await copyText(diagnostics.report);
    button.title = diagnostics.log_path
      ? `${diagnostics.privacy_notice} 本地日志：${diagnostics.log_path}`
      : diagnostics.privacy_notice;
    notify("诊断信息已复制，不包含密钥、问题、回答或原文");
  } catch (error) {
    notify(friendlyError(error, "复制诊断信息"), true);
  } finally {
    button.disabled = false;
  }
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // 某些 WebView 禁用 Clipboard API；继续使用受用户点击触发的 DOM 回退。
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("系统剪贴板不可用");
}
