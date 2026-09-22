"""模型、检索与索引阶段的统一耗时记录。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from time import perf_counter
from typing import TypeAlias

StageValue: TypeAlias = str | int | float | bool | None
logger = logging.getLogger("app.diagnostics.stage")


@contextmanager
def timed_stage(stage: str, **dimensions: StageValue) -> Iterator[None]:
    """记录阶段、结果和耗时；调用方只传 ID/计数，不传正文。"""
    started = perf_counter()
    try:
        yield
    except Exception:
        logger.warning(
            "stage=%s outcome=error duration_ms=%.1f%s",
            stage,
            (perf_counter() - started) * 1000,
            _dimensions(dimensions),
        )
        raise
    else:
        logger.info(
            "stage=%s outcome=ok duration_ms=%.1f%s",
            stage,
            (perf_counter() - started) * 1000,
            _dimensions(dimensions),
        )


def _dimensions(values: dict[str, StageValue]) -> str:
    if not values:
        return ""
    rendered = " ".join(
        f"{key}={_safe_value(value)}" for key, value in sorted(values.items())
    )
    return f" {rendered}"


def _safe_value(value: StageValue) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    # 阶段维度只允许短机器标识，换行与空白被压平以保护日志结构。
    return "_".join(value.split())[:80]
