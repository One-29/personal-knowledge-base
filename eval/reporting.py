"""评估指标的稳定 JSON 序列化与归档。"""

from __future__ import annotations

import json
from pathlib import Path

from app import evaluation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _retrieval_payload(metrics: evaluation.RetrievalMetrics) -> dict[str, object]:
    def values(item: evaluation.RetrievalMetrics) -> dict[str, object]:
        return {
            "total": item.total,
            "hits": item.hits,
            "recall_at_k": item.recall_at_k,
            "mrr": item.mrr,
        }

    payload = values(metrics)
    payload["by_library"] = {
        key: values(item) for key, item in sorted(metrics.by_library.items())
    }
    payload["by_difficulty"] = {
        key: values(item) for key, item in sorted(metrics.by_difficulty.items())
    }
    return payload


def _refusal_payload(metrics: evaluation.RefusalMetrics) -> dict[str, object]:
    def values(item: evaluation.RefusalMetrics) -> dict[str, object]:
        return {
            "out_of_kb_total": item.out_of_kb_total,
            "out_of_kb_refused": item.out_of_kb_refused,
            "out_of_kb_refusal_rate": item.out_of_kb_refusal_rate,
            "in_kb_total": item.in_kb_total,
            "in_kb_refused": item.in_kb_refused,
            "in_kb_false_refusal_rate": item.in_kb_false_refusal_rate,
            "refusal_reasons": item.refusal_reasons,
        }

    payload = values(metrics)
    payload["by_library"] = {
        key: values(item) for key, item in sorted(metrics.by_library.items())
    }
    payload["by_difficulty"] = {
        key: values(item) for key, item in sorted(metrics.by_difficulty.items())
    }
    return payload


def _write_report(path: Path, report: dict[str, object]) -> None:
    target = path if path.is_absolute() else PROJECT_ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n[评估报告] {target}")
