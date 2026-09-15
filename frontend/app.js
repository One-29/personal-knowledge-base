/**
 * KnowBase 前端（原生 JS，无构建步骤）。
 *
 * 设计约束：
 * - 前端只渲染后端契约对象，不重复实现排序、拒答与引用校验；
 * - 「核对」是核心动作，桌面端使用常驻原文栏，窄屏使用按需抽屉；
 * - 问答与工作流分别维护请求控制器和运行状态，允许两类任务同时在途；
 * - 模型文本先转义再做轻量格式化，不执行回答中的 HTML。
 *
 * 会话记录保存在浏览器本地（localStorage），并把最近几轮随请求回传给后端做
 * 追问改写——后端因此保持无状态：刷新页面或重启服务都不会丢掉对话上下文。
 */

const API = "/api/v1";
const CONV_KEY = "kb_conversations";
const ACTIVE_KEY = "kb_active_conv";
const EVIDENCE_KEY = "kb_evidence_open";
const HISTORY_TURNS = 6;          // 回传给后端的最近轮数（用于指代句改写）

const VIEW_META = {
  ask: {
    kicker: "ASK YOUR LIBRARY",
    title: "知识问答",
    description: "从自己的资料中获得有出处、可核对的回答。",
  },
  trace: {
    kicker: "MULTI-STEP RESEARCH",
    title: "综合工作流",
    description: "将跨文档任务拆解为清晰步骤，并逐项说明材料是否充分。",
  },
  map: {
    kicker: "KNOWLEDGE CONNECTIONS",
    title: "知识关联图",
    description: "通过语义相似度观察文档之间的关系与主题聚类。",
  },
  docs: {
    kicker: "DOCUMENT LIBRARY",
    title: "文档管理",
    description: "查看处理状态、更新原文，并确认哪些资料已经可以检索。",
  },
  kbs: {
    kicker: "LIBRARY SPACES",
    title: "知识库",
    description: "按课程、项目或主题组织彼此独立的资料集合。",
  },
};

const DOC_STATUS = {
  ready: "可以检索",
  pending: "等待处理",
  processing: "处理中",
  failed: "处理失败",
};

const state = {
  kbs: [],
  docs: [],
  docsKbId: null,
  askKbId: null,
  activeView: "ask",
  docsPoll: null,
  activityTimers: { ask: null, trace: null },
  activityResetTimers: { ask: null, trace: null },
  // 问答与工作流各自的请求控制器：两者互不影响，可以同时进行
  controllers: { ask: null, trace: null },
};

/* ── 基础工具 ─────────────────────────────────────── */

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `请求失败（${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(API + path, {
      headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      ...options,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "无法连接本机服务，请确认启动窗口仍在运行");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { }
    throw new ApiError(resp.status, detail);
  }
  return resp.status === 204 ? null : resp.json();
}

function notify(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  document.getElementById("sr-status").textContent = message;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), isError ? 5200 : 3200);
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderInline(content, includeCitations = true) {
  const held = [];
  const hold = (html) => `\uE000${held.push(html) - 1}\uE001`;
  let source = String(content ?? "");
  source = source.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${esc(code)}</code>`));
  source = source.replace(/\$([^$\n]+)\$/g, (_, math) =>
    hold(`<span class="math-inline">${esc(math)}</span>`));
  let safe = esc(source)
    .replace(/(?:\*{2})([^*]+)(?:\*{2})/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>");
  if (includeCitations) {
    safe = safe.replace(/\[(\d+)\]/g, (_, n) =>
      `<span class="cite" data-index="${n}" role="button" tabindex="0" aria-label="打开引用 ${n}">[${n}]</span>`);
  }
  return safe.replace(/\uE000(\d+)\uE001/g, (_, index) => held[Number(index)] || "");
}

function renderRichText(content, includeCitations = true) {
  const lines = String(content ?? "").replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  const special = (line) => (
    /^```/.test(line)
    || /^\s*\$\$/.test(line)
    || /^#{1,3}\s+/.test(line)
    || /^\s*[-*]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || /^\s*>\s?/.test(line)
  );

  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    if (/^```/.test(line)) {
      const language = line.slice(3).trim();
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i += 1;
      html.push(`<pre${language ? ` data-language="${esc(language)}"` : ""}><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }

    if (/^\s*\$\$/.test(line)) {
      const singleLineMath = line.trim();
      if (singleLineMath.length > 4 && singleLineMath.endsWith("$$")) {
        html.push(`<span class="math-block">${esc(singleLineMath.slice(2, -2).trim())}</span>`);
        i += 1;
        continue;
      }
      const math = [line.replace(/^\s*\$\$/, "")];
      i += 1;
      while (i < lines.length && !/\$\$\s*$/.test(lines[i])) math.push(lines[i++]);
      if (i < lines.length) {
        math.push(lines[i].replace(/\$\$\s*$/, ""));
        i += 1;
      }
      html.push(`<span class="math-block">${esc(math.join("\n").trim())}</span>`);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      html.push(`<h${level}>${renderInline(heading[2], includeCitations)}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*[-*]\s+/, ""), includeCitations)}</li>`);
        i += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""), includeCitations)}</li>`);
        i += 1;
      }
      html.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote>${quoted.map((part) => renderInline(part, includeCitations)).join("<br>")}</blockquote>`);
      continue;
    }

    const paragraph = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !special(lines[i])) paragraph.push(lines[i++]);
    html.push(`<p>${paragraph.map((part) => renderInline(part, includeCitations)).join("<br>")}</p>`);
  }

  return html.join("") || "<p>没有返回可显示的内容。</p>";
}

function withCitations(content) {
  return renderRichText(content, true);
}

function sourceSummary(citations) {
  const unique = [];
  const seen = new Set();
  (citations || []).forEach((citation) => {
    if (seen.has(citation.chunk_id)) return;
    seen.add(citation.chunk_id);
    unique.push(citation);
  });
  if (!unique.length) return "";
  return `<div class="source-summary"><span>引用来源</span>${unique.map((citation) => `
    <button type="button" class="source-pill" data-chunk="${citation.chunk_id}" data-index="${citation.index}">
      <b>[${citation.index}]</b>${esc(citation.doc_title)}
    </button>`).join("")}</div>`;
}

function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function formatCount(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
}

function friendlyError(error, action = "完成操作") {
  if (error instanceof ApiError) {
    if ([400, 404, 409, 413, 422].includes(error.status) || error.status === 0) return error.detail;
    return `${action}失败：${error.detail}`;
  }
  return `${action}失败：${error.message || "发生未知错误"}`;
}

/* ── 会话（本地保存） ─────────────────────────────── */

function loadConversations() {
  try {
    const stored = JSON.parse(localStorage.getItem(CONV_KEY));
    return Array.isArray(stored) ? stored : [];
  } catch (_) {
    return [];
  }
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

function rememberTurn(conversationId, turn) {
  const list = loadConversations();
  const index = list.findIndex((c) => c.id === conversationId);
  // 请求期间用户可能切换或删除会话。只写回发起请求时的会话；
  // 目标已删除时直接放弃保存，避免把旧结果写进当前会话或复活已删除会话。
  if (index < 0) return false;

  const conv = list[index];
  conv.turns.push(turn);
  conv.at = Date.now();
  if (!conv.title) conv.title = turn.question.slice(0, 24);
  list[index] = conv;
  persistConversations(list);
  renderConvSelect();
  flashSaved();
  return true;
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

function activateView(viewName, updateHash = true) {
  const name = VIEW_META[viewName] ? viewName : "ask";
  state.activeView = name;
  document.querySelectorAll(".views button").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("on", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((view) => view.classList.remove("on"));
  document.getElementById("view-" + name).classList.add("on");

  const meta = VIEW_META[name];
  document.getElementById("view-kicker").textContent = meta.kicker;
  document.getElementById("view-title").textContent = meta.title;
  document.getElementById("view-description").textContent = meta.description;
  document.title = `${meta.title} · KnowBase`;

  if (updateHash && window.location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
  if (name !== "docs") {
    clearTimeout(state.docsPoll);
    state.docsPoll = null;
  }
  if (name === "docs") loadDocs();
  if (name === "map") loadGraph();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.querySelectorAll(".views button").forEach((button) => {
  button.addEventListener("click", () => activateView(button.dataset.view));
});

window.addEventListener("hashchange", () => {
  activateView(window.location.hash.slice(1), false);
});

/* ── 页边注：核对原文（可同时钉住多条） ───────────── */

const marginItems = new Map();

function setEvidenceOpen(open, persist = true) {
  document.getElementById("app-shell").classList.toggle("evidence-collapsed", !open);
  document.getElementById("margin-open").setAttribute("aria-expanded", String(open));
  if (persist) localStorage.setItem(EVIDENCE_KEY, open ? "1" : "0");
}

function updateEvidenceState() {
  const count = marginItems.size;
  document.getElementById("margin-count").textContent = String(count);
  document.getElementById("margin-badge").textContent = String(count);
  document.getElementById("margin-hint").classList.toggle("hidden", count > 0);
  document.getElementById("margin-clear").disabled = count === 0;
}

function removeMarginItem(key, chunkId = null) {
  const item = marginItems.get(key);
  if (!item) return;
  item.remove();
  marginItems.delete(key);
  if (chunkId !== null) {
    document.querySelectorAll(`.cite.on[data-chunk="${chunkId}"], .source-pill[data-chunk="${chunkId}"]`)
      .forEach((element) => element.classList.remove("on"));
  }
  updateEvidenceState();
}

async function pinToMargin(chunkId, displayIndex = null) {
  const key = `chunk-${chunkId}`;
  try {
    setEvidenceOpen(true);
    const existing = marginItems.get(key);
    if (existing) {
      existing.scrollIntoView({ behavior: "smooth", block: "nearest" });
      existing.classList.remove("margin-item-enter");
      void existing.offsetWidth;
      existing.classList.add("margin-item-enter");
      markActiveCite(chunkId);
      return;
    }

    const detail = await api(`/citations/${chunkId}`);
    const item = document.createElement("article");
    item.className = "margin-item margin-item-enter";
    item.dataset.chunk = String(chunkId);
    item.innerHTML = `
      <div class="item-head">
        <div class="item-source">
          ${displayIndex ? `<span class="item-index">[${esc(displayIndex)}]</span>` : ""}
          <p class="item-src">${esc(detail.doc_title)}</p>
          <span class="item-range">字符位置 ${detail.char_start}–${detail.char_end}</span>
        </div>
        <button type="button" class="text-action" data-unpin>移除</button>
      </div>
      <blockquote>${esc(detail.chunk_text)}</blockquote>`;
    item.querySelector("[data-unpin]").addEventListener("click", () => removeMarginItem(key, chunkId));
    document.getElementById("margin-list").appendChild(item);
    marginItems.set(key, item);
    updateEvidenceState();
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    markActiveCite(chunkId);
  } catch (error) {
    notify(friendlyError(error, "读取引用"), true);
  }
}

async function openDocument(docId, title = "") {
  const key = `document-${docId}`;
  try {
    setEvidenceOpen(true);
    const existing = marginItems.get(key);
    if (existing) {
      existing.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    const detail = await api(`/documents/${docId}/content`);
    const item = document.createElement("article");
    item.className = "margin-item document-preview margin-item-enter";
    item.innerHTML = `
      <div class="item-head">
        <div class="item-source">
          <p class="item-src">${esc(detail.title || title)}</p>
          <span class="item-range">完整原文预览</span>
        </div>
        <button type="button" class="text-action" data-unpin>移除</button>
      </div>
      <blockquote>${esc(detail.content)}</blockquote>`;
    item.querySelector("[data-unpin]").addEventListener("click", () => removeMarginItem(key));
    document.getElementById("margin-list").appendChild(item);
    marginItems.set(key, item);
    updateEvidenceState();
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    notify(friendlyError(error, "读取原文"), true);
  }
}

function markActiveCite(chunkId) {
  document.querySelectorAll(".cite.on, .source-pill.on").forEach((element) => element.classList.remove("on"));
  document.querySelectorAll(`.cite[data-chunk="${chunkId}"], .source-pill[data-chunk="${chunkId}"]`)
    .forEach((element) => element.classList.add("on"));
}

document.getElementById("margin-clear").addEventListener("click", () => {
  marginItems.clear();
  document.getElementById("margin-list").innerHTML = "";
  document.querySelectorAll(".cite.on, .source-pill.on").forEach((element) => element.classList.remove("on"));
  updateEvidenceState();
  notify("已清空核对区");
});

document.getElementById("margin-open").addEventListener("click", () => {
  const collapsed = document.getElementById("app-shell").classList.contains("evidence-collapsed");
  setEvidenceOpen(collapsed);
});

document.getElementById("margin-close").addEventListener("click", () => setEvidenceOpen(false));

/* 宽度可拖（存 localStorage，下次打开保持） */
(function initResizer() {
  const resizer = document.getElementById("margin-resizer");
  const saved = localStorage.getItem("kb_margin_width");
  if (saved) {
    const width = Math.max(300, Math.min(520, Number.parseInt(saved, 10) || 356));
    document.documentElement.style.setProperty("--margin-width", width + "px");
  }

  let dragging = false;
  resizer.addEventListener("mousedown", () => { dragging = true; resizer.classList.add("on"); });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("on");
    localStorage.setItem("kb_margin_width", getComputedStyle(document.documentElement)
      .getPropertyValue("--margin-width").trim());
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const width = Math.max(300, Math.min(520, window.innerWidth - event.clientX));
    document.documentElement.style.setProperty("--margin-width", width + "px");
  });
})();

function bindCitations(scope, citations) {
  scope.querySelectorAll(".cite, .source-pill").forEach((el) => {
    const open = () => {
      if (el.dataset.chunk) return pinToMargin(Number(el.dataset.chunk), el.dataset.index || null);
      const hit = (citations || []).find((c) => String(c.index) === el.dataset.index);
      if (hit) {
        el.dataset.chunk = String(hit.chunk_id);
        pinToMargin(hit.chunk_id, hit.index);
      }
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
  document.getElementById("kb-count").textContent = `${state.kbs.length} 个`;
  if (!state.kbs.length) {
    list.innerHTML = `<li class="empty-state">
      <h2>还没有知识库</h2>
      <p>在左侧填写名称和说明，创建第一个资料空间。</p>
    </li>`;
    return;
  }
  list.innerHTML = state.kbs.map((kb) => `
    <li>
      <div class="kb-glyph" aria-hidden="true">${esc(kb.name.slice(0, 1).toUpperCase())}</div>
      <div class="kb-info">
        <div class="kb-name">${esc(kb.name)}</div>
        <div class="kb-meta">${esc(kb.description || "暂未填写说明")} · ${formatCount(kb.doc_count)} 篇文档</div>
      </div>
      <div class="kb-actions">
        <button type="button" class="kb-open" data-open-kb="${kb.id}">管理文档</button>
        <button type="button" class="kb-delete" data-drop="${kb.id}">删除</button>
      </div>
    </li>`).join("");

  list.querySelectorAll("[data-open-kb]").forEach((button) => {
    button.addEventListener("click", () => {
      state.docsKbId = Number(button.dataset.openKb);
      renderKbSelects();
      activateView("docs");
    });
  });

  list.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("删除这个知识库？它下面的文档和索引会一起清理。")) return;
      try {
        await api(`/kbs/${button.dataset.drop}`, { method: "DELETE" });
        notify("知识库已删除");
        await loadKbs();
        if (state.activeView === "docs") await loadDocs();
      } catch (error) {
        notify(friendlyError(error, "删除知识库"), true);
      }
    });
  });
}

function renderKbSelects() {
  const askSelect = document.getElementById("ask-kb");
  const traceSelect = document.getElementById("trace-kb");
  const docsSelect = document.getElementById("docs-kb");
  const mapSelect = document.getElementById("map-kb");
  const askKeep = askSelect.value || (state.askKbId ? String(state.askKbId) : "");
  const traceKeep = traceSelect.value;
  const docsKeep = state.docsKbId ? String(state.docsKbId) : docsSelect.value;
  const mapKeep = mapSelect.value;

  const html = ['<option value="">全部知识库</option>']
    .concat(state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`)).join("");
  askSelect.innerHTML = html;
  traceSelect.innerHTML = html;
  if ([...askSelect.options].some((option) => option.value === askKeep)) askSelect.value = askKeep;
  if ([...traceSelect.options].some((option) => option.value === traceKeep)) traceSelect.value = traceKeep;
  state.askKbId = askSelect.value ? Number(askSelect.value) : null;

  const onlyKbs = state.kbs.length
    ? state.kbs.map((kb) => `<option value="${kb.id}">${esc(kb.name)}</option>`).join("")
    : '<option value="">暂无知识库</option>';
  docsSelect.innerHTML = onlyKbs;
  mapSelect.innerHTML = onlyKbs;
  docsSelect.disabled = !state.kbs.length;
  mapSelect.disabled = !state.kbs.length;
  if ([...docsSelect.options].some((option) => option.value === docsKeep)) docsSelect.value = docsKeep;
  else if (state.kbs.length) docsSelect.value = String(state.kbs[0].id);
  if ([...mapSelect.options].some((option) => option.value === mapKeep)) mapSelect.value = mapKeep;
  else if (state.kbs.length) mapSelect.value = String(state.kbs[0].id);
  state.docsKbId = docsSelect.value ? Number(docsSelect.value) : null;
}

document.getElementById("kb-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = document.getElementById("kb-name").value.trim();
  const description = document.getElementById("kb-desc").value.trim();
  if (!name) return;
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.textContent = "正在创建…";
  try {
    await api("/kbs", { method: "POST", body: JSON.stringify({ name, description: description || null }) });
    event.target.reset();
    notify(`已新建知识库「${name}」`);
    await loadKbs();
  } catch (error) {
    notify(friendlyError(error, "创建知识库"), true);
  } finally {
    submit.disabled = false;
    submit.textContent = "创建知识库";
  }
});

/* ── 文档清单 ─────────────────────────────────────── */

function updateDocStats(docs) {
  const total = docs.length;
  const ready = docs.filter((doc) => doc.status === "ready").length;
  const processing = docs.filter((doc) => ["pending", "processing"].includes(doc.status)).length;
  const failed = docs.filter((doc) => doc.status === "failed" || doc.last_error_message).length;
  document.getElementById("docs-total").textContent = formatCount(total);
  document.getElementById("docs-ready").textContent = formatCount(ready);
  document.getElementById("docs-processing").textContent = formatCount(processing);
  document.getElementById("docs-failed").textContent = formatCount(failed);
}

function showDocsEmpty(title, description, action = "upload") {
  const empty = document.getElementById("docs-empty");
  empty.querySelector("h2").textContent = title;
  empty.querySelector("p").textContent = description;
  const button = document.getElementById("docs-empty-upload");
  button.dataset.action = action;
  button.textContent = action === "reset" ? "清除筛选" : action === "create" ? "新建知识库" : "选择文档";
  empty.classList.remove("hidden");
  document.getElementById("docs-table-shell").classList.add("hidden");
}

function renderDocs() {
  const query = document.getElementById("docs-search").value.trim().toLocaleLowerCase("zh-CN");
  const status = document.getElementById("docs-status-filter").value;
  const filtered = state.docs.filter((doc) => (
    (!query || doc.title.toLocaleLowerCase("zh-CN").includes(query))
    && (!status || doc.status === status)
  ));
  const tbody = document.querySelector("#docs-table tbody");
  updateDocStats(state.docs);

  if (!state.docs.length) {
    tbody.innerHTML = "";
    showDocsEmpty(
      "这个知识库还没有文档",
      "上传 UTF-8 编码的 Markdown 或纯文本文件，系统会自动切块并建立索引。",
    );
    return;
  }
  if (!filtered.length) {
    tbody.innerHTML = "";
    showDocsEmpty("没有符合条件的文档", "换一个标题关键词或状态筛选后再查看。", "reset");
    return;
  }

  document.getElementById("docs-empty").classList.add("hidden");
  document.getElementById("docs-table-shell").classList.remove("hidden");
  tbody.innerHTML = filtered.map((doc) => {
    const extension = doc.title.toLowerCase().endsWith(".md") ? "MD" : "TXT";
    const statusText = DOC_STATUS[doc.status] || doc.status;
    return `
      <tr>
        <td>
          <div class="doc-title">
            <span class="doc-icon">${extension}</span>
            <span title="${esc(doc.title)}">${esc(doc.title)}</span>
          </div>
        </td>
        <td><span class="status-badge ${esc(doc.status)}">${esc(statusText)}</span></td>
        <td class="num">${formatCount(doc.chunk_count)}</td>
        <td class="num">${formatCount(doc.char_count)}</td>
        <td class="num">${stamp(doc.updated_at)}</td>
        <td class="acts">
          <button type="button" class="table-action" data-open-doc="${doc.id}" data-title="${esc(doc.title)}">查看</button>
          <button type="button" class="table-action" data-again="${doc.id}">重传</button>
          <button type="button" class="table-action danger" data-remove="${doc.id}">删除</button>
        </td>
      </tr>
      ${doc.last_error_message ? `<tr class="error-row"><td colspan="6"><span class="hint-inline">${
        doc.status === "ready" ? "当前旧索引仍可使用；上次更新未完成" : esc(doc.last_error_code || "处理失败")
      }：${esc(doc.last_error_message)}</span></td></tr>` : ""}`;
  }).join("");

  tbody.querySelectorAll("[data-open-doc]").forEach((button) => {
    button.addEventListener("click", () => openDocument(Number(button.dataset.openDoc), button.dataset.title));
  });
  tbody.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("删除这篇文档？它的索引和原文会一并清理。")) return;
      try {
        await api(`/documents/${button.dataset.remove}`, { method: "DELETE" });
        notify("文档已删除");
        await loadDocs();
        await loadKbs();
      } catch (error) {
        notify(friendlyError(error, "删除文档"), true);
      }
    });
  });
  tbody.querySelectorAll("[data-again]").forEach((button) => {
    button.addEventListener("click", () => pickAndReupload(Number(button.dataset.again)));
  });
}

async function loadDocs({ quiet = false } = {}) {
  clearTimeout(state.docsPoll);
  state.docsPoll = null;
  const loading = document.getElementById("docs-loading");
  if (!state.docsKbId) {
    state.docs = [];
    updateDocStats([]);
    showDocsEmpty("先创建一个知识库", "知识库用于组织文档，也是问答和关联图的检索范围。", "create");
    return;
  }
  const requestedKb = state.docsKbId;
  if (!quiet) {
    loading.classList.remove("hidden");
    document.getElementById("docs-table-shell").classList.add("hidden");
    document.getElementById("docs-empty").classList.add("hidden");
  }
  try {
    const docs = await api(`/kbs/${requestedKb}/documents`);
    if (state.docsKbId !== requestedKb) return;
    state.docs = docs;
    renderDocs();
    if (state.activeView === "docs" && docs.some((doc) => ["pending", "processing"].includes(doc.status))) {
      state.docsPoll = setTimeout(() => loadDocs({ quiet: true }), 2200);
    }
  } catch (error) {
    if (state.docsKbId !== requestedKb) return;
    state.docs = [];
    updateDocStats([]);
    showDocsEmpty("文档列表读取失败", friendlyError(error, "读取文档"), "reset");
    notify(friendlyError(error, "读取文档"), true);
  } finally {
    loading.classList.add("hidden");
  }
}

function setUploadBusy(busy) {
  const label = document.getElementById("docs-upload-label");
  label.classList.toggle("busy", busy);
  label.querySelector("span").textContent = busy ? "正在上传…" : "上传文档";
  document.getElementById("docs-file").disabled = busy;
}

async function uploadDocument(file) {
  if (!state.docsKbId) {
    notify("请先选择一个知识库", true);
    return;
  }
  const form = new FormData();
  form.append("file", file);
  setUploadBusy(true);
  try {
    await api(`/kbs/${state.docsKbId}/documents`, { method: "POST", body: form });
    notify(`「${file.name}」已上传，正在建立索引`);
    await loadDocs();
    await loadKbs();
  } catch (error) {
    notify(friendlyError(error, "上传文档"), true);
  } finally {
    setUploadBusy(false);
  }
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
      notify(result.content_changed ? "内容已更新，正在重建索引" : "内容没有变化，已跳过重建");
      await loadDocs();
    } catch (error) {
      notify(friendlyError(error, "重传文档"), true);
    }
  });
  picker.click();
}

document.getElementById("docs-kb").addEventListener("change", (event) => {
  state.docsKbId = event.target.value ? Number(event.target.value) : null;
  loadDocs();
});
document.getElementById("docs-refresh").addEventListener("click", () => loadDocs());
document.getElementById("docs-search").addEventListener("input", renderDocs);
document.getElementById("docs-status-filter").addEventListener("change", renderDocs);
document.getElementById("docs-empty-upload").addEventListener("click", (event) => {
  if (event.currentTarget.dataset.action === "reset") {
    document.getElementById("docs-search").value = "";
    document.getElementById("docs-status-filter").value = "";
    renderDocs();
  } else if (event.currentTarget.dataset.action === "create") {
    activateView("kbs");
    document.getElementById("kb-name").focus();
  } else {
    document.getElementById("docs-file").click();
  }
});
document.getElementById("docs-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (file) await uploadDocument(file);
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
  if (state.controllers.ask) {
    state.controllers.ask.abort();
    notify("浏览器已停止等待；服务端可能仍在完成已经发出的模型调用");
  }
});
document.getElementById("trace-stop").addEventListener("click", () => {
  if (state.controllers.trace) {
    state.controllers.trace.abort();
    notify("浏览器已停止等待；服务端可能仍在完成已经发出的模型调用");
  }
});

function setBusy(scope, busy, outcome = "idle") {
  const isAsk = scope === "ask";
  const stop = document.getElementById(isAsk ? "ask-stop" : "trace-stop");
  const send = document.getElementById(isAsk ? "ask-send" : "trace-send");
  const navBusy = document.getElementById(isAsk ? "nav-ask-busy" : "nav-trace-busy");
  const activity = document.getElementById(isAsk ? "activity-ask" : "activity-trace");
  const activityText = activity.querySelector("strong");
  stop.classList.toggle("hidden", !busy);
  send.disabled = busy;
  navBusy.classList.toggle("hidden", !busy);
  clearInterval(state.activityTimers[scope]);
  clearTimeout(state.activityResetTimers[scope]);

  if (busy) {
    const startedAt = Date.now();
    activity.dataset.status = "busy";
    const update = () => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      activityText.textContent = `运行中 ${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    };
    update();
    state.activityTimers[scope] = setInterval(update, 1000);
    return;
  }

  const labels = { done: "刚刚完成", error: "需要查看", stopped: "已停止等待", idle: "空闲" };
  activity.dataset.status = outcome;
  activityText.textContent = labels[outcome] || labels.idle;
  if (outcome !== "idle") {
    state.activityResetTimers[scope] = setTimeout(() => {
      if (!state.controllers[scope]) {
        activity.dataset.status = "idle";
        activityText.textContent = "空闲";
      }
    }, 5000);
  }
}

function focusLatest(entry) {
  requestAnimationFrame(() => entry.scrollIntoView({ behavior: "smooth", block: "end" }));
}

function questionHeader(question) {
  return `<div class="entry-question"><h3 class="entry-q">${esc(question)}</h3></div>`;
}

function mountRunState(entry, question, scope) {
  const isAsk = scope === "ask";
  const phases = isAsk
    ? ["正在提交问题并检索知识库…", "正在等待检索与回答生成…", "仍在处理较长的回答，请稍候…"]
    : ["正在规划任务并准备检索…", "正在逐步检索并整理材料…", "正在等待各步骤完成并汇总结论…"];
  const startedAt = Date.now();
  const update = () => {
    if (!entry.isConnected) return;
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    const phase = seconds >= 12 ? phases[2] : seconds >= 3 ? phases[1] : phases[0];
    const phaseElement = entry.querySelector("[data-run-phase]");
    const timeElement = entry.querySelector("[data-run-time]");
    if (phaseElement) phaseElement.textContent = phase;
    if (timeElement) timeElement.textContent = `${seconds} 秒`;
  };
  entry.innerHTML = `${questionHeader(question)}
    <div class="run-state">
      <div class="run-state-head">
        <span class="spinner"></span>
        <strong data-run-phase>${phases[0]}</strong>
        <time data-run-time>0 秒</time>
      </div>
      <p>${isAsk ? "完成后会显示引用来源，期间可以切换到工作流。" : "完成后一次返回分步结果；期间可以切换到问答。"}</p>
      <div class="run-progress"></div>
    </div>`;
  const timer = setInterval(update, 1000);
  return () => clearInterval(timer);
}

function wireComposer(inputId, formId) {
  const input = document.getElementById(inputId);
  const resize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 148) + "px";
  };
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      document.getElementById(formId).requestSubmit();
    }
  });
  input._resize = resize;
}

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById("ask-input");
    input.value = button.dataset.prompt;
    input._resize();
    input.focus();
  });
});

document.getElementById("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.controllers.ask) { notify("上一个提问还在进行中", true); return; }

  const input = document.getElementById("ask-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  input._resize();

  const conv = ensureConversation();
  const conversationId = conv.id;
  const flow = document.getElementById("ask-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  document.getElementById("ask-welcome").classList.add("hidden");
  flow.appendChild(entry);
  const stopRunState = mountRunState(entry, question, "ask");
  focusLatest(entry);

  const controller = new AbortController();
  state.controllers.ask = controller;
  setBusy("ask", true);
  const startedAt = Date.now();
  let outcome = "done";
  try {
    const answer = await api("/ask", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        question,
        kb_id: state.askKbId,
        history: historyForRequest(conv),
      }),
    });
    const durationMs = Date.now() - startedAt;
    entry.innerHTML = renderAnswerEntry(question, answer, durationMs);
    bindCitations(entry, answer.citations);
    const saved = rememberTurn(conversationId, {
      kind: "ask", question, answer, answerText: answer.content, durationMs, at: Date.now(),
    });
    if (!saved) {
      notify("发起提问的会话已被删除，本次结果未保存", true);
    } else if (activeConversation()?.id === conversationId && !entry.isConnected) {
      // 用户曾切走又切回原会话时，旧的在途节点已经被重绘移除；
      // 从刚保存的记录重绘，确保答案立即可见，无需再次切换或刷新。
      renderConversation();
    }
    if (entry.isConnected) focusLatest(entry);
  } catch (error) {
    if (error.name === "AbortError") {
      outcome = "stopped";
      entry.innerHTML = `${questionHeader(question)}
        <div class="inline-state stopped"><b>已停止等待这次回答</b>问题没有保存到会话。已经发出的服务端模型调用可能仍会执行完。</div>`;
    } else {
      outcome = "error";
      entry.innerHTML = `${questionHeader(question)}
        <div class="inline-state error"><b>没有取得回答</b>${esc(friendlyError(error, "问答"))}。你可以检查服务后重新发送。</div>`;
    }
    if (!input.value) {
      input.value = question;
      input._resize();
    }
  } finally {
    stopRunState();
    state.controllers.ask = null;
    setBusy("ask", false, outcome);
  }
});

function renderAnswerEntry(question, answer, durationMs = null) {
  const citations = answer.citations || [];
  const refusal = answer.refused
    ? `<span class="refused">材料不足 · ${esc(answer.refusal_reason || "未能形成可靠回答")}</span>` : "";
  const rewrote = answer.search_query && answer.search_query !== question
    ? `<p class="rewrote">按上文改写后检索：<code>${esc(answer.search_query)}</code></p>` : "";
  const duration = durationMs === null ? "" : `<span class="meta-dot">${Math.max(1, Math.round(durationMs / 1000))} 秒</span>`;
  return `${questionHeader(question)}
    <div class="answer-shell">
      <div class="answer-meta">
        <span class="answer-label">${answer.refused ? "检索结果" : "KNOWBASE 回答"}</span>
        <span class="meta-dot">${citations.length} 条引用</span>
        ${duration}
        ${refusal}
      </div>
      ${rewrote}
      <div class="entry-a rich-text">${withCitations(answer.content)}</div>
      ${sourceSummary(citations)}
    </div>`;
}

/* ── 工作流 ───────────────────────────────────────── */

document.getElementById("trace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.controllers.trace) { notify("上一个任务还在进行中", true); return; }

  const input = document.getElementById("trace-input");
  const task = input.value.trim();
  if (!task) return;
  input.value = "";
  input._resize();

  const conv = ensureConversation();
  const conversationId = conv.id;
  const flow = document.getElementById("trace-flow");
  const entry = document.createElement("article");
  entry.className = "entry";
  document.getElementById("trace-welcome").classList.add("hidden");
  flow.appendChild(entry);
  const stopRunState = mountRunState(entry, task, "trace");
  focusLatest(entry);

  const kbValue = document.getElementById("trace-kb").value;
  const controller = new AbortController();
  state.controllers.trace = controller;
  setBusy("trace", true);
  const startedAt = Date.now();
  let outcome = "done";
  try {
    const result = await api("/workflow", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({ task, kb_id: kbValue ? Number(kbValue) : null }),
    });
    const durationMs = Date.now() - startedAt;
    entry.innerHTML = renderTrace(task, result, durationMs);
    bindCitations(entry, result.citations);
    const saved = rememberTurn(conversationId, {
      kind: "workflow", question: task, result, answerText: result.answer, durationMs, at: Date.now(),
    });
    if (!saved) notify("发起任务的会话已被删除，本次结果未保存", true);
    focusLatest(entry);
  } catch (error) {
    if (error.name === "AbortError") {
      outcome = "stopped";
      entry.innerHTML = `${questionHeader(task)}
        <div class="inline-state stopped"><b>已停止等待这个工作流</b>任务没有保存到会话。已经发出的服务端模型调用可能仍会执行完。</div>`;
    } else {
      outcome = "error";
      entry.innerHTML = `${questionHeader(task)}
        <div class="inline-state error"><b>工作流没有完成</b>${esc(friendlyError(error, "执行工作流"))}。你可以检查服务后重新执行。</div>`;
    }
    if (!input.value) {
      input.value = task;
      input._resize();
    }
  } finally {
    stopRunState();
    state.controllers.trace = null;
    setBusy("trace", false, outcome);
  }
});

const STATE_WORD = { answered: "已作答", insufficient: "没有材料", error: "未完成" };

function renderTrace(task, result, durationMs = null) {
  const stepsData = result.steps || [];
  const steps = stepsData.map((step) => {
    const body = step.status === "answered"
      ? `<div class="trace-body rich-text">${withCitations(step.conclusion || "")}</div>`
      : `<p class="trace-note">${esc(step.note || "")}</p>`;
    const cites = (step.citations || []).length
      ? `<div class="trace-cites">${step.citations.map((citation) =>
          `<span class="cite" data-chunk="${citation.chunk_id}" data-index="${citation.index}" role="button" tabindex="0" aria-label="打开引用 ${citation.index}">[${citation.index}]</span>`
        ).join("")}</div>` : "";
    return `<li>
        <div class="trace-card">
          <div class="trace-head">${esc(step.goal)}
            <span class="trace-state ${step.status}">${STATE_WORD[step.status] || step.status}</span>
          </div>
          <p class="trace-query">检索语句：${esc(step.query)}</p>
          ${body}${cites}
        </div>
      </li>`;
  }).join("");

  const answered = stepsData.filter((step) => step.status === "answered").length;
  const duration = durationMs === null ? "" : ` · ${Math.max(1, Math.round(durationMs / 1000))} 秒`;
  return `${questionHeader(task)}
    <div class="answer-shell">
      <div class="answer-meta">
        <span class="answer-label">WORKFLOW 结果</span>
        <span class="meta-dot">${answered}/${stepsData.length} 步完成${duration}</span>
      </div>
      <ol class="trace-list">${steps}</ol>
    </div>
    <div class="summary">
      <div class="summary-head"><h3>综合结论</h3><span>${(result.citations || []).length} 条引用</span></div>
      <div class="entry-a rich-text">${withCitations(result.answer)}</div>
      ${sourceSummary(result.citations || [])}
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
  requestId: 0,
};

async function loadGraph() {
  const kbId = document.getElementById("map-kb").value;
  const empty = document.getElementById("map-empty");
  const stat = document.getElementById("map-stat");
  const loading = document.getElementById("map-loading");
  const requestId = ++mapState.requestId;
  if (!kbId) {
    empty.textContent = "先建一个知识库并导入笔记，这里会出现笔记之间的关联。";
    empty.classList.remove("hidden");
    stat.textContent = "暂无可计算的数据";
    mapState.nodes = [];
    mapState.edges = [];
    drawGraph();
    return;
  }
  const threshold = document.getElementById("map-threshold").value;
  loading.classList.remove("hidden");
  empty.classList.add("hidden");
  stat.textContent = `正在按 ${threshold} 阈值计算…`;
  try {
    const data = await api(`/graph?kb_id=${kbId}&min_similarity=${threshold}`);
    if (requestId !== mapState.requestId) return;
    if (!data.nodes.length || data.nodes.length < 2) {
      empty.textContent = "这个知识库至少需要两篇已完成处理的文档，才能展示语义关联。";
      empty.classList.remove("hidden");
      stat.textContent = `${data.nodes.length} 篇文档 · 暂无关联图`;
      mapState.nodes = [];
      mapState.edges = [];
      drawGraph();
      return;
    }
    empty.classList.add("hidden");
    prepareGraph(data);
    stat.textContent = `${data.nodes.length} 篇文档 · ${data.edges.length} 条关联 · 阈值 ${threshold}${
      data.truncated ? " · 已达到计算上限" : ""}`;
  } catch (error) {
    if (requestId !== mapState.requestId) return;
    mapState.nodes = [];
    mapState.edges = [];
    drawGraph();
    stat.textContent = "关联图计算失败";
    empty.textContent = friendlyError(error, "计算关联图");
    empty.classList.remove("hidden");
    notify(friendlyError(error, "计算关联图"), true);
  } finally {
    if (requestId === mapState.requestId) loading.classList.add("hidden");
  }
}

function prepareGraph(data) {
  const count = data.nodes.length;
  const radius = Math.min(230, 80 + count * 9);
  mapState.nodes = data.nodes.map((node, i) => {
    const angle = (i / count) * Math.PI * 2;
    return {
      ...node,
      x: 500 + Math.cos(angle) * radius,
      y: 300 + Math.sin(angle) * radius,
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
  if (document.hidden || state.activeView !== "map") {
    mapState.raf = null;
    return;
  }
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
      const force = (6000 * alpha) / (dist * dist);
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
    const target = 290 - e.weight * 55;
    const force = ((dist - target) / dist) * 0.018 * alpha * (0.4 + e.weight);
    dx *= force; dy *= force;
    e.a.vx += dx; e.a.vy += dy;
    e.b.vx -= dx; e.b.vy -= dy;
  });

  // 向心 + 阻尼 + 边界
  nodes.forEach((n) => {
    n.vx += (500 - n.x) * 0.0014 * alpha;
    n.vy += (300 - n.y) * 0.0014 * alpha;
    if (n === mapState.dragging) { n.vx = 0; n.vy = 0; return; }
    n.vx *= 0.82;
    n.vy *= 0.82;
    n.x = Math.max(n.r + 42, Math.min(958 - n.r, n.x + n.vx));
    n.y = Math.max(n.r + 34, Math.min(578 - n.r, n.y + n.vy));
  });

  mapState.alpha = alpha * 0.985;
  drawGraph();
  if (mapState.alpha > 0.022 || mapState.dragging) {
    mapState.raf = requestAnimationFrame(tickGraph);
  } else {
    mapState.raf = null;
  }
}

function drawGraph() {
  const canvas = document.getElementById("map-canvas");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const style = getComputedStyle(document.documentElement);
  const rule = style.getPropertyValue("--line-strong").trim() || "#C4CFCA";
  const stamp = style.getPropertyValue("--accent").trim() || "#AD4A3C";
  const ink = style.getPropertyValue("--ink").trim() || "#182522";
  const ledger = style.getPropertyValue("--primary").trim() || "#173F3D";
  const sans = '"Segoe UI Variable", "Microsoft YaHei UI", sans-serif';

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

    ctx.font = `${isHover ? "600 " : ""}12px ${sans}`;
    ctx.textAlign = "center";
    const label = n.title.replace(/\.(md|txt)$/i, "").slice(0, 16);
    const labelY = n.y + n.r + 17;
    const labelWidth = ctx.measureText(label).width;
    ctx.fillStyle = "rgba(255, 255, 255, .82)";
    ctx.fillRect(n.x - labelWidth / 2 - 3, labelY - 11, labelWidth + 6, 15);
    ctx.fillStyle = ink;
    ctx.fillText(label, n.x, labelY);
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

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.activeView === "map" && mapState.nodes.length && !mapState.raf) {
    mapState.alpha = Math.max(mapState.alpha, 0.12);
    tickGraph();
  }
});

/* ── 会话渲染（含历史记录回看） ───────────────────── */

function renderConversation() {
  const flow = document.getElementById("ask-flow");
  const welcome = document.getElementById("ask-welcome");
  const conv = activeConversation();
  flow.innerHTML = "";
  if (!conv || !conv.turns.length) {
    welcome.classList.remove("hidden");
    return;
  }
  welcome.classList.add("hidden");
  conv.turns.forEach((turn) => {
    const entry = document.createElement("article");
    entry.className = "entry";
    if (turn.kind === "workflow") {
      entry.innerHTML = renderTrace(turn.question, turn.result, turn.durationMs ?? null);
      bindCitations(entry, (turn.result && turn.result.citations) || []);
    } else {
      entry.innerHTML = renderAnswerEntry(turn.question, turn.answer, turn.durationMs ?? null);
      bindCitations(entry, (turn.answer && turn.answer.citations) || []);
    }
    flow.appendChild(entry);
  });
  // 历史按旧→新排列，进来先看到最新的几轮（贴近底部输入框）
  requestAnimationFrame(() => {
    const last = flow.querySelector(".entry:last-child");
    if (last) last.scrollIntoView({ block: "end" });
  });
}

/* ── 启动 ─────────────────────────────────────────── */

async function checkService() {
  const dot = document.getElementById("service-dot");
  const label = document.getElementById("service-status");
  dot.className = "service-dot checking";
  label.textContent = "检查中";
  try {
    const response = await fetch("/ready", { cache: "no-store" });
    if (!response.ok) throw new Error("readiness check failed");
    dot.className = "service-dot";
    label.textContent = "已连接";
  } catch (_) {
    dot.className = "service-dot offline";
    label.textContent = "未连接";
  }
}

window.addEventListener("online", checkService);
window.addEventListener("offline", checkService);

(async function start() {
  bindGraphCanvas();
  wireComposer("ask-input", "ask-form");
  wireComposer("trace-input", "trace-form");
  ensureConversation();
  renderConvSelect();
  renderConversation();
  updateEvidenceState();
  const savedEvidence = localStorage.getItem(EVIDENCE_KEY);
  const evidenceOpen = window.innerWidth <= 1020
    ? false
    : savedEvidence === null || savedEvidence === "1";
  setEvidenceOpen(evidenceOpen, false);
  checkService();

  try {
    await loadKbs();
    const initialView = window.location.hash.slice(1);
    activateView(VIEW_META[initialView] ? initialView : "ask", false);
  } catch (error) {
    notify(friendlyError(error, "加载应用数据"), true);
    document.getElementById("service-dot").className = "service-dot offline";
    document.getElementById("service-status").textContent = "未连接";
  }
})();
