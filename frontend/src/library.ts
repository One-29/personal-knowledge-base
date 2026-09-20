import { api } from "./api";
import { byId, queryAll, queryOne } from "./dom";
import { openDocument } from "./evidence";
import { escapeHtml, formatCount, formatTimestamp, friendlyError } from "./format";
import { activateView } from "./navigation";
import { notify } from "./notifications";
import { state } from "./state";
import type {
  DocumentStatus,
  KnowledgeBase,
  KnowledgeDocument,
  UploadResult,
} from "./types";

const DOCUMENT_STATUS: Record<DocumentStatus, string> = {
  ready: "可以检索",
  pending: "等待处理",
  processing: "处理中",
  failed: "处理失败",
};

type EmptyAction = "upload" | "reset" | "create";

export async function loadKnowledgeBases(): Promise<void> {
  state.kbs = await api<KnowledgeBase[]>("/kbs");
  renderShelf();
  renderKnowledgeBaseSelects();
}

function renderShelf(): void {
  const list = byId<HTMLUListElement>("kb-list");
  byId("kb-count").textContent = `${state.kbs.length} 个`;
  if (state.kbs.length === 0) {
    list.innerHTML = `<li class="empty-state">
      <h2>还没有知识库</h2>
      <p>在左侧填写名称和说明，创建第一个资料空间。</p>
    </li>`;
    return;
  }

  list.innerHTML = state.kbs.map((knowledgeBase) => `
    <li>
      <div class="kb-glyph" aria-hidden="true">${escapeHtml(knowledgeBase.name.slice(0, 1).toUpperCase())}</div>
      <div class="kb-info">
        <div class="kb-name">${escapeHtml(knowledgeBase.name)}</div>
        <div class="kb-meta">${escapeHtml(knowledgeBase.description || "暂未填写说明")} · ${formatCount(knowledgeBase.doc_count)} 篇文档</div>
      </div>
      <div class="kb-actions">
        <button type="button" class="kb-open" data-open-kb="${knowledgeBase.id}">管理文档</button>
        <button type="button" class="kb-delete" data-drop="${knowledgeBase.id}">删除</button>
      </div>
    </li>`).join("");

  queryAll<HTMLButtonElement>("[data-open-kb]", list).forEach((button) => {
    button.addEventListener("click", () => {
      state.docsKbId = Number(button.dataset.openKb);
      renderKnowledgeBaseSelects();
      activateView("docs");
    });
  });
  queryAll<HTMLButtonElement>("[data-drop]", list).forEach((button) => {
    button.addEventListener("click", () => {
      void deleteKnowledgeBase(button.dataset.drop ?? "");
    });
  });
}

async function deleteKnowledgeBase(id: string): Promise<void> {
  if (!id || !window.confirm("删除这个知识库？它下面的文档和索引会一起清理。")) return;
  try {
    await api<null>(`/kbs/${encodeURIComponent(id)}`, { method: "DELETE" });
    notify("知识库已删除");
    await loadKnowledgeBases();
    if (state.activeView === "docs") await loadDocuments();
  } catch (error) {
    notify(friendlyError(error, "删除知识库"), true);
  }
}

export function renderKnowledgeBaseSelects(): void {
  const ask = byId<HTMLSelectElement>("ask-kb");
  const trace = byId<HTMLSelectElement>("trace-kb");
  const docs = byId<HTMLSelectElement>("docs-kb");
  const map = byId<HTMLSelectElement>("map-kb");
  const askKeep = ask.value || (state.askKbId === null ? "" : String(state.askKbId));
  const traceKeep = trace.value;
  const docsKeep = state.docsKbId === null ? docs.value : String(state.docsKbId);
  const mapKeep = map.value;

  const allOptions = ['<option value="">全部知识库</option>', ...state.kbs.map(
    (knowledgeBase) => `<option value="${knowledgeBase.id}">${escapeHtml(knowledgeBase.name)}</option>`,
  )].join("");
  ask.innerHTML = allOptions;
  trace.innerHTML = allOptions;
  if (Array.from(ask.options).some((option) => option.value === askKeep)) ask.value = askKeep;
  if (Array.from(trace.options).some((option) => option.value === traceKeep)) trace.value = traceKeep;
  state.askKbId = ask.value ? Number(ask.value) : null;

  const scopedOptions = state.kbs.length > 0
    ? state.kbs.map((knowledgeBase) => (
      `<option value="${knowledgeBase.id}">${escapeHtml(knowledgeBase.name)}</option>`
    )).join("")
    : '<option value="">暂无知识库</option>';
  docs.innerHTML = scopedOptions;
  map.innerHTML = scopedOptions;
  docs.disabled = state.kbs.length === 0;
  map.disabled = state.kbs.length === 0;
  if (Array.from(docs.options).some((option) => option.value === docsKeep)) docs.value = docsKeep;
  else if (state.kbs[0] !== undefined) docs.value = String(state.kbs[0].id);
  if (Array.from(map.options).some((option) => option.value === mapKeep)) map.value = mapKeep;
  else if (state.kbs[0] !== undefined) map.value = String(state.kbs[0].id);
  state.docsKbId = docs.value ? Number(docs.value) : null;
}

function updateDocumentStats(documents: KnowledgeDocument[]): void {
  const ready = documents.filter((document) => document.status === "ready").length;
  const processing = documents.filter(
    (document) => document.status === "pending" || document.status === "processing",
  ).length;
  const failed = documents.filter(
    (document) => document.status === "failed" || Boolean(document.last_error_message),
  ).length;
  byId("docs-total").textContent = formatCount(documents.length);
  byId("docs-ready").textContent = formatCount(ready);
  byId("docs-processing").textContent = formatCount(processing);
  byId("docs-failed").textContent = formatCount(failed);
}

function showDocumentsEmpty(
  title: string,
  description: string,
  action: EmptyAction = "upload",
): void {
  const empty = byId("docs-empty");
  queryOne("h2", empty).textContent = title;
  queryOne("p", empty).textContent = description;
  const button = byId<HTMLButtonElement>("docs-empty-upload");
  button.dataset.action = action;
  button.textContent = action === "reset"
    ? "清除筛选"
    : action === "create" ? "新建知识库" : "选择文档";
  empty.classList.remove("hidden");
  byId("docs-table-shell").classList.add("hidden");
}

function renderDocuments(): void {
  const search = byId<HTMLInputElement>("docs-search").value.trim().toLocaleLowerCase("zh-CN");
  const status = byId<HTMLSelectElement>("docs-status-filter").value;
  const filtered = state.docs.filter((document) => (
    (!search || document.title.toLocaleLowerCase("zh-CN").includes(search))
    && (!status || document.status === status)
  ));
  const body = queryOne<HTMLTableSectionElement>("#docs-table tbody");
  updateDocumentStats(state.docs);

  if (state.docs.length === 0) {
    body.replaceChildren();
    showDocumentsEmpty(
      "这个知识库还没有文档",
      "上传 UTF-8 Markdown/纯文本；含本地图片时上传一篇 Markdown 与图片组成的 ZIP。",
    );
    return;
  }
  if (filtered.length === 0) {
    body.replaceChildren();
    showDocumentsEmpty("没有符合条件的文档", "换一个标题关键词或状态筛选后再查看。", "reset");
    return;
  }

  byId("docs-empty").classList.add("hidden");
  byId("docs-table-shell").classList.remove("hidden");
  body.innerHTML = filtered.map((document) => {
    const extension = document.title.toLowerCase().endsWith(".md") ? "MD" : "TXT";
    const statusText = DOCUMENT_STATUS[document.status] ?? String(document.status);
    return `<tr>
      <td><div class="doc-title">
        <span class="doc-icon">${extension}</span>
        <span title="${escapeHtml(document.title)}">${escapeHtml(document.title)}</span>
      </div></td>
      <td><span class="status-badge ${escapeHtml(document.status)}">${escapeHtml(statusText)}</span></td>
      <td class="num">${formatCount(document.chunk_count)}</td>
      <td class="num">${formatCount(document.image_count)}</td>
      <td class="num">${formatCount(document.char_count)}</td>
      <td class="num">${formatTimestamp(document.updated_at)}</td>
      <td class="acts">
        <button type="button" class="table-action" data-open-doc="${document.id}" data-title="${escapeHtml(document.title)}">查看</button>
        <button type="button" class="table-action" data-again="${document.id}">重传</button>
        <button type="button" class="table-action danger" data-remove="${document.id}">删除</button>
      </td>
    </tr>${document.last_error_message ? `<tr class="error-row"><td colspan="7"><span class="hint-inline">${
      document.status === "ready" ? "当前旧索引仍可使用；上次更新未完成" : escapeHtml(document.last_error_code || "处理失败")
    }：${escapeHtml(document.last_error_message)}</span></td></tr>` : ""}`;
  }).join("");

  queryAll<HTMLButtonElement>("[data-open-doc]", body).forEach((button) => {
    button.addEventListener("click", () => {
      void openDocument(Number(button.dataset.openDoc), button.dataset.title ?? "");
    });
  });
  queryAll<HTMLButtonElement>("[data-remove]", body).forEach((button) => {
    button.addEventListener("click", () => { void deleteDocument(button.dataset.remove ?? ""); });
  });
  queryAll<HTMLButtonElement>("[data-again]", body).forEach((button) => {
    button.addEventListener("click", () => reuploadDocument(Number(button.dataset.again)));
  });
}

async function deleteDocument(id: string): Promise<void> {
  if (!id || !window.confirm("删除这篇文档？它的索引和原文会一并清理。")) return;
  try {
    await api<null>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
    notify("文档已删除");
    await loadDocuments();
    await loadKnowledgeBases();
  } catch (error) {
    notify(friendlyError(error, "删除文档"), true);
  }
}

export async function loadDocuments({ quiet = false }: { quiet?: boolean } = {}): Promise<void> {
  if (state.docsPoll !== null) window.clearTimeout(state.docsPoll);
  state.docsPoll = null;
  const loading = byId("docs-loading");
  if (state.docsKbId === null) {
    state.docs = [];
    updateDocumentStats([]);
    showDocumentsEmpty("先创建一个知识库", "知识库用于组织文档，也是问答和关联图的检索范围。", "create");
    return;
  }

  const requestedKnowledgeBase = state.docsKbId;
  if (!quiet) {
    loading.classList.remove("hidden");
    byId("docs-table-shell").classList.add("hidden");
    byId("docs-empty").classList.add("hidden");
  }
  try {
    const documents = await api<KnowledgeDocument[]>(`/kbs/${requestedKnowledgeBase}/documents`);
    if (state.docsKbId !== requestedKnowledgeBase) return;
    state.docs = documents;
    renderDocuments();
    const hasPending = documents.some(
      (document) => document.status === "pending" || document.status === "processing",
    );
    if (state.activeView === "docs" && hasPending) {
      state.docsPoll = window.setTimeout(() => { void loadDocuments({ quiet: true }); }, 2200);
    }
  } catch (error) {
    if (state.docsKbId !== requestedKnowledgeBase) return;
    state.docs = [];
    updateDocumentStats([]);
    showDocumentsEmpty("文档列表读取失败", friendlyError(error, "读取文档"), "reset");
    notify(friendlyError(error, "读取文档"), true);
  } finally {
    loading.classList.add("hidden");
  }
}

function setUploadBusy(busy: boolean): void {
  const label = byId<HTMLLabelElement>("docs-upload-label");
  label.classList.toggle("busy", busy);
  queryOne("span", label).textContent = busy ? "正在上传…" : "上传文档";
  byId<HTMLInputElement>("docs-file").disabled = busy;
}

async function uploadDocument(file: File): Promise<void> {
  if (state.docsKbId === null) {
    notify("请先选择一个知识库", true);
    return;
  }
  const form = new FormData();
  form.append("file", file);
  setUploadBusy(true);
  try {
    await api<UploadResult>(`/kbs/${state.docsKbId}/documents`, { method: "POST", body: form });
    notify(`「${file.name}」已上传，正在建立索引`);
    await loadDocuments();
    await loadKnowledgeBases();
  } catch (error) {
    notify(friendlyError(error, "上传文档"), true);
  } finally {
    setUploadBusy(false);
  }
}

function reuploadDocument(documentId: number): void {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".md,.txt,.zip";
  picker.addEventListener("change", () => {
    const file = picker.files?.[0];
    if (file !== undefined) void submitReupload(documentId, file);
  });
  picker.click();
}

async function submitReupload(documentId: number, file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api<UploadResult>(`/documents/${documentId}/reupload`, {
      method: "POST",
      body: form,
    });
    notify(result.content_changed ? "内容已更新，正在重建索引" : "内容没有变化，已跳过重建");
    await loadDocuments();
  } catch (error) {
    notify(friendlyError(error, "重传文档"), true);
  }
}

async function createKnowledgeBase(form: HTMLFormElement): Promise<void> {
  const name = byId<HTMLInputElement>("kb-name").value.trim();
  const description = byId<HTMLTextAreaElement>("kb-desc").value.trim();
  if (!name) return;
  const submit = queryOne<HTMLButtonElement>('button[type="submit"]', form);
  submit.disabled = true;
  submit.textContent = "正在创建…";
  try {
    await api<KnowledgeBase>("/kbs", {
      method: "POST",
      body: JSON.stringify({ name, description: description || null }),
    });
    form.reset();
    notify(`已新建知识库「${name}」`);
    await loadKnowledgeBases();
  } catch (error) {
    notify(friendlyError(error, "创建知识库"), true);
  } finally {
    submit.disabled = false;
    submit.textContent = "创建知识库";
  }
}

export function initLibrary(): void {
  const form = byId<HTMLFormElement>("kb-form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void createKnowledgeBase(form);
  });
  const documentsKnowledgeBase = byId<HTMLSelectElement>("docs-kb");
  documentsKnowledgeBase.addEventListener("change", () => {
    state.docsKbId = documentsKnowledgeBase.value
      ? Number(documentsKnowledgeBase.value)
      : null;
    void loadDocuments();
  });
  byId("docs-refresh").addEventListener("click", () => { void loadDocuments(); });
  byId("docs-search").addEventListener("input", renderDocuments);
  byId("docs-status-filter").addEventListener("change", renderDocuments);
  const emptyAction = byId<HTMLButtonElement>("docs-empty-upload");
  emptyAction.addEventListener("click", () => {
    const action = emptyAction.dataset.action as EmptyAction | undefined;
    if (action === "reset") {
      byId<HTMLInputElement>("docs-search").value = "";
      byId<HTMLSelectElement>("docs-status-filter").value = "";
      renderDocuments();
    } else if (action === "create") {
      activateView("kbs");
      byId<HTMLInputElement>("kb-name").focus();
    } else {
      byId<HTMLInputElement>("docs-file").click();
    }
  });
  const fileInput = byId<HTMLInputElement>("docs-file");
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    fileInput.value = "";
    if (file !== undefined) void uploadDocument(file);
  });
}
