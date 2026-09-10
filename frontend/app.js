/**
 * KnowBase 前端（原生 JS，无构建步骤）。
 *
 * 设计约束（frontend-design skill 的设计计划）：
 * - 前端只渲染后端契约对象，不做业务判定（排序/拒答/溯源拼接都在后端，02 §3 M5 边界）；
 * - 「核对」是核心动作，所以原文显示在常驻的页边注栏，而不是弹窗；
 * - 引用编号是整页唯一的高饱和元素。
 */

const API = "/api/v1";

const state = {
  kbs: [],
  docsKbId: null,
  askKbId: null,
  sessionId: sessionStorage.getItem("kb_session") || newSessionId(),
};

/* ── 基础工具 ─────────────────────────────────────── */

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
    } catch (_) { /* 响应体不是 JSON，沿用状态文本 */ }
    throw new Error(`${resp.status} ${detail}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function notify(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 2800);
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** 把回答里的 [n] 变成可点的引用编号；其余内容转义后原样输出。 */
function withCitations(content) {
  return esc(content).replace(/\[(\d+)\]/g, (_, n) =>
    `<span class="cite" data-index="${n}" role="button" tabindex="0">[${n}]</span>`);
}

function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/* ── 视图切换 ─────────────────────────────────────── */

document.querySelectorAll(".views button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".views button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("on"));
    btn.classList.add("on");
    document.getElementById("view-" + btn.dataset.view).classList.add("on");
    if (btn.dataset.view === "docs") loadDocs();
  });
});

/* ── 页边注：核对原文 ─────────────────────────────── */

async function showInMargin(chunkId) {
  try {
    const detail = await api(`/citations/${chunkId}`);
    document.getElementById("margin-hint").classList.add("hidden");
    const body = document.getElementById("margin-body");
    body.classList.remove("hidden");
    body.classList.remove("margin-body-enter");
    void body.offsetWidth;                       // 重放淡入（只响应点击动作）
    body.classList.add("margin-body-enter");

    document.getElementById("margin-src").textContent =
      `${detail.doc_title} · 偏移 ${detail.char_start}–${detail.char_end}`;
    document.getElementById("margin-text").textContent = detail.chunk_text;
    document.getElementById("margin-note").textContent = `块 ${chunkId} · 块内文本即上方段落`;
    document.querySelectorAll(".cite.on").forEach((el) => el.classList.remove("on"));
    document.querySelectorAll(`.cite[data-index]`).forEach((el) => {
      if (el.dataset.chunk === String(chunkId)) el.classList.add("on");
    });
    if (window.innerWidth <= 900) document.getElementById("margin").scrollIntoView({ behavior: "smooth" });
  } catch (err) { notify(err.message, true); }
}

function bindCitations(scope, citations) {
  scope.querySelectorAll(".cite").forEach((el) => {
    const chunkId = el.dataset.chunk;
    const index = el.dataset.index;
    const open = () => {
      if (chunkId) return showInMargin(Number(chunkId));
      const hit = (citations || []).find((c) => String(c.index) === index);
      if (hit) showInMargin(hit.chunk_id);
      else notify("这条编号不在本次引用列表中", true);
    };
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
}

/* ── 知识库 ───────────────────────────────────────── */

async function loadKbs() {
  state.kbs = await api("/kbs");
  renderShelf();
  renderKbSelects();
}

function renderShelf() {
  const list = document.getElementById("kb-list");
  if (!state.kbs.length) {
    list.innerHTML = '<li class="empty">还没有知识库。先建一个，比如「计算机网络」。</li>';
    return;
  }
  list.innerHTML = state.kbs.map((kb) => `
    <li>
      <div>
        <div class="kb-name">${esc(kb.name)}</div>
        <div class="kb-meta">${esc(kb.description || "没有说明")} · <code>${kb.doc_count}</code> 篇文档</div>
      </div>
      <button class="link" data-drop="${kb.id}">删除</button>
    </li>`).join("");

  list.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.drop;
      if (!confirm("删除这个知识库？它下面的文档和索引会一起清理。")) return;
      try {
        await api(`/kbs/${id}`, { method: "DELETE" });
        notify("知识库已删除");
        await loadKbs();
      } catch (err) { notify(err.message, true); }
    });
  });
}

function renderKbSelects() {
  const askSelect = document.getElementById("ask-kb");
  const traceSelect = document.getElementById("trace-kb");
  const docsSelect = document.getElementById("docs-kb");

  const askHtml = ['<option value="">全部知识库</option>']
    .concat(state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`)).join("");
  const keepAsk = askSelect.value;
  askSelect.innerHTML = askHtml;
  traceSelect.innerHTML = askHtml;
  if (keepAsk) askSelect.value = keepAsk;

  docsSelect.innerHTML = state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`).join("");
  if (state.docsKbId && state.kbs.some((kb) => kb.id === state.docsKbId)) {
    docsSelect.value = String(state.docsKbId);
  } else if (state.kbs.length) {
    docsSelect.value = String(state.kbs[0].id);
  }
  state.docsKbId = docsSelect.value ? Number(docsSelect.value) : null;
}

document.getElementById("kb-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = document.getElementById("kb-name").value.trim();
  const description = document.getElementById("kb-desc").value.trim();
  if (!name) return;
  try {
    await api("/kbs", { method: "POST", body: JSON.stringify({ name, description: description || null }) });
    event.target.reset();
    notify(`已新建知识库「${name}」`);
    await loadKbs();
  } catch (err) { notify(err.message, true); }
});

/* ── 文档清单 ─────────────────────────────────────── */

async function loadDocs() {
  const tbody = document.querySelector("#docs-table tbody");
  const empty = document.getElementById("docs-empty");
  if (!state.docsKbId) {
    tbody.innerHTML = "";
    empty.textContent = "先建一个知识库，再来上传笔记。";
    empty.classList.remove("hidden");
    return;
  }
  try {
    const docs = await api(`/kbs/${state.docsKbId}/documents`);
    tbody.innerHTML = docs.map((doc) => `
      <tr>
        <td>${esc(doc.title)}</td>
        <td class="state ${doc.status}">${doc.status}</td>
        <td class="num">${doc.chunk_count}</td>
        <td class="num">${doc.char_count}</td>
        <td class="num">${stamp(doc.updated_at)}</td>
        <td class="acts">
          <button class="link" data-again="${doc.id}">重传</button>
          <button class="link" data-remove="${doc.id}">删除</button>
        </td>
      </tr>
      ${doc.last_error_message ? `<tr><td colspan="6" class="hint-inline">${
        esc(doc.last_error_code || "失败")}：${esc(doc.last_error_message)}</td></tr>` : ""}
    `).join("");
    empty.classList.toggle("hidden", docs.length > 0);

    tbody.querySelectorAll("[data-remove]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("删除这篇文档？")) return;
        try {
          await api(`/documents/${btn.dataset.remove}`, { method: "DELETE" });
          notify("文档已删除");
          await loadDocs();
          await loadKbs();
        } catch (err) { notify(err.message, true); }
      });
    });
    tbody.querySelectorAll("[data-again]").forEach((btn) => {
      btn.addEventListener("click", () => pickAndReupload(Number(btn.dataset.again)));
    });
  } catch (err) { notify(err.message, true); }
}

function pickAndReupload(docId) {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".md,.txt";
  picker.addEventListener("change", async () => {
    if (!picker.files.length) return;
    const form = new FormData();
    form.append("file", picker.files[0]);
    try {
      const result = await api(`/documents/${docId}/reupload`, { method: "POST", body: form });
      notify(result.content_changed ? "内容已更新，正在重建索引" : "内容没有变化，跳过重建");
      await loadDocs();
    } catch (err) { notify(err.message, true); }
  });
  picker.click();
}

document.getElementById("docs-kb").addEventListener("change", (event) => {
  state.docsKbId = event.target.value ? Number(event.target.value) : null;
  loadDocs();
});
document.getElementById("docs-refresh").addEventListener("click", loadDocs);

document.getElementById("docs-file").addEventListener("change", async (event) => {
  if (!state.docsKbId) { notify("先选一个知识库", true); return; }
  const file = event.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await api(`/kbs/${state.docsKbId}/documents`, { method: "POST", body: form });
    notify("已上传，正在后台切块并建立索引");
    event.target.value = "";
    await loadDocs();
    await loadKbs();
  } catch (err) { notify(err.message, true); }
});

/* ── 问答 ─────────────────────────────────────────── */

document.getElementById("ask-kb").addEventListener("change", (event) => {
  state.askKbId = event.target.value ? Number(event.target.value) : null;
});

document.getElementById("ask-new").addEventListener("click", () => {
  state.sessionId = newSessionId();
  showSession();
  document.getElementById("ask-flow").innerHTML = "";
  notify("已开始新会话，之前的问题不再作为上下文");
});

function showSession() {
  document.getElementById("ask-session").textContent = `会话 ${state.sessionId}`;
}

document.getElementById("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("ask-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";

  const flow = document.getElementById("ask-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  entry.innerHTML = `<h3 class="entry-q">${esc(question)}</h3><p class="entry-a">正在检索并生成…</p>`;
  flow.appendChild(entry);
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    const answer = await api("/ask", {
      method: "POST",
      body: JSON.stringify({ question, kb_id: state.askKbId, session_id: state.sessionId }),
    });
    entry.innerHTML = renderEntry(question, answer);
    bindCitations(entry, answer.citations);
  } catch (err) {
    entry.innerHTML = `<h3 class="entry-q">${esc(question)}</h3>
      <p class="entry-a">没能取到回答：${esc(err.message)}</p>`;
  }
});

function renderEntry(question, answer) {
  const refusal = answer.refused
    ? `<span class="refused">未作答 · ${esc(answer.refusal_reason || "")}</span>` : "";
  const rewrote = answer.search_query && answer.search_query !== question
    ? `<p class="rewrote">按上文改写后检索：<code>${esc(answer.search_query)}</code></p>` : "";
  return `<h3 class="entry-q">${esc(question)}</h3>
    ${refusal}${rewrote}
    <p class="entry-a">${withCitations(answer.content)}</p>`;
}

/* ── 工作流 ───────────────────────────────────────── */

document.getElementById("trace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("trace-input");
  const task = input.value.trim();
  if (!task) return;
  input.value = "";

  const flow = document.getElementById("trace-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  entry.innerHTML = `<h3 class="entry-q">${esc(task)}</h3><p class="entry-a">正在拆解任务并逐步执行…</p>`;
  flow.appendChild(entry);

  const kbValue = document.getElementById("trace-kb").value;
  try {
    const result = await api("/workflow", {
      method: "POST",
      body: JSON.stringify({ task, kb_id: kbValue ? Number(kbValue) : null }),
    });
    entry.innerHTML = renderTrace(task, result);
    bindCitations(entry, result.citations);
  } catch (err) {
    entry.innerHTML = `<h3 class="entry-q">${esc(task)}</h3>
      <p class="entry-a">没能执行：${esc(err.message)}</p>`;
  }
});

const STATE_WORD = { answered: "已作答", insufficient: "没有材料", error: "未完成" };

function renderTrace(task, result) {
  const steps = result.steps.map((step) => {
    const body = step.status === "answered"
      ? `<p class="trace-body">${withCitations(step.conclusion || "")}</p>`
      : `<p class="trace-note">${esc(step.note || "")}</p>`;
    const cites = step.citations.length
      ? `<p class="trace-query">${step.citations.map((c) =>
          `<span class="cite" data-chunk="${c.chunk_id}" role="button" tabindex="0">[${c.index}]</span>`
        ).join(" ")}</p>` : "";
    return `<li>
        <div class="trace-head">${esc(step.goal)}
          <span class="trace-state ${step.status}">${STATE_WORD[step.status] || step.status}</span>
        </div>
        <p class="trace-query">检索：${esc(step.query)}</p>
        ${body}${cites}
      </li>`;
  }).join("");

  return `<h3 class="entry-q">${esc(task)}</h3>
    <ol class="trace-list">${steps}</ol>
    <div class="summary">
      <h3>汇总</h3>
      <p class="entry-a">${withCitations(result.answer)}</p>
    </div>`;
}

/* ── 启动 ─────────────────────────────────────────── */

(async function start() {
  showSession();
  try {
    await loadKbs();
    await loadDocs();
  } catch (err) {
    notify("加载失败：" + err.message, true);
  }
})();
