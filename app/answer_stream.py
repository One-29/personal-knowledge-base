"""问答生成流：把模型增量与最终可信结果分成明确的领域事件。

这里不处理 HTTP/SSE 格式。同步问答仍由 ``app.ask`` 完成；流式路由只把
本模块产生的事件编码为传输协议，从而让生成、引用校验和 API 格式彼此独立。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeAlias

from . import ask, generation
from .diagnostics import timed_stage
from .generation import LLMError, LLMProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnswerDelta:
    """尚未完成 L2 引用校验的模型文本增量。"""

    content: str


@dataclass(frozen=True)
class AnswerCompleted:
    """经过拒答门与引用校验、可以持久化和展示的最终结果。"""

    result: ask.AnswerData


AnswerStreamEvent: TypeAlias = AnswerDelta | AnswerCompleted


def stream_prepared_answer(
    prepared: ask.AnswerPreparation,
    *,
    llm: LLMProvider | None = None,
) -> Iterator[AnswerStreamEvent]:
    """生成文本增量，末尾恰好产生一个最终结果事件。

    L1 已在 ``prepare_answer`` 中完成。模型输出只作为草稿增量传输；完整文本
    到齐后仍调用问答服务的 L2 校验。供应商故障沿用同步接口的正常拒答语义。
    """
    if prepared.terminal_result is not None:
        yield AnswerCompleted(
            ask.finish_prepared_answer(prepared, prepared.terminal_result)
        )
        return

    parts: list[str] = []
    try:
        with timed_stage(
            "ask.answer_generation_stream",
            candidates=len(prepared.candidates),
        ):
            for content in generation.stream_answer(
                prepared.question,
                prepared.candidates,
                provider=llm,
            ):
                if not content:
                    continue
                parts.append(content)
                yield AnswerDelta(content)
    except LLMError as exc:
        logger.warning("流式生成失败: %s", exc)
        result = ask.refuse_prepared_answer(
            prepared,
            ask.REFUSAL_LLM_UNAVAILABLE,
        )
    else:
        result = ask.finish_generated_answer(prepared, "".join(parts))
    yield AnswerCompleted(result)
