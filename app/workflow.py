"""多步工作流编排（M4）：规划 → 逐步执行 → 缺料记录 → 汇总（05 设计文档）。

边界红线（02 §3 / 05 §1）：**只编排，不造检索**——
每个子步骤都调用 M3 的问答能力（`ask.answer_question`），复用同一套检索参数、
拒答阈值与防幻觉校验；M4 自己不做检索、不做生成。

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
from typing import Protocol

from sqlalchemy.orm import Session

from . import ask as ask_service
from . import generation
from .ask import CitationData
from .core.config import settings
from .generation import LLMError, LLMProvider

logger = logging.getLogger(__name__)

STEP_ANSWERED = "answered"           # 该步正常回答
STEP_INSUFFICIENT = "insufficient"   # 该步缺料（M3 拒答）
STEP_ERROR = "error"                 # 该步执行故障（技术问题）

PLANNER_SYSTEM_PROMPT = """把用户的综合任务拆解成若干可以独立检索的子问题。

要求：
1. 每个子问题必须自包含——不依赖其它步骤的上下文，能直接拿去做检索。
2. 输出 JSON 数组，每个元素形如：{{"goal": "该步要查什么", "query": "检索用的问题"}}
3. 最多 {max_steps} 个步骤；任务简单时只输出 1 步。
4. 只输出 JSON，不要 markdown 代码块、不要解释。"""
# 注：JSON 示例的花括号需转义为 {{ }}——否则 str.format 会把它当占位符（KeyError）


class PlannerError(RuntimeError):
    """规划失败（LLM 故障或输出无法解析）。"""


@dataclass
class StepPlan:
    """一个子步骤的规划结果。"""

    goal: str      # 该步要达到什么（给用户看）
    query: str     # 实际用于检索的问题


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
    for item in data[:max_steps]:
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal", "")).strip()
        query = str(item.get("query", "")).strip()
        if goal and query:
            plans.append(StepPlan(goal=goal, query=query))
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
    limit = max_steps or settings.workflow_max_steps
    plans = plan_steps(task, limit, provider=llm)

    steps: list[WorkflowStepData] = []
    for index, plan in enumerate(plans, start=1):
        try:
            answer = ask_service.answer_question(db, plan.query, kb_id, llm=llm)
        except ask_service.KnowledgeBaseNotFound:
            raise                                     # 库不存在是整体性错误，不降级为单步故障
        except Exception as exc:                     # 单步技术故障不中断后续步骤
            logger.exception("工作流第 %d 步执行失败", index)
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_ERROR, note=f"该步执行失败：{exc}",
                )
            )
            continue

        if answer.refused:
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_INSUFFICIENT, note=answer.content,
                )
            )
        else:
            steps.append(
                WorkflowStepData(
                    index=index, goal=plan.goal, query=plan.query,
                    status=STEP_ANSWERED, conclusion=answer.content,
                    citations=list(answer.citations),
                )
            )

    answer_text, citations = synthesize(task, steps)
    return WorkflowResultData(task=task, steps=steps, answer=answer_text, citations=citations)


# ── 汇总（AW4：规则化 + 全局统一编号） ───────────────────────

_CITATION_RE = re.compile(r"\[(\d+)\]")


def remap_citations(text: str, mapping: dict[int, int]) -> str:
    """把某步结论里的 [n] 重映射为全局编号（纯规则）。

    不做这一步，正文编号与全局引用列表会错位——前端溯源就会点错段落。
    """
    def _replace(match: re.Match[str]) -> str:
        local = int(match.group(1))
        global_index = mapping.get(local)
        return f"[{global_index}]" if global_index is not None else match.group(0)

    return _CITATION_RE.sub(_replace, text)


def synthesize(
    task: str,
    steps: list[WorkflowStepData],
) -> tuple[str, list[CitationData]]:
    """规则化汇总：分步列出结论 + 缺料说明，引用重编号为全局连续（AW4）。

    MVP 采用规则化汇总（而非再让 LLM 生成一遍）：结构化输出本就是"分步列出"，
    规则汇总可测、无二次幻觉、不引入编号漂移；LLM 汇总留作 V1.0 演进。
    """
    blocks: list[str] = []
    global_citations: list[CitationData] = []

    for step in steps:
        header = f"**{step.index}. {step.goal}**"
        if step.status == STEP_ANSWERED:
            mapping: dict[int, int] = {}
            for citation in step.citations:
                mapping[citation.index] = len(global_citations) + 1
                global_citations.append(replace(citation, index=mapping[citation.index]))
            body = remap_citations(step.conclusion or "", mapping)
            blocks.append(f"{header}\n\n{body}")
        elif step.status == STEP_INSUFFICIENT:
            blocks.append(f"{header}\n\n（知识库无相关内容：{step.note}）")
        else:
            blocks.append(f"{header}\n\n（该步未完成：{step.note}）")

    return "\n\n".join(blocks), global_citations
