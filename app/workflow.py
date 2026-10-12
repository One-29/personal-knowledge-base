"""多步工作流编排（M4）：规划 → 逐步执行 → 缺料记录 → 汇总（05 设计文档）。

边界红线（02 §3 / 05 §1）：**只编排，不造检索**——
知识步骤调用 M3 检索问答，出处步骤调用 M3 原文来源核对；
M4 负责依赖调度与最终来源有效性检查，不自行实现检索或生成。

已拍板决策（05 §5）：
- AW1 LLM 规划步骤清单，解析失败退化为「单步 = 原任务」
- AW2 步骤上限（默认 5，可配置）
- AW3 MVP 同步执行（步骤状态在响应结构里对用户可见）
- AW4 引用全局统一编号（汇总时重映射，纯规则）
"""

import json
import logging
import re
from dataclasses import dataclass, field, replace
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import ask as ask_service
from . import generation
from .core.config import settings
from .diagnostics import current_request_id, timed_stage
from .evidence import (
    AnswerStatus, CitationData, EvidenceCandidateData, RetrievalEvidenceData, SourceSnapshot,
)
from .generation import LLMError, LLMProvider

logger = logging.getLogger(__name__)

STEP_ANSWERED = "answered"           # 该步正常回答
STEP_NEEDS_REVIEW = "needs_review"   # 该步命中灰区，需人工核对候选来源
STEP_INSUFFICIENT = "insufficient"   # 该步缺料（M3 拒答）
STEP_ERROR = "error"                 # 该步执行故障（技术问题）

PLANNER_SYSTEM_PROMPT = """把用户的综合任务拆解成知识查询和必要的来源核对步骤。

要求：
1. 忠实保留用户的问题和范围，不增加未要求的主题。知识查询必须自包含。
2. 输出 JSON 数组，每个元素形如：{{"goal": "该步目标", "query": "具体问题", "kind": "research", "depends_on": []}}
3. 最多 {max_steps} 个步骤；任务简单时只输出 1 步。
4. “给出出处、附引用、说明来源”通常是知识回答的输出要求，应保留在对应 query 中，不单独重复检索。
5. “比较数列极限和函数极限，并给出处”可只规划一个 research 步骤，不拆成两个独立的出处查询。
6. 用户明确要求单独整理前一步的出处时，使用 kind="source_lookup"，depends_on 填所依赖的前序步骤编号（从 1 开始）。
7. source_lookup 的 source_scope="topic" 表示从前序原文选择本项内容的出处，"all" 仅用于列出指定前序步骤的全部引用。
8. 查询新知识、穷举全部方法、追溯原始论文或作者属于 research；不得用已有结论的出处替代新知识检索。research 的 depends_on 必须为空。
9. 只输出 JSON，不要 markdown 代码块、不要解释。"""
# 注：JSON 示例的花括号需转义为 {{ }}——否则 str.format 会把它当占位符（KeyError）


class PlannerError(RuntimeError):
    """规划失败（LLM 故障或输出无法解析）。"""


@dataclass
class StepPlan:
    """一个子步骤的规划结果。"""

    goal: str      # 该步要达到什么（给用户看）
    query: str     # 实际用于检索的问题
    kind: str = "research"
    depends_on: list[int] = field(default_factory=list)
    source_scope: str = "topic"


@dataclass
class WorkflowStepData:
    """一个子步骤的执行结果。"""

    index: int
    goal: str
    query: str
    status: str
    conclusion: str | None = None
    note: str | None = None
    citations: list[CitationData] = field(default_factory=list)
    possible_sources: list[EvidenceCandidateData] = field(default_factory=list)
    kind: str = "research"
    depends_on: list[int] = field(default_factory=list)
    source_scope: str = "topic"
    search_query: str | None = None
    evidence: RetrievalEvidenceData | None = None
    refusal_reason: str | None = None
    evidence_origin: str | None = None


@dataclass
class WorkflowResultData:
    """工作流整体结果（schemas 层负责序列化）。"""

    task: str
    steps: list[WorkflowStepData]
    answer: str
    citations: list[CitationData] = field(default_factory=list)


# ── 规划（AW1） ─────────────────────────────────────────────

def _strip_code_fence(raw: str) -> str:
    """去掉模型可能包上的 ```json ``` 代码块围栏。"""
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_plan(raw: str, max_steps: int) -> list[StepPlan]:
    """解析规划输出；无法解析或为空时返回空列表（由调用方退化）。"""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        logger.warning("规划输出不是合法 JSON，退化为单步")
        return []
    if not isinstance(data, list):
        return []

    plans: list[StepPlan] = []
    for raw_index, item in enumerate(data[:max_steps], start=1):
        if not isinstance(item, dict):
            continue
        goal = item.get("goal", "")
        query = item.get("query", "")
        if not isinstance(goal, str) or not isinstance(query, str) or not goal.strip() or not query.strip():
            continue
        kind = item.get("kind", "research")
        dependencies = item.get("depends_on", [])
        source_scope = item.get("source_scope", "topic")
        if (
            not isinstance(kind, str) or kind not in {"research", "source_lookup"}
            or not isinstance(source_scope, str) or source_scope not in {"topic", "all"}
            or not isinstance(dependencies, list)
            or any(isinstance(value, bool) or not isinstance(value, int)
                   or not 1 <= value <= len(plans) for value in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or (kind == "research" and dependencies)
            or (kind == "source_lookup" and (not dependencies or raw_index != len(plans) + 1))
        ):
            logger.warning("规划步骤类型或依赖不合法，退化为原任务")
            return []
        plans.append(StepPlan(
            goal=goal.strip(), query=query.strip(), kind=kind,
            depends_on=dependencies, source_scope=source_scope,
        ))
    return plans


def plan_steps(
    task: str,
    max_steps: int,
    provider: LLMProvider | None = None,
) -> list[StepPlan]:
    """把任务拆成子步骤；失败退化为「单步 = 原任务」（AW1）。"""
    fallback = [StepPlan(goal=task, query=task)]
    try:
        llm = provider or generation.get_llm_provider()
        raw = llm.complete(
            PLANNER_SYSTEM_PROMPT.format(max_steps=max_steps), task
        )
    except LLMError as exc:
        logger.warning("规划调用失败，退化为单步: %s", exc)
        return fallback

    plans = parse_plan(raw, max_steps)
    return plans or fallback


# ── 执行（AW3：同步逐步执行） ────────────────────────────────

def run_workflow(
    db: Session,
    task: str,
    kb_id: int | None,
    max_steps: int | None = None,
    llm: LLMProvider | None = None,
) -> WorkflowResultData:
    """执行一次多步任务：规划 → 逐步调 M3 → 汇总。"""
    requested_limit = max_steps if max_steps is not None else settings.workflow_max_steps
    limit = min(settings.workflow_max_steps, requested_limit)
    if limit < 1:
        raise ValueError("max_steps 必须大于 0")
    scope = kb_id if kb_id is not None else "all"
    with timed_stage("workflow.validate_scope", kb_id=scope):
        ask_service.validate_kb(db, kb_id)
    with timed_stage("workflow.planning", max_steps=limit):
        plans = plan_steps(task, limit, provider=llm)

    steps: list[WorkflowStepData] = []
    sources_by_step: dict[int, list[SourceSnapshot]] = {}
    for index, plan in enumerate(plans, start=1):
        try:
            with timed_stage("workflow.step", step=index, kb_id=scope, kind=plan.kind):
                if plan.kind == "source_lookup":
                    parents = [steps[number - 1] for number in plan.depends_on]
                    sources = [source for number in plan.depends_on
                               for source in sources_by_step.get(number, [])]
                    if any(parent.status != STEP_ANSWERED for parent in parents) or not sources:
                        answer = ask_service.AnswerData(
                            question=plan.query,
                            content=ask_service.REFUSAL_MESSAGES[ask_service.REFUSAL_DEPENDENCY_UNAVAILABLE],
                            status=(AnswerStatus.NEEDS_REVIEW if any(
                                parent.status == STEP_NEEDS_REVIEW for parent in parents
                            ) else AnswerStatus.INSUFFICIENT),
                            refusal_reason=ask_service.REFUSAL_DEPENDENCY_UNAVAILABLE,
                            possible_sources=[candidate for parent in parents for candidate in parent.possible_sources],
                        )
                    else:
                        answer = ask_service.answer_from_sources(
                            db, plan.query, kb_id, sources, source_scope=plan.source_scope, llm=llm,
                        )
                else:
                    answer = ask_service.answer_question(db, plan.query, kb_id, llm=llm)
        except ask_service.KnowledgeBaseNotFound:
            db.rollback()
            raise                                     # 库不存在是整体性错误，不降级为单步故障
        except Exception:                            # 单步技术故障不中断后续步骤
            db.rollback()
            logger.exception("工作流第 %d 步执行失败", index)
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_ERROR, note=_step_error_note(),
                    kind=plan.kind, depends_on=list(plan.depends_on), source_scope=plan.source_scope,
                    refusal_reason="step_error",
                    evidence_origin="previous_step" if plan.kind == "source_lookup" else "retrieval",
                )
            )
            continue

        details = {
            "kind": plan.kind, "depends_on": list(plan.depends_on),
            "source_scope": plan.source_scope, "search_query": answer.search_query,
            "evidence": answer.evidence, "refusal_reason": answer.refusal_reason,
            "evidence_origin": "previous_step" if plan.kind == "source_lookup" else "retrieval",
        }
        logger.info(
            "workflow_step=%s kind=%s status=%s refusal_reason=%s reused_sources=%s",
            index, plan.kind, answer.status.value, answer.refusal_reason or "none",
            len(answer.source_snapshots) if plan.kind == "source_lookup" else 0,
        )
        if answer.status in {AnswerStatus.ERROR, AnswerStatus.UNVERIFIED}:
            steps.append(
                WorkflowStepData(
                    index=index,
                    goal=plan.goal,
                    query=plan.query,
                    status=STEP_ERROR,
                    note=answer.content,
                    **details,
                )
            )
        elif answer.status is AnswerStatus.NEEDS_REVIEW:
            steps.append(
                WorkflowStepData(
                    index=index,
                    goal=plan.goal,
                    query=plan.query,
                    status=STEP_NEEDS_REVIEW,
                    note=answer.content,
                    possible_sources=list(answer.possible_sources),
                    **details,
                )
            )
        elif answer.refused:
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_INSUFFICIENT, note=answer.content,
                    **details,
                )
            )
        else:
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_ANSWERED, conclusion=answer.content,
                    citations=list(answer.citations),
                    **details,
                )
            )
            sources_by_step[index] = list(answer.source_snapshots)

    all_sources = [source for sources in sources_by_step.values() for source in sources]
    if all_sources:
        validation_reason = ask_service.REFUSAL_SOURCE_CHANGED
        try:
            with timed_stage("workflow.validate_sources", sources=len(all_sources), kb_id=scope):
                invalid = ask_service.invalid_source_chunk_ids(db, all_sources, kb_id)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("工作流最终来源核对失败")
            invalid = {source.citation.chunk_id for source in all_sources}
            validation_reason = ask_service.REFUSAL_SOURCE_VALIDATION_FAILED
        changed_steps: set[int] = set()
        for step in steps:
            stale = any(source.citation.chunk_id in invalid for source in sources_by_step.get(step.index, []))
            dependency_changed = any(number in changed_steps for number in step.depends_on)
            if stale or dependency_changed:
                changed_steps.add(step.index)
                step.status = STEP_ERROR
                step.refusal_reason = validation_reason if stale else ask_service.REFUSAL_DEPENDENCY_UNAVAILABLE
                step.note = ask_service.REFUSAL_MESSAGES[step.refusal_reason]
                step.conclusion = None
                step.citations = []
                step.possible_sources = []
                logger.info("workflow_step=%s final_status=error reason=%s", step.index, step.refusal_reason)

    with timed_stage("workflow.synthesis", steps=len(steps)):
        answer_text, citations = synthesize(task, steps)
    return WorkflowResultData(task=task, steps=steps, answer=answer_text, citations=citations)


def _step_error_note() -> str:
    """给界面返回可排障但不含 SQL、路径或供应商响应的故障说明。"""
    request_id = current_request_id()
    suffix = f"（请求 ID：{request_id}）" if request_id != "-" else ""
    return f"该步执行失败，请重试或复制诊断信息{suffix}"


# ── 汇总（AW4：规则化 + 全局统一编号） ───────────────────────

_CITATION_RE = re.compile(r"\[(\d+)\]")


def remap_citations(
    text: str,
    mapping: dict[int, int],
    *,
    strict: bool = False,
) -> str:
    """把某步结论里的 [n] 重映射为全局编号（纯规则）。

    不做这一步，正文编号与全局引用列表会错位——前端溯源就会点错段落。
    strict 用于正式汇总：不能把找不到来源的编号留给前端误认。
    """
    def _replace(match: re.Match[str]) -> str:
        local = int(match.group(1))
        global_index = mapping.get(local)
        if global_index is None:
            if strict:
                raise ValueError(f"引用编号 [{local}] 没有对应的来源")
            return match.group(0)
        return f"[{global_index}]"

    return _CITATION_RE.sub(_replace, text)


def synthesize(
    task: str,
    steps: list[WorkflowStepData],
) -> tuple[str, list[CitationData]]:
    """规则化汇总：分步列出结论 + 缺料说明，步骤与正文共用全局引用编号。

    MVP 采用规则化汇总（而非再让 LLM 生成一遍）：结构化输出本就是"分步列出"，
    同一块跨步骤复用编号；规则汇总可测、无二次幻觉、不引入编号漂移。
    LLM 汇总留作 V1.0 演进。
    """
    blocks: list[str] = []
    global_citations: list[CitationData] = []
    citations_by_chunk: dict[int, CitationData] = {}

    for step in steps:
        header = f"**{step.index}. {step.goal}**"
        if step.status == STEP_ANSWERED:
            mapping: dict[int, int] = {}
            pending: dict[int, CitationData] = {}
            step_citations: list[CitationData] = []
            seen_step_chunks: set[int] = set()
            invalid_mapping = False
            for citation in step.citations:
                global_citation = citations_by_chunk.get(citation.chunk_id)
                if global_citation is None:
                    global_citation = pending.get(citation.chunk_id)
                if global_citation is None:
                    global_citation = replace(
                        citation,
                        index=len(global_citations) + len(pending) + 1,
                    )
                    pending[citation.chunk_id] = global_citation
                if global_citation.doc_id != citation.doc_id:
                    invalid_mapping = True
                    break
                previous = mapping.get(citation.index)
                if previous is not None and previous != global_citation.index:
                    invalid_mapping = True
                    break
                mapping[citation.index] = global_citation.index
                if citation.chunk_id not in seen_step_chunks:
                    step_citations.append(global_citation)
                    seen_step_chunks.add(citation.chunk_id)
            try:
                if invalid_mapping:
                    raise ValueError("同一步的引用编号指向多个来源")
                body = remap_citations(step.conclusion or "", mapping, strict=True)
            except ValueError:
                logger.warning("工作流第 %d 步引用映射失败，结论未展示", step.index)
                step.status = STEP_ERROR
                step.note = "该步引用无法与来源对应，结论未展示。"
                step.conclusion = None
                step.citations = []
                step.refusal_reason = ask_service.REFUSAL_INVALID_CITATION
                blocks.append(f"{header}\n\n（该步未完成：{step.note}）")
                continue

            global_citations.extend(pending.values())
            citations_by_chunk.update(pending)
            step.conclusion = body
            step.citations = step_citations
            blocks.append(f"{header}\n\n{body}")
        elif step.status == STEP_INSUFFICIENT:
            blocks.append(f"{header}\n\n（本步证据不足：{step.note}）")
        elif step.status == STEP_NEEDS_REVIEW:
            blocks.append(f"{header}\n\n（找到可能相关的原文，需人工核对：{step.note}）")
        else:
            blocks.append(f"{header}\n\n（该步未完成：{step.note}）")

    return "\n\n".join(blocks), global_citations
