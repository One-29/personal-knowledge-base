/**
 * KnowBase 前端（原生 JS，无构建步骤）。
 *
 * 设计约束（frontend-design skill 的设计计划）：
 * - 前端只渲染后端契约对象，不做业务判定（排序/拒答/溯源拼接都在后端，02 §3 M5 边界）；
 * - 「核对」是核心动作，原文显示在常驻页边注栏，而非弹窗；
 * - 引用编号是整页唯一的高饱和元素。
 *
 * 会话记录保存在浏览器本地（localStorage），并把最近几轮随请求回传给后端做
 * 追问改写——后端因此保持无状态：刷新页面或重启服务都不会丢掉对话上下文。
 */

const API = "/api/v1";
const CONV_KEY = "kb_conversations";
const ACTIVE_KEY = "kb_active_conv";
const HISTORY_TURNS = 6;          // 回传给后端的最近轮数（用于指代句改写）

const state = {
  kbs: [],
  docsKbId: null,
  askKbId: null,
  controller: null,               // 请求中的 AbortController（停止按钮用）
};

/* ── 基础工具 ─────────────────────────────────────── */

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

/* ── 会话（本地保存） ─────────────────────────────── */

function loadConversations() {
  try { return JSON.parse(localStorage.getItem(CONV_KEY)) || []; } catch (_) { return []; }
}

function persistConversations(list) {
  localStorage.setItem(CONV_KEY, JSON.stringify(list.slice(0, 50)));
}

function activeConversation() {
  const id = localStorage.getItem(ACTIVE_KEY);
  return loadConversations().find((c) => c.id === id) || null;
}

function startConversation() {
  const conv = {
    id: "c-" + Date.now().toString(36),
    title: "",
    at: Date.now(),
    turns: [],
  };
  const list = loadConversations();
  list.unshift(conv);
  persistConversations(list);
  localStorage.setItem(ACTIVE_KEY, conv.id);
  return conv;
}

function ensureConversation() {
  return activeConversation() || startConversation();
}

function useConversation(id) {
  localStorage.setItem(ACTIVE_KEY, id);
  renderConversation();
  renderConvSelect();
}

function dropConversation(id) {
  const list = loadConversations().filter((c) => c.id !== id);
  persistConversations(list);
  if (localStorage.getItem(ACTIVE_KEY) === id) {
    if (list.length) localStorage.setItem(ACTIVE_KEY, list[0].id);
    else localStorage.removeItem(ACTIVE_KEY);
  }
  renderConversation();
  renderConvSelect();
}

function rememberTurn(turn) {
  const conv = ensureConversation();
  conv.turns.push(turn);
  conv.at = Date.now();
  if (!conv.title) conv.title = turn.question.slice(0, 24);
  const list = loadConversations();
  const index = list.findIndex((c) => c.id === conv.id);
  if (index >= 0) list[index] = conv; else list.unshift(conv);
  persistConversations(list);
  renderConvSelect();
  flashSaved();
}

function flashSaved() {
  const el = document.getElementById("ask-saved");
  el.textContent = "已保存到本地";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.textContent = ""; }, 1800);
}

function renderConvSelect() {
  const select = document.getElementById("ask-conv");
  const list = loadConversations();
  select.innerHTML = list.map((c) =>
    `<option value="${c.id}">${esc(c.title || "新会话")} · ${c.turns.length} 轮</option>`).join("");
  const active = activeConversation();
  if (active) select.value = active.id;
  else if (list.length) select.value = list[0].id;
}

/** 回传给后端的最近几轮（用于指代句改写）。 */
function historyForRequest(conv) {
  return conv.turns.slice(-HISTORY_TURNS).map((t) => ({
    question: t.question,
    answer: (t.answerText || "").slice(0, 1500),
  }));
}

/* ── 视图切换 ─────────────────────────────────────── */

document.querySelectorAll(".views button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".views button").forEach((b) => b.classList.remove("on"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("on"));
    btn.classList.add("on");
    document.getElementById("view-" + btn.dataset.view).classList.add("on");
    if (btn.dataset.view === "docs") loadDocs();
    if (btn.dataset.view === "map") loadGraph();
  });
});

/* ── 页边注：核对原文 ─────────────────────────────── */

async function showInMargin(chunkId) {
  try {
    const detail = await api(`/citations/${chunkId}`);
    document.getElementById("margin-hint").classList.add("hidden");
    const body = document.getElementById("margin-body");
    body.classList.remove("hidden", "margin-body-enter");
    void body.offsetWidth;                          // 重放淡入（只响应点击）
    body.classList.add("margin-body-enter");

    document.getElementById("margin-src").textContent =
      `${detail.doc_title} · 偏移 ${detail.char_start}–${detail.char_end}`;
    document.getElementById("margin-text").textContent = detail.chunk_text;
    document.getElementById("margin-note").textContent = `块 ${chunkId}`;
    document.querySelectorAll(".cite.on").forEach((el) => el.classList.remove("on"));
    document.querySelectorAll(".cite[data-chunk]").forEach((el) => {
      if (el.dataset.chunk === String(chunkId)) el.classList.add("on");
    });
    if (window.innerWidth <= 900) {
      document.getElementById("margin").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (err) { notify(err.message, true); }
}

function bindCitations(scope, citations) {
  scope.querySelectorAll(".cite").forEach((el) => {
    const open = () => {
      if (el.dataset.chunk) return showInMargin(Number(el.dataset.chunk));
      const hit = (citations || []).find((c) => String(c.index) === el.dataset.index);
      if (hit) showInMargin(hit.chunk_id);
      else notify("这条编号不在本次引用列表中", true);
    };
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
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
      if (!confirm("删除这个知识库？它下面的文档和索引会一起清理。")) return;
      try {
        await api(`/kbs/${btn.dataset.drop}`, { method: "DELETE" });
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
  const mapSelect = document.getElementById("map-kb");

  const html = ['<option value="">全部知识库</option>']
    .concat(state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`)).join("");
  const keep = askSelect.value;
  askSelect.innerHTML = html;
  traceSelect.innerHTML = html;
  if (keep) askSelect.value = keep;

  const onlyKbs = state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`).join("");
  docsSelect.innerHTML = onlyKbs;
  mapSelect.innerHTML = onlyKbs;
  if (state.docsKbId && state.kbs.some((kb) => kb.id === state.docsKbId)) {
    docsSelect.value = String(state.docsKbId);
  } else if (state.kbs.length) {
    docsSelect.value = String(state.kbs[0].id);
  }
  if (!mapSelect.value && state.kbs.length) mapSelect.value = String(state.kbs[0].id);
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

document.getElementById("ask-conv").addEventListener("change", (event) => {
  if (event.target.value) useConversation(event.target.value);
});

document.getElementById("ask-new").addEventListener("click", () => {
  startConversation();
  renderConversation();
  renderConvSelect();
  notify("已新建会话");
});

document.getElementById("ask-drop").addEventListener("click", () => {
  const conv = activeConversation();
  if (!conv) { notify("还没有会话", true); return; }
  if (!confirm(`删除会话「${conv.title || "新会话"}」？本机保存的记录会一并清除。`)) return;
  dropConversation(conv.id);
  notify("会话已删除");
});

document.getElementById("ask-stop").addEventListener("click", () => {
  if (state.controller) {
    state.controller.abort();
    notify("已停止等待这次回答");
  }
});

function setBusy(busy) {
  document.getElementById("ask-stop").classList.toggle("hidden", !busy);
  document.getElementById("ask-send").disabled = busy;
}

document.getElementById("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.controller) { notify("上一次提问还在进行中", true); return; }

  const input = document.getElementById("ask-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";

  const conv = ensureConversation();
  const flow = document.getElementById("ask-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  entry.innerHTML = `<h3 class="entry-q">${esc(question)}</h3>
    <p class="entry-a">正在检索资料并组织回答…</p>`;
  flow.appendChild(entry);
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });

  state.controller = new AbortController();
  setBusy(true);
  try {
    const answer = await api("/ask", {
      method: "POST",
      signal: state.controller.signal,
      body: JSON.stringify({
        question,
        kb_id: state.askKbId,
        history: historyForRequest(conv),
      }),
    });
    entry.innerHTML = renderAnswerEntry(question, answer);
    bindCitations(entry, answer.citations);
    rememberTurn({ kind: "ask", question, answer, answerText: answer.content, at: Date.now() });
  } catch (err) {
    if (err.name === "AbortError") {
      entry.innerHTML = `<h3 class="entry-q">${esc(question)}</h3>
        <p class="entry-a">已停止等待这次回答。问题没有保存进会话。</p>`;
    } else {
      entry.innerHTML = `<h3 class="entry-q">${esc(question)}</h3>
        <p class="entry-a">没能取到回答：${esc(err.message)}</p>`;
    }
  } finally {
    state.controller = null;
    setBusy(false);
  }
});

function renderAnswerEntry(question, answer) {
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
  if (state.controller) { notify("上一次任务还在进行中", true); return; }

  const input = document.getElementById("trace-input");
  const task = input.value.trim();
  if (!task) return;
  input.value = "";

  const conv = ensureConversation();
  const flow = document.getElementById("trace-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  entry.innerHTML = `<h3 class="entry-q">${esc(task)}</h3>
    <p class="entry-a">正在拆解任务并逐步执行…（可点「停止」中断等待）</p>`;
  flow.appendChild(entry);

  const kbValue = document.getElementById("trace-kb").value;
  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "link";
  stop.textContent = "停止";
  stop.addEventListener("click", () => state.controller && state.controller.abort());
  entry.appendChild(stop);

  state.controller = new AbortController();
  try {
    const result = await api("/workflow", {
      method: "POST",
      signal: state.controller.signal,
      body: JSON.stringify({ task, kb_id: kbValue ? Number(kbValue) : null }),
    });
    entry.innerHTML = renderTrace(task, result);
    bindCitations(entry, result.citations);
    rememberTurn({ kind: "workflow", question: task, result, answerText: result.answer, at: Date.now() });
  } catch (err) {
    if (err.name === "AbortError") {
      entry.innerHTML = `<h3 class="entry-q">${esc(task)}</h3>
        <p class="entry-a">已停止等待这个任务。任务没有保存进会话。</p>`;
    } else {
      entry.innerHTML = `<h3 class="entry-q">${esc(task)}</h3>
        <p class="entry-a">没能执行：${esc(err.message)}</p>`;
    }
  } finally {
    state.controller = null;
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

/* ── 关联图（Canvas 力导向，零依赖） ─────────────── */

const mapState = {
  nodes: [],
  edges: [],
  dragging: null,
  hover: null,
  raf: null,
  alpha: 1,
};

async function loadGraph() {
  const kbId = document.getElementById("map-kb").value;
  const empty = document.getElementById("map-empty");
  const stat = document.getElementById("map-stat");
  if (!kbId) {
    empty.textContent = "先建一个知识库并导入笔记，这里会出现笔记之间的关联。";
    empty.classList.remove("hidden");
    stat.textContent = "";
    mapState.nodes = [];
    mapState.edges = [];
    drawGraph();
    return;
  }
  const threshold = document.getElementById("map-threshold").value;
  stat.textContent = "计算中…";
  try {
    const data = await api(`/graph?kb_id=${kbId}&min_similarity=${threshold}`);
    if (!data.nodes.length || data.nodes.length < 2) {
      empty.textContent = "这个知识库至少需要两篇文档才能看出关联。";
      empty.classList.remove("hidden");
      stat.textContent = "";
      mapState.nodes = [];
      mapState.edges = [];
      drawGraph();
      return;
    }
    empty.classList.add("hidden");
    prepareGraph(data);
    stat.textContent = `${data.nodes.length} 篇 · ${data.edges.length} 条关联${data.truncated ? "（块数超上限，结果不完整）" : ""}`;
  } catch (err) {
    stat.textContent = "";
    notify(err.message, true);
  }
}

/** 初始化节点位置（圆环分布 + 少量抖动，避免全叠在中心）并启动力模拟。 */
function prepareGraph(data) {
  const count = data.nodes.length;
  const radius = Math.min(200, 60 + count * 8);
  mapState.nodes = data.nodes.map((node, i) => {
    const angle = (i / count) * Math.PI * 2;
    return {
      ...node,
      x: 450 + Math.cos(angle) * radius,
      y: 280 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      r: 8 + Math.min(14, node.chunks * 1.6),
    };
  });
  const index = new Map(mapState.nodes.map((n) => [n.doc_id, n]));
  mapState.edges = data.edges
    .map((e) => ({ ...e, a: index.get(e.source), b: index.get(e.target) }))
    .filter((e) => e.a && e.b);
  mapState.alpha = 1;
  if (mapState.raf) cancelAnimationFrame(mapState.raf);
  tickGraph();
}

function tickGraph() {
  const nodes = mapState.nodes;
  const edges = mapState.edges;
  const alpha = mapState.alpha;

  // 斥力：节点两两推开（文档数一般在几十以内，O(n²) 可接受）
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i];
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let dist = Math.hypot(dx, dy) || 0.01;
      const force = (2200 * alpha) / (dist * dist);
      dx /= dist; dy /= dist;
      a.vx += dx * force; a.vy += dy * force;
      b.vx -= dx * force; b.vy -= dy * force;
    }
  }

  // 弹簧：有边相连的节点互相吸引（权重越大越紧）
  edges.forEach((e) => {
    let dx = e.b.x - e.a.x;
    let dy = e.b.y - e.a.y;
    const dist = Math.hypot(dx, dy) || 0.01;
    const target = 160 - e.weight * 60;
    const force = ((dist - target) / dist) * 0.02 * alpha * (0.4 + e.weight);
    dx *= force; dy *= force;
    e.a.vx += dx; e.a.vy += dy;
    e.b.vx -= dx; e.b.vy -= dy;
  });

  // 向心 + 阻尼 + 边界
  nodes.forEach((n) => {
    n.vx += (450 - n.x) * 0.002 * alpha;
    n.vy += (280 - n.y) * 0.002 * alpha;
    if (n === mapState.dragging) { n.vx = 0; n.vy = 0; return; }
    n.vx *= 0.82;
    n.vy *= 0.82;
    n.x = Math.max(n.r + 30, Math.min(870 - n.r, n.x + n.vx));
    n.y = Math.max(n.r + 24, Math.min(536 - n.r, n.y + n.vy));
  });

  mapState.alpha = Math.max(0.02, alpha * 0.99);
  drawGraph();
  mapState.raf = requestAnimationFrame(tickGraph);
}

function drawGraph() {
  const canvas = document.getElementById("map-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const style = getComputedStyle(document.documentElement);
  const rule = style.getPropertyValue("--rule").trim() || "#D6D3CB";
  const stamp = style.getPropertyValue("--stamp").trim() || "#93262A";
  const ink = style.getPropertyValue("--ink").trim() || "#191A1C";
  const ledger = style.getPropertyValue("--ledger").trim() || "#2B4741";
  const serif = '"Source Han Serif SC", "Songti SC", Georgia, serif';

  // 边
  mapState.edges.forEach((e) => {
    const active = mapState.hover === e.a.doc_id || mapState.hover === e.b.doc_id;
    ctx.strokeStyle = active ? stamp : rule;
    ctx.globalAlpha = active ? 0.9 : 0.25 + e.weight * 0.5;
    ctx.lineWidth = 0.6 + e.weight * 3;
    ctx.beginPath();
    ctx.moveTo(e.a.x, e.a.y);
    ctx.lineTo(e.b.x, e.b.y);
    ctx.stroke();
  });
  ctx.globalAlpha = 1;

  // 节点
  mapState.nodes.forEach((n) => {
    const isHover = mapState.hover === n.doc_id;
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fillStyle = isHover ? stamp : ledger;
    ctx.globalAlpha = isHover ? 1 : 0.82;
    ctx.fill();
    ctx.globalAlpha = 1;

    ctx.font = `${isHover ? "600 " : ""}12.5px ${serif}`;
    ctx.fillStyle = ink;
    ctx.textAlign = "center";
    ctx.fillText(n.title.replace(/\.(md|txt)$/i, "").slice(0, 14), n.x, n.y + n.r + 15);
  });
}

function bindGraphCanvas() {
  const canvas = document.getElementById("map-canvas");
  const hover = document.getElementById("map-hover");

  const pick = (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = (event.clientX - rect.left) * (canvas.width / rect.width);
    const y = (event.clientY - rect.top) * (canvas.height / rect.height);
    return mapState.nodes.find((n) => Math.hypot(n.x - x, n.y - y) <= n.r + 4) || null;
  };

  canvas.addEventListener("mousemove", (event) => {
    const node = pick(event);
    mapState.hover = node ? node.doc_id : null;
    if (node) {
      const links = mapState.edges
        .filter((e) => e.a.doc_id === node.doc_id || e.b.doc_id === node.doc_id)
        .map((e) => (e.a.doc_id === node.doc_id ? e.b.title : e.a.title));
      hover.textContent = `${node.title} · ${node.chunks} 块 · ${node.chars} 字`
        + (links.length ? ` · 关联：${links.slice(0, 4).join("、")}` : " · 暂无关联");
    } else {
      hover.textContent = "";
    }
    drawGraph();
  });

  canvas.addEventListener("mousedown", (event) => {
    const node = pick(event);
    if (!node) return;
    mapState.dragging = node;
    mapState.alpha = Math.max(mapState.alpha, 0.4);
    if (!mapState.raf) tickGraph();
  });

  window.addEventListener("mouseup", () => { mapState.dragging = null; });

  canvas.addEventListener("mousemove", (event) => {
    if (!mapState.dragging) return;
    const rect = canvas.getBoundingClientRect();
    mapState.dragging.x = (event.clientX - rect.left) * (canvas.width / rect.width);
    mapState.dragging.y = (event.clientY - rect.top) * (canvas.height / rect.height);
  });
}

document.getElementById("map-kb").addEventListener("change", loadGraph);
document.getElementById("map-threshold").addEventListener("change", loadGraph);
document.getElementById("map-reload").addEventListener("click", loadGraph);

/* ── 会话渲染（含历史记录回看） ───────────────────── */

function renderConversation() {
  const flow = document.getElementById("ask-flow");
  const conv = activeConversation();
  flow.innerHTML = "";
  if (!conv || !conv.turns.length) {
    flow.innerHTML = '<p class="empty">这个会话还没有内容。开始提问吧。</p>';
    return;
  }
  conv.turns.forEach((turn) => {
    const entry = document.createElement("article");
    entry.className = "entry";
    if (turn.kind === "workflow") {
      entry.innerHTML = renderTrace(turn.question, turn.result);
      bindCitations(entry, (turn.result && turn.result.citations) || []);
    } else {
      entry.innerHTML = renderAnswerEntry(turn.question, turn.answer);
      bindCitations(entry, (turn.answer && turn.answer.citations) || []);
    }
    flow.appendChild(entry);
  });
}

/* ── 启动 ─────────────────────────────────────────── */

(async function start() {
  try {
    bindGraphCanvas();
    ensureConversation();
    renderConvSelect();
    renderConversation();
    await loadKbs();
    await loadDocs();
  } catch (err) {
    notify("加载失败：" + err.message, true);
  }
})();
