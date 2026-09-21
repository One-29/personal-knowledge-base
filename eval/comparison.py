"""评估报告兼容性校验与存储迁移质量门。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class EvaluationGateError(ValueError):
    """报告不可比较或候选质量低于迁移门槛。"""


@dataclass(frozen=True)
class EvaluationComparison:
    reference_recall: float
    candidate_recall: float
    reference_mrr: float
    candidate_mrr: float
    max_mrr_drop: float

    @property
    def mrr_drop(self) -> float:
        return self.reference_mrr - self.candidate_mrr

    def summary(self) -> str:
        return (
            "评估迁移门通过："
            f"recall@k {self.reference_recall:.3f} → {self.candidate_recall:.3f}，"
            f"MRR {self.reference_mrr:.3f} → {self.candidate_mrr:.3f} "
            f"（下降 {max(0.0, self.mrr_drop):.3f} / 上限 {self.max_mrr_drop:.3f}）"
        )


COMPARABILITY_FIELDS = (
    "dataset_version",
    "embedding_model",
    "embedding_dimension",
    "query_vector_count",
    "configured_refusal_threshold",
    "top_k",
    "library_count",
    "document_count",
    "item_count",
)


def load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationGateError(f"无法读取评估报告 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationGateError(f"评估报告顶层必须是对象：{path}")
    return payload


def compare_reports(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_mrr_drop: float = 0.01,
    max_group_mrr_drop: float = 0.05,
) -> EvaluationComparison:
    """要求语料/模型/TopK 相同、recall 不下降且 MRR 下降不超过阈值。"""
    if max_mrr_drop < 0 or max_group_mrr_drop < 0:
        raise ValueError("MRR 下降上限不能小于 0")
    missing = [
        field
        for field in COMPARABILITY_FIELDS
        if field not in reference or field not in candidate
    ]
    if missing:
        raise EvaluationGateError(
            f"评估报告缺少可比字段：{', '.join(missing)}"
        )
    mismatches = [
        field
        for field in COMPARABILITY_FIELDS
        if reference.get(field) != candidate.get(field)
    ]
    if mismatches:
        details = "，".join(
            f"{field}: {reference.get(field)!r} != {candidate.get(field)!r}"
            for field in mismatches
        )
        raise EvaluationGateError(f"评估报告不可比较：{details}")

    reference_retrieval = _retrieval_metrics(reference, "参考")
    candidate_retrieval = _retrieval_metrics(candidate, "候选")
    result = EvaluationComparison(
        reference_recall=reference_retrieval[0],
        candidate_recall=candidate_retrieval[0],
        reference_mrr=reference_retrieval[1],
        candidate_mrr=candidate_retrieval[1],
        max_mrr_drop=max_mrr_drop,
    )
    if result.candidate_recall + 1e-12 < result.reference_recall:
        raise EvaluationGateError(
            "迁移后 recall@k 下降："
            f"{result.reference_recall:.6f} → {result.candidate_recall:.6f}"
        )
    if result.mrr_drop > max_mrr_drop + 1e-12:
        raise EvaluationGateError(
            "迁移后 MRR 下降超过上限："
            f"{result.reference_mrr:.6f} → {result.candidate_mrr:.6f}，"
            f"允许下降 {max_mrr_drop:.6f}"
        )
    _validate_breakdowns(
        reference,
        candidate,
        max_mrr_drop=max_group_mrr_drop,
    )
    return result


def _retrieval_metrics(report: dict[str, Any], label: str) -> tuple[float, float]:
    payload = report.get("retrieval")
    if not isinstance(payload, dict):
        raise EvaluationGateError(f"{label}报告缺少 retrieval 指标。")
    try:
        recall = float(payload["recall_at_k"])
        mrr = float(payload["mrr"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationGateError(f"{label}报告的检索指标无效。") from exc
    if not 0.0 <= recall <= 1.0 or not 0.0 <= mrr <= 1.0:
        raise EvaluationGateError(f"{label}报告的检索指标超出 0–1。")
    return recall, mrr


def _validate_breakdowns(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_mrr_drop: float,
) -> None:
    reference_retrieval = reference["retrieval"]
    candidate_retrieval = candidate["retrieval"]
    for breakdown in ("by_library", "by_difficulty"):
        reference_groups = reference_retrieval.get(breakdown)
        candidate_groups = candidate_retrieval.get(breakdown)
        if not isinstance(reference_groups, dict) or not isinstance(candidate_groups, dict):
            raise EvaluationGateError(f"评估报告缺少 {breakdown} 分组指标。")
        if set(reference_groups) != set(candidate_groups):
            raise EvaluationGateError(f"评估报告的 {breakdown} 分组不一致。")
        for group in sorted(reference_groups):
            reference_metrics = _retrieval_metrics(
                {"retrieval": reference_groups[group]},
                f"参考 {breakdown}/{group}",
            )
            candidate_metrics = _retrieval_metrics(
                {"retrieval": candidate_groups[group]},
                f"候选 {breakdown}/{group}",
            )
            if candidate_metrics[0] + 1e-12 < reference_metrics[0]:
                raise EvaluationGateError(
                    f"迁移后 {breakdown}/{group} recall@k 下降。"
                )
            if reference_metrics[1] - candidate_metrics[1] > max_mrr_drop + 1e-12:
                raise EvaluationGateError(
                    f"迁移后 {breakdown}/{group} MRR 下降超过 {max_mrr_drop:.3f}。"
                )
