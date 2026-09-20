import { api, ApiError } from "./api";
import { byId, queryAll, queryOne } from "./dom";
import { escapeHtml, friendlyError } from "./format";
import { notify } from "./notifications";
import type {
  Citation,
  CitationDetail,
  DocumentContent,
  DocumentImage,
} from "./types";

const EVIDENCE_KEY = "kb_evidence_open";
const MARGIN_WIDTH_KEY = "kb_margin_width";
const marginItems = new Map<string, HTMLElement>();

export function setEvidenceOpen(open: boolean, persist = true): void {
  byId("app-shell").classList.toggle("evidence-collapsed", !open);
  byId<HTMLButtonElement>("margin-open").setAttribute("aria-expanded", String(open));
  if (persist) localStorage.setItem(EVIDENCE_KEY, open ? "1" : "0");
}

function updateEvidenceState(): void {
  const count = marginItems.size;
  byId("margin-count").textContent = String(count);
  byId("margin-badge").textContent = String(count);
  byId("margin-hint").classList.toggle("hidden", count > 0);
  byId<HTMLButtonElement>("margin-clear").disabled = count === 0;
}

function removeMarginItem(key: string, chunkId: number | null = null): void {
  const item = marginItems.get(key);
  if (item === undefined) return;
  item.remove();
  marginItems.delete(key);
  if (chunkId !== null) {
    queryAll<HTMLElement>(
      `.cite.on[data-chunk="${chunkId}"], .source-pill[data-chunk="${chunkId}"]`,
    ).forEach((element) => element.classList.remove("on"));
  }
  updateEvidenceState();
}

function citationSnapshotDetail(citation: Citation | null): CitationDetail | null {
  if (citation === null || typeof citation.chunk_text !== "string") return null;
  const charStart = Number(citation.char_start);
  const charEnd = Number(citation.char_end);
  if (
    !Number.isInteger(charStart)
    || !Number.isInteger(charEnd)
    || charStart < 0
    || charEnd < charStart
  ) {
    return null;
  }
  return {
    doc_id: Number(citation.doc_id),
    doc_title: citation.doc_title || "原文标题未知",
    chunk_text: citation.chunk_text,
    char_start: charStart,
    char_end: charEnd,
    images: Array.isArray(citation.images) ? citation.images : [],
  };
}

export function renderSourceWithImages(
  host: HTMLElement,
  content: string,
  images: DocumentImage[],
  baseOffset = 0,
): void {
  const characters = Array.from(content);
  const ordered = [...images].sort((left, right) => (
    left.char_start - right.char_start || left.ordinal - right.ordinal
  ));
  let cursor = 0;

  for (const image of ordered) {
    const start = Number(image.char_start) - baseOffset;
    const end = Number(image.char_end) - baseOffset;
    const sourceUrl = String(image.content_url || "");
    if (
      !Number.isInteger(start)
      || !Number.isInteger(end)
      || start < cursor
      || end <= start
      || end > characters.length
      || !sourceUrl.startsWith("/api/v1/")
    ) {
      continue;
    }

    host.append(document.createTextNode(characters.slice(cursor, start).join("")));
    const figure = document.createElement("figure");
    figure.className = "source-image";
    figure.dataset.ordinal = String(image.ordinal);
    const link = document.createElement("a");
    link.href = sourceUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "打开无缩放原图";
    const element = document.createElement("img");
    element.src = sourceUrl;
    element.alt = image.alt_text;
    element.loading = "lazy";
    element.decoding = "async";
    if (Number.isInteger(image.width) && image.width > 0) element.width = image.width;
    if (Number.isInteger(image.height) && image.height > 0) element.height = image.height;
    element.addEventListener("error", () => {
      figure.classList.add("broken");
      element.alt = `图片读取失败：${image.source_reference || "原图"}`;
    });
    link.append(element);
    figure.append(link);
    const caption = document.createElement("figcaption");
    const label = image.alt_text || image.source_reference || `图片 ${image.ordinal}`;
    caption.textContent = `${label} · ${image.width || "?"}×${image.height || "?"} · 点击查看原图`;
    figure.append(caption);
    host.append(figure);
    cursor = end;
  }
  host.append(document.createTextNode(characters.slice(cursor).join("")));
}

async function pinToMargin(
  chunkId: number,
  displayIndex: string | number | null = null,
  citation: Citation | null = null,
): Promise<void> {
  const key = `chunk-${chunkId}`;
  try {
    setEvidenceOpen(true);
    const existing = marginItems.get(key);
    if (existing !== undefined) {
      existing.scrollIntoView({ behavior: "smooth", block: "nearest" });
      existing.classList.remove("margin-item-enter");
      void existing.offsetWidth;
      existing.classList.add("margin-item-enter");
      markActiveCitation(chunkId);
      return;
    }

    let detail: CitationDetail;
    let isHistoricalSnapshot = false;
    try {
      detail = await api<CitationDetail>(`/citations/${chunkId}`);
    } catch (error) {
      const snapshot = error instanceof ApiError && error.status === 404
        ? citationSnapshotDetail(citation)
        : null;
      if (snapshot === null) throw error;
      detail = snapshot;
      isHistoricalSnapshot = true;
    }
    const rangeLabel = isHistoricalSnapshot
      ? `回答时引用快照 · 原文当前已更新或删除 · 原字符位置 ${detail.char_start}–${detail.char_end}`
      : `字符位置 ${detail.char_start}–${detail.char_end}`;
    const item = document.createElement("article");
    item.className = "margin-item margin-item-enter";
    item.dataset.chunk = String(chunkId);
    item.innerHTML = `
      <div class="item-head">
        <div class="item-source">
          ${displayIndex === null ? "" : `<span class="item-index">[${escapeHtml(displayIndex)}]</span>`}
          <p class="item-src">${escapeHtml(detail.doc_title)}</p>
          <span class="item-range">${escapeHtml(rangeLabel)}</span>
        </div>
        <button type="button" class="text-action" data-unpin>移除</button>
      </div>`;
    const quote = document.createElement("blockquote");
    quote.className = "positioned-source";
    renderSourceWithImages(quote, detail.chunk_text, detail.images ?? [], detail.char_start);
    item.append(quote);
    queryOne<HTMLButtonElement>("[data-unpin]", item).addEventListener(
      "click",
      () => removeMarginItem(key, chunkId),
    );
    byId("margin-list").append(item);
    marginItems.set(key, item);
    updateEvidenceState();
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    markActiveCitation(chunkId);
    if (isHistoricalSnapshot) {
      notify("当前原文已更新或删除，已显示回答时保存的引用快照");
    }
  } catch (error) {
    notify(friendlyError(error, "读取引用"), true);
  }
}

export async function openDocument(docId: number, title = ""): Promise<void> {
  const key = `document-${docId}`;
  try {
    setEvidenceOpen(true);
    const existing = marginItems.get(key);
    if (existing !== undefined) {
      existing.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    const detail = await api<DocumentContent>(`/documents/${docId}/content`);
    const item = document.createElement("article");
    item.className = "margin-item document-preview margin-item-enter";
    item.innerHTML = `
      <div class="item-head">
        <div class="item-source">
          <p class="item-src">${escapeHtml(detail.title || title)}</p>
          <span class="item-range">完整原文预览</span>
        </div>
        <button type="button" class="text-action" data-unpin>移除</button>
      </div>`;
    const quote = document.createElement("blockquote");
    quote.className = "positioned-source";
    renderSourceWithImages(quote, detail.content, detail.images ?? []);
    item.append(quote);
    queryOne<HTMLButtonElement>("[data-unpin]", item).addEventListener(
      "click",
      () => removeMarginItem(key),
    );
    byId("margin-list").append(item);
    marginItems.set(key, item);
    updateEvidenceState();
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    notify(friendlyError(error, "读取原文"), true);
  }
}

function markActiveCitation(chunkId: number): void {
  queryAll<HTMLElement>(".cite.on, .source-pill.on")
    .forEach((element) => element.classList.remove("on"));
  queryAll<HTMLElement>(`.cite[data-chunk="${chunkId}"], .source-pill[data-chunk="${chunkId}"]`)
    .forEach((element) => element.classList.add("on"));
}

export function bindCitations(scope: ParentNode, citations: Citation[]): void {
  queryAll<HTMLElement>(".cite, .source-pill", scope).forEach((element) => {
    const open = (): void => {
      const hit = element.dataset.chunk !== undefined
        ? citations.find((citation) => String(citation.chunk_id) === element.dataset.chunk)
        : citations.find((citation) => String(citation.index) === element.dataset.index);
      if (element.dataset.chunk !== undefined) {
        void pinToMargin(
          Number(element.dataset.chunk),
          element.dataset.index ?? null,
          hit ?? null,
        );
        return;
      }
      if (hit !== undefined) {
        element.dataset.chunk = String(hit.chunk_id);
        void pinToMargin(hit.chunk_id, hit.index, hit);
      } else {
        notify("这条编号不在本次引用列表中", true);
      }
    };
    element.addEventListener("click", open);
    element.addEventListener("keydown", (event) => {
      if (!(event instanceof KeyboardEvent)) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
}

function initResizer(): void {
  const resizer = byId("margin-resizer");
  const saved = localStorage.getItem(MARGIN_WIDTH_KEY);
  if (saved !== null) {
    const width = Math.max(300, Math.min(520, Number.parseInt(saved, 10) || 356));
    document.documentElement.style.setProperty("--margin-width", `${width}px`);
  }
  let dragging = false;
  resizer.addEventListener("mousedown", () => {
    dragging = true;
    resizer.classList.add("on");
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("on");
    localStorage.setItem(
      MARGIN_WIDTH_KEY,
      getComputedStyle(document.documentElement).getPropertyValue("--margin-width").trim(),
    );
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const width = Math.max(300, Math.min(520, window.innerWidth - event.clientX));
    document.documentElement.style.setProperty("--margin-width", `${width}px`);
  });
}

export function initEvidence(): void {
  byId<HTMLButtonElement>("margin-clear").addEventListener("click", () => {
    marginItems.clear();
    byId("margin-list").replaceChildren();
    queryAll<HTMLElement>(".cite.on, .source-pill.on")
      .forEach((element) => element.classList.remove("on"));
    updateEvidenceState();
    notify("已清空核对区");
  });
  byId<HTMLButtonElement>("margin-open").addEventListener("click", () => {
    setEvidenceOpen(byId("app-shell").classList.contains("evidence-collapsed"));
  });
  byId<HTMLButtonElement>("margin-close").addEventListener(
    "click",
    () => setEvidenceOpen(false),
  );
  initResizer();
  updateEvidenceState();
  const savedEvidence = localStorage.getItem(EVIDENCE_KEY);
  const evidenceOpen = window.innerWidth > 1020
    && (savedEvidence === null || savedEvidence === "1");
  setEvidenceOpen(evidenceOpen, false);
}
