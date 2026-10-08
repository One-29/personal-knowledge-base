import {
  escapeHtml,
  questionHeader,
  renderRichText,
  sourceSummary,
  withCitations,
} from "./format";
import type {
  AnswerResponse,
  AnswerStatus,
  EvidenceCandidate,
  RetrievalEvidence,
  WorkflowResponse,
  WorkflowStepStatus,
} from "./types";

const STEP_STATUS: Record<WorkflowStepStatus, string> = {
  answered: "已作答",
  needs_review: "待核对",
  insufficient: "没有材料",
  error: "未完成",
};

const ANSWER_LABEL: Record<AnswerStatus, string> = {
  answered: "KNOWBASE 回答",
  needs_review: "灰区 · 待核对",
  insufficient: "检索结果",
  unverified: "引用校验未通过",
  error: "服务未完成",
};

function relevanceSummary(evidence: RetrievalEvidence | null): string {
  if (evidence === null) return "";
  const maximum = evidence.max_vector_similarity === null
    ? "没有可比较的向量分数"
    : `最高向量相似度 ${evidence.max_vector_similarity.toFixed(3)}`;
  return `<p class="relevance-summary">${escapeHtml(maximum)} · 明确拒答线
    ${evidence.refusal_threshold.toFixed(2)} · 自动回答线
    ${evidence.answer_threshold.toFixed(2)} · 共 ${evidence.candidate_count} 条候选</p>`;
}

function possibleSourceList(sources: EvidenceCandidate[]): string {
  if (sources.length === 0) return "";
  const items = sources.map((source) => {
    const score = source.vector_similarity === null
      ? "仅关键词命中"
      : `向量相似度 ${source.vector_similarity.toFixed(3)}`;
    const characters = Array.from(source.chunk_text);
    const excerpt = characters.length > 150
      ? `${characters.slice(0, 150).join("")}…`
      : source.chunk_text;
    return `<button type="button" class="candidate-source source-pill"
      data-chunk="${source.chunk_id}" data-index="${source.index}"
      aria-label="打开候选原文 ${source.index}">
      <span><b>候选 ${source.index}</b>${escapeHtml(source.doc_title)}</span>
      <small>${escapeHtml(score)}</small>
      <q>${escapeHtml(excerpt)}</q>
    </button>`;
  }).join("");
  return `<section class="possible-sources" aria-label="可能相关的候选原文">
    <div class="possible-sources-head"><strong>可能相关的原文</strong><span>需人工核对，不是正式引用</span></div>
    <div class="candidate-source-list">${items}</div>
  </section>`;
}

export function renderAnswerEntry(
  question: string,
  answer: AnswerResponse,
  durationMs: number | null = null,
): string {
  const refusal = answer.refused
    ? `<span class="answer-status ${answer.status}">${escapeHtml(
      answer.status === "needs_review" ? "未生成回答" : answer.refusal_reason || "未能形成可靠回答",
    )}</span>`
    : "";
  const rewritten = answer.search_query && answer.search_query !== question
    ? `<p class="rewrote">按上文改写后检索：<code>${escapeHtml(answer.search_query)}</code></p>`
    : "";
  const duration = durationMs === null
    ? ""
    : `<span class="meta-dot">${Math.max(1, Math.round(durationMs / 1000))} 秒</span>`;
  return `${questionHeader(question)}
    <div class="answer-shell">
      <div class="answer-meta">
        <span class="answer-label">${ANSWER_LABEL[answer.status]}</span>
        <span class="meta-dot">${answer.status === "needs_review"
          ? `${answer.possible_sources.length} 条候选原文`
          : `${answer.citations.length} 条引用`}</span>
        ${duration}${refusal}
      </div>
      ${rewritten}
      ${relevanceSummary(answer.evidence)}
      <div class="entry-a rich-text">${withCitations(answer.content)}</div>
      ${possibleSourceList(answer.possible_sources)}
      ${sourceSummary(answer.citations)}
    </div>`;
}

export function renderStreamingAnswerEntry(
  question: string,
  searchQuery: string | null,
  content: string,
): string {
  const rewritten = searchQuery && searchQuery !== question
    ? `<p class="rewrote">按上文改写后检索：<code>${escapeHtml(searchQuery)}</code></p>`
    : "";
  const body = content
    ? renderRichText(content, false)
    : "<p>已找到相关资料，正在接收回答…</p>";
  return `${questionHeader(question)}
    <div class="answer-shell streaming-answer">
      <div class="answer-meta">
        <span class="answer-label">KNOWBASE 回答</span>
        <span class="streaming-status"><i></i>正在生成 · 完成后校验引用</span>
      </div>
      ${rewritten}
      <div class="entry-a rich-text" data-stream-content aria-live="polite">${body}</div>
    </div>`;
}

export function renderWorkflowEntry(
  task: string,
  result: WorkflowResponse,
  durationMs: number | null = null,
): string {
  const steps = result.steps.map((step) => {
    const statusLabel = STEP_STATUS[step.status] ?? String(step.status);
    const body = step.status === "answered"
      ? `<div class="trace-body rich-text">${withCitations(step.conclusion || "")}</div>`
      : `<p class="trace-note">${escapeHtml(step.note || "")}</p>`;
    const citations = step.citations.length > 0
      ? `<div class="trace-cites">${step.citations.map((citation) => (
        `<span class="cite" data-chunk="${citation.chunk_id}" data-index="${citation.index}" role="button" tabindex="0" aria-label="打开引用 ${citation.index}">[${citation.index}]</span>`
      )).join("")}</div>`
      : "";
    return `<li><div class="trace-card">
      <div class="trace-head">${escapeHtml(step.goal)}
        <span class="trace-state ${escapeHtml(step.status)}">${escapeHtml(statusLabel)}</span>
      </div>
      <p class="trace-query">检索语句：${escapeHtml(step.query)}</p>
      ${body}${possibleSourceList(step.possible_sources)}${citations}
    </div></li>`;
  }).join("");
  const answered = result.steps.filter((step) => step.status === "answered").length;
  const duration = durationMs === null ? "" : ` · ${Math.max(1, Math.round(durationMs / 1000))} 秒`;
  return `${questionHeader(task)}
    <div class="answer-shell">
      <div class="answer-meta">
        <span class="answer-label">WORKFLOW 结果</span>
        <span class="meta-dot">${answered}/${result.steps.length} 步完成${duration}</span>
      </div>
      <ol class="trace-list">${steps}</ol>
    </div>
    <div class="summary">
      <div class="summary-head"><h3>综合结论</h3><span>${result.citations.length} 条引用</span></div>
      <div class="entry-a rich-text">${withCitations(result.answer)}</div>
      ${sourceSummary(result.citations)}
    </div>`;
}
