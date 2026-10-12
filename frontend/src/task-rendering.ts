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
  insufficient: "证据不足",
  error: "未完成",
};

const ANSWER_LABEL: Record<AnswerStatus, string> = {
  answered: "KNOWBASE 回答",
  needs_review: "灰区 · 待核对",
  insufficient: "检索结果",
  unverified: "引用校验未通过",
  error: "服务未完成",
};

function refusalLabel(reason: string | null | undefined): string {
  const labels: Record<string, string> = {
    empty_kb: "检索未取得候选原文",
    low_relevance: "检索相关性不足",
    generation_insufficient: "原文不足以支持回答",
    borderline_relevance: "需人工核对原文",
    dependency_unavailable: "前序来源尚不可用",
    source_not_supported: "已有原文未覆盖本项内容",
    source_changed: "原文来源已变化",
    source_validation_failed: "原文来源暂无法核对",
    source_context_limit: "来源核对长度超限",
    context_incomplete: "必要上下文不完整",
    invalid_source_selection: "来源核对未通过校验",
    invalid_citation: "引用编号未通过校验",
    no_citation: "回答缺少有效引用",
    llm_unavailable: "回答服务暂不可用",
    embedding_unavailable: "检索服务暂不可用",
    embedding_mismatch: "向量模型与索引不一致",
    step_error: "步骤执行未完成",
  };
  return reason ? labels[reason] ?? "未能形成可靠回答" : "未能形成可靠回答";
}

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
      answer.status === "needs_review" ? "未生成回答" : refusalLabel(answer.refusal_reason),
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
      <div class="entry-a rich-text">${withCitations(answer.content, answer.citations)}</div>
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
      ? `<div class="trace-body rich-text">${withCitations(step.conclusion || "", step.citations)}</div>`
      : `<p class="trace-note">${escapeHtml(step.note || "")}</p>`;
    const sourceLookup = step.kind === "source_lookup";
    const queryLabel = sourceLookup ? "来源核对问题" : "检索语句";
    const dependencies = (step.depends_on ?? []).join("、");
    const sourceOrigin = dependencies ? `第 ${dependencies} 步` : "前序步骤";
    const origin = step.evidence_origin === "previous_step"
      ? `<p class="relevance-summary">使用${escapeHtml(sourceOrigin)}的原文核对出处 · 未重复检索</p>`
      : "";
    const rewritten = !sourceLookup && step.search_query && step.search_query !== step.query
      ? `<p class="trace-query">实际检索：${escapeHtml(step.search_query)}</p>` : "";
    const reason = step.refusal_reason
      ? `<p class="relevance-summary">${escapeHtml(refusalLabel(step.refusal_reason))}</p>` : "";
    return `<li><details class="trace-card">
      <summary class="trace-head"><span>${escapeHtml(step.goal)}</span>
        <span class="trace-state ${escapeHtml(step.status)}">${escapeHtml(statusLabel)}</span>
      </summary>
      <p class="trace-query">${queryLabel}：${escapeHtml(step.query)}</p>
      ${rewritten}${origin}${sourceLookup ? "" : relevanceSummary(step.evidence ?? null)}${reason}
      ${body}${possibleSourceList(step.possible_sources)}
    </details></li>`;
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
      <div class="entry-a rich-text">${withCitations(result.answer, result.citations)}</div>
      ${sourceSummary(result.citations)}
    </div>`;
}
