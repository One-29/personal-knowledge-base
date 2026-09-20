import { byId } from "./dom";

export async function checkService(): Promise<void> {
  const indicator = byId("service-dot");
  const label = byId("service-status");
  indicator.className = "service-dot checking";
  label.textContent = "检查中";
  try {
    const response = await fetch("/ready", { cache: "no-store" });
    if (!response.ok) throw new Error("readiness check failed");
    indicator.className = "service-dot";
    label.textContent = "已连接";
  } catch {
    indicator.className = "service-dot offline";
    label.textContent = "未连接";
  }
}

export function markServiceOffline(): void {
  byId("service-dot").className = "service-dot offline";
  byId("service-status").textContent = "未连接";
}

export function initServiceStatus(): void {
  window.addEventListener("online", () => { void checkService(); });
  window.addEventListener("offline", () => { void checkService(); });
}
