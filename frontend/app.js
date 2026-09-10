/**
 * KnowBase 前端（原生 JS，无构建步骤）。
 *
 * 设计约束：
 * - 前端只渲染后端契约对象，不做业务判定（排序/拒答/溯源拼接都在后端，02 §3 M5 边界）；
 * - 引用 [n] 的渲染与点击溯源是本文件的核心交互；
 * - 会话 id 存 sessionStorage：刷新页面仍能追问同一会话。
 */

const API = "/api/v1";

const state = {
  kbs: [],
  docsKbId: null,
  qaKbId: null,
  sessionId: sessionStorage.getItem("kb_session") || newSessionId(),
};

/* ── 工具 ─────────────────────────────────────────── */

function newSessionId() {
  const id = "s-" + Math.random().toString(36).slice(2, 10);
  sessionStorage.setItem("kb_session", id);
  return id;
}

async function api(path, options = {}) {
  const resp = await fetch(API + path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* 响应体非 JSON，沿用状态文本 */ }
    throw new Error(`${resp.status} ${detail}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** 把回答里的 [n] 渲染成可点击的引用标记（其余内容原样转义）。 */
function renderAnswerHtml(content) {
  return escapeHtml(content).replace(/\[(\d+)\]/g, (_, n) =>
    `<span class="cite" data-index="${n}" title="查看第 ${n} 条来源">[${n}]</span>`);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false });
}

/* ── 标签页 ───────────────────────────────────────── */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

/* ── 知识库 ───────────────────────────────────────── */

async function loadKbs() {
  state.kbs = await api("/kbs");
  renderKbList();
  renderKbSelects();
}

function renderKbList() {
  const list = document.getElementById("kb-list");
  if (!state.kbs.length) {
    list.innerHTML = '<li class="muted">还没有知识库，先新建一个。</li>';
    return;
  }
  list.innerHTML = state.kbs.map((kb) => `
    <li class="kb-item">
      <div>
        <div>${escapeHtml(kb.name)} <small>· ${kb.doc_count} 篇文档</small></div>
        <small>${escapeHtml(kb.description || "无描述")} · 创建于 ${fmtTime(kb.created_at)}</small>
      </div>
      <button class="ghost small" data-del-kb="${kb.id}">删除</button>
    </li>`).join("");

  list.querySelectorAll("[data-del-kb]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.delKb;
      if (!confirm("确认删除这个知识库？其下文档与向量会一并清理。")) return;
      try {
        await api(`/kbs/${id}`, { method: "DELETE" });
        toast("知识库已删除");
        await loadKbs();
      } catch (err) { toast(err.message, true); }
    });
  });
}

function renderKbSelects() {
  const options = ['<option value="">（全部知识库）</option>']
    .concat(state.kbs.map((kb) => `<option value="${kb.id}">${escapeHtml(kb.name)}</option>`));
  const html = options.join("");

  const qaSelect = document.getElementById("qa-kb");
  const wfSelect = document.getElementById("wf-kb");
  const docsSelect = document.getElementById("docs-kb");

  const keepQa = qaSelect.value;
  qaSelect.innerHTML = html;
  wfSelect.innerHTML = html;
  docsSelect.innerHTML = state.kbs.map((kb) =>
    `<option value="${kb.id}">${escapeHtml(kb.name)}</option>`).join("");

  if (keepQa) qaSelect.value = keepQa;
  if (state.docsKbId) docsSelect.value = String(state.docsKbId);
  else if (state.kbs.length) docsSelect.value = String(state.kbs[0].id);
  state.docsKbId = docsSelect.value ? Number(docsSelect.value) : null;
}

document.getElementById("kb-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = document.getElementById("kb-name").value.trim();
  const description = document.getElementById("kb-desc").value.trim();
  if (!name) return;
  try {
    await api("/kbs", { method: "POST", body: JSON.stringify({ name, description: description || null }) });
    document.getElementById("kb-form").reset();
    toast("知识库已创建");
    await loadKbs();
  } catch (err) { toast(err.message, true); }
});

/* ── 文档 ─────────────────────────────────────────── */

async function loadDocs() {
  if (!state.docsKbId) {
    document.querySelector("#docs-table tbody").innerHTML = "";
    document.getElementById("docs-empty").textContent = "先新建一个知识库。";
    document.getElementById("docs-empty").classList.remove("hidden");
    return;
  }
  try {
    const docs = await api(`/kbs/${state.docsKbId}/documents`);
    const tbody = document.querySelector("#docs-table tbody");
    tbody.innerHTML = docs.map((doc) => `
      <tr>
        <td>${escapeHtml(doc.title)}</td>
        <td><span class="status ${doc.status}">${doc.status}</span>
            ${doc.last_error_message ? `<small class="muted" title="${escapeHtml(doc.last_error_message)}">（${escapeHtml(doc.last_error_code || "")}）</small>` : ""}
        </td>
        <td>${doc.chunk_count}</td>
        <td>${doc.char_count}</td>
        <td>${fmtTime(doc.updated_at)}</td>
        <td>
          <button class="ghost small" data-reupload="${doc.id}" data-title="${escapeHtml(doc.title)}">重传</button>
          <button class="ghost small" data-del-doc="${doc.id}">删除</button>
        </td>
      </tr>`).join("");
    document.getElementById("docs-empty").classList.toggle("hidden", docs.length > 0);

    tbody.querySelectorAll("[data-del-doc]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("确认删除这篇文档？")) return;
        try {
          await api(`/documents/${btn.dataset.delDoc}`, { method: "DELETE" });
          toast("文档已删除");
          await loadDocs();
          await loadKbs();
        } catch (err) { toast(err.message, true); }
      });
    });

    tbody.querySelectorAll("[data-reupload]").forEach((btn) => {
      btn.addEventListener("click", () => reupload(Number(btn.dataset.reupload)));
    });
  } catch (err) { toast(err.message, true); }
}

async function reupload(docId) {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".md,.txt";
  picker.addEventListener("change", async () => {
    if (!picker.files.length) return;
    const form = new FormData();
    form.append("file", picker.files[0]);
    try {
      const result = await api(`/documents/${docId}/reupload`, { method: "POST", body: form });
      toast(result.content_changed ? "内容已更新，正在重建索引" : "内容未变化，已跳过重建");
      await loadDocs();
    } catch (err) { toast(err.message, true); }
  });
  picker.click();
}

document.getElementById("docs-kb").addEventListener("change", (event) => {
  state.docsKbId = event.target.value ? Number(event.target.value) : null;
  loadDocs();
});
document.getElementById("docs-refresh").addEventListener("click", loadDocs);

document.getElementById("docs-file").addEventListener("change", async (event) => {
  if (!state.docsKbId) { toast("请先选择知识库", true); return; }
  const file = event.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await api(`/kbs/${state.docsKbId}/documents`, { method: "POST", body: form });
    toast("上传成功，正在后台切分与向量化");
    event.target.value = "";
    await loadDocs();
    await loadKbs();
  } catch (err) { toast(err.message, true); }
});

/* ── 问答 ─────────────────────────────────────────── */

document.getElementById("qa-reset").addEventListener("click", () => {
  state.sessionId = newSessionId();
  renderSessionHint();
  document.getElementById("qa-log").innerHTML = "";
  toast("已开始新会话");
});

function renderSessionHint() {
  document.getElementById("qa-session").textContent = `会话：${state.sessionId}`;
}

document.getElementById("qa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("qa-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendQuestion(question);
  const placeholder = appendPending();

  try {
    const answer = await api("/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        kb_id: state.qaKbId,
        session_id: state.sessionId,
      }),
    });
    placeholder.replaceWith(renderAnswer(question, answer));
  } catch (err) {
    placeholder.replaceWith(renderError(err.message));
  }
});

function appendQuestion(question) {
  const log = document.getElementById("qa-log");
  const div = document.createElement("div");
  div.className = "qa-item";
  div.innerHTML = `<p class="qa-q">Q：${escapeHtml(question)}</p>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function appendPending() {
  const log = document.getElementById("qa-log");
  const div = document.createElement("div");
  div.className = "qa-item";
  div.innerHTML = '<p class="muted">检索并生成中…</p>';
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function renderError(message) {
  const div = document.createElement("div");
  div.className = "qa-item";
  div.innerHTML = `<p class="muted">请求失败：${escapeHtml(message)}</p>`;
  return div;
}

function renderAnswer(question, answer) {
  const div = document.createElement("div");
  div.className = "qa-item" + (answer.refused ? " qa-refused" : "");

  const rewrite = answer.search_query && answer.search_query !== question
    ? `<p class="rewrite-note">追问改写后的检索用语：${escapeHtml(answer.search_query)}</p>` : "";
  const refusal = answer.refused
    ? `<span class="refusal-tag">拒答 · ${escapeHtml(answer.refusal_reason || "")}</span>` : "";
  const citations = answer.citations.length
    ? `<div class="citations">${answer.citations.map((c) => `
        <div class="citation-row">
          <span class="cite" data-chunk="${c.chunk_id}">[${c.index}]</span>
          <b>${escapeHtml(c.doc_title)}</b> · 偏移 ${c.char_start}-${c.char_end}
          · ${escapeHtml(c.chunk_text.slice(0, 60))}…
        </div>`).join("")}</div>` : "";

  div.innerHTML = `<p class="qa-q">A：</p>${refusal}${rewrite}
    <p class="qa-a">${renderAnswerHtml(answer.content)}</p>${citations}`;

  div.querySelectorAll("[data-chunk]").forEach((el) => {
    el.addEventListener("click", () => openCitation(Number(el.dataset.chunk)));
  });
  div.querySelectorAll(".qa-a .cite").forEach((el) => {
    const index = Number(el.dataset.index);
    el.addEventListener("click", () => {
      const citation = answer.citations.find((c) => c.index === index);
      if (citation) openCitation(citation.chunk_id);
      else toast("该引用不在本次引用列表中", true);
    });
  });
  return div;
}

document.getElementById("qa-kb").addEventListener("change", (event) => {
  state.qaKbId = event.target.value ? Number(event.target.value) : null;
});

/* ── 溯源抽屉 ─────────────────────────────────────── */

async function openCitation(chunkId) {
  try {
    const detail = await api(`/citations/${chunkId}`);
    document.getElementById("citation-title").textContent = `引用原文 · ${detail.doc_title}`;
    document.getElementById("citation-text").textContent = detail.chunk_text;
    document.getElementById("citation-meta").textContent =
      `块 ${chunkId} · 原文偏移 ${detail.char_start}-${detail.char_end}（在文档中按此区间高亮）`;
    document.getElementById("citation-drawer").classList.remove("hidden");
  } catch (err) { toast(err.message, true); }
}

document.getElementById("citation-close").addEventListener("click", () => {
  document.getElementById("citation-drawer").classList.add("hidden");
});

/* ── 工作流 ───────────────────────────────────────── */

document.getElementById("wf-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("wf-input");
  const task = input.value.trim();
  if (!task) return;
  input.value = "";

  const log = document.getElementById("wf-log");
  const item = document.createElement("div");
  item.className = "qa-item";
  item.innerHTML = `<p class="qa-q">任务：${escapeHtml(task)}</p><p class="muted">拆解并逐步执行中…</p>`;
  log.appendChild(item);

  const kbValue = document.getElementById("wf-kb").value;
  try {
    const result = await api("/workflow", {
      method: "POST",
      body: JSON.stringify({ task, kb_id: kbValue ? Number(kbValue) : null }),
    });
    item.innerHTML = renderWorkflowHtml(task, result);
    item.querySelectorAll("[data-chunk]").forEach((el) => {
      el.addEventListener("click", () => openCitation(Number(el.dataset.chunk)));
    });
  } catch (err) {
    item.innerHTML = `<p class="qa-q">任务：${escapeHtml(task)}</p>
      <p class="muted">执行失败：${escapeHtml(err.message)}</p>`;
  }
});

function renderWorkflowHtml(task, result) {
  const steps = result.steps.map((step) => {
    const badge = `<span class="badge ${step.status}">${
      { answered: "已作答", insufficient: "缺料", error: "未完成" }[step.status] || step.status}</span>`;
    const body = step.status === "answered"
      ? `<p class="step-body">${renderAnswerHtml(step.conclusion || "")}</p>`
      : `<p class="step-note">${escapeHtml(step.note || "")}</p>`;
    const citations = step.citations.length
      ? `<div class="citations">${step.citations.map((c) => `
          <div class="citation-row">
            <span class="cite" data-chunk="${c.chunk_id}">[${c.index}]</span>
            <b>${escapeHtml(c.doc_title)}</b> · ${escapeHtml(c.chunk_text.slice(0, 50))}…
          </div>`).join("")}</div>` : "";
    return `<div class="step ${step.status}">
        <div class="step-head">${step.index}. ${escapeHtml(step.goal)}${badge}</div>
        <div class="step-query">检索用语：${escapeHtml(step.query)}</div>
        ${body}${citations}
      </div>`;
  }).join("");

  const globalCitations = result.citations.length
    ? `<div class="citations">${result.citations.map((c) => `
        <div class="citation-row">
          <span class="cite" data-chunk="${c.chunk_id}">[${c.index}]</span>
          <b>${escapeHtml(c.doc_title)}</b> · ${escapeHtml(c.chunk_text.slice(0, 60))}…
        </div>`).join("")}</div>` : "";

  return `<p class="qa-q">任务：${escapeHtml(task)}</p>
    <div class="steps">${steps}</div>
    <div class="citations"><b>汇总结果</b></div>
    <p class="qa-a">${renderAnswerHtml(result.answer)}</p>
    ${globalCitations}`;
}

/* ── 启动 ─────────────────────────────────────────── */

(async function init() {
  renderSessionHint();
  try {
    await loadKbs();
    await loadDocs();
  } catch (err) {
    toast("加载失败：" + err.message, true);
  }
})();
