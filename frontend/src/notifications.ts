import { byId } from "./dom";

let hideTimer: number | null = null;

export function notify(message: string, isError = false): void {
  const toast = byId<HTMLDivElement>("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  byId("sr-status").textContent = message;
  if (hideTimer !== null) window.clearTimeout(hideTimer);
  hideTimer = window.setTimeout(
    () => toast.classList.add("hidden"),
    isError ? 5200 : 3200,
  );
}
