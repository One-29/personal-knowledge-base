"""纯规则的双阈值证据判定。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from .models import EvidenceBand, RetrievalEvidenceData


class SimilarityCandidate(Protocol):
    vector_similarity: float | None


def classify_retrieval_evidence(
    candidates: Sequence[SimilarityCandidate],
    *,
    refusal_threshold: float,
    answer_threshold: float,
) -> RetrievalEvidenceData:
    """按最高有效向量相似度把证据分成拒答、灰区和可回答。

    候选存在但没有有效向量分数时归入灰区。这样关键词召回仍可供用户
    人工核对，但不会在缺少可比较分数时直接触发模型生成。
    """
    similarities: list[float] = []
    for candidate in candidates:
        if candidate.vector_similarity is None:
            continue
        try:
            similarity = float(candidate.vector_similarity)
        except (TypeError, ValueError):
            continue
        if math.isfinite(similarity) and -1.0 <= similarity <= 1.0:
            similarities.append(similarity)
    return classify_similarity(
        candidate_count=len(candidates),
        max_vector_similarity=max(similarities, default=None),
        refusal_threshold=refusal_threshold,
        answer_threshold=answer_threshold,
    )


def classify_similarity(
    *,
    candidate_count: int,
    max_vector_similarity: float | None,
    refusal_threshold: float,
    answer_threshold: float,
) -> RetrievalEvidenceData:
    """根据已采集的检索摘要执行同一套双阈值判定，供离线评估复用。"""
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < 0
    ):
        raise ValueError("candidate_count 必须是非负整数")
    lower = _validated_threshold(refusal_threshold, name="refusal_threshold")
    upper = _validated_threshold(answer_threshold, name="answer_threshold")
    if lower >= upper:
        raise ValueError("refusal_threshold 必须小于 answer_threshold")

    maximum: float | None = None
    if max_vector_similarity is not None:
        try:
            value = float(max_vector_similarity)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value) and -1.0 <= value <= 1.0:
            maximum = value
    if candidate_count == 0:
        band = EvidenceBand.INSUFFICIENT
    elif maximum is None:
        band = EvidenceBand.BORDERLINE
    elif maximum < lower:
        band = EvidenceBand.INSUFFICIENT
    elif maximum < upper:
        band = EvidenceBand.BORDERLINE
    else:
        band = EvidenceBand.SUFFICIENT

    return RetrievalEvidenceData(
        band=band,
        candidate_count=candidate_count,
        max_vector_similarity=maximum,
        refusal_threshold=lower,
        answer_threshold=upper,
    )


def _validated_threshold(value: float, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数值")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{name} 必须是有限数值")
    return normalized
