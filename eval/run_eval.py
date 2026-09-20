r"""隔离的多知识库评估 CLI。

Windows 推荐通过 ``scripts/run-eval.ps1`` 运行。直接执行模块时，数据库与原文
目录必须已经显式指向 ``knowbase_eval`` 和 ``data/eval-storage``。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import sys

from app import evaluation
from app.core.config import settings
from app.db import SessionLocal

from .baseline import build_eval_kbs
from .environment import UnsafeEvalEnvironmentError, validate_eval_environment
from .reporting import _refusal_payload, _retrieval_payload, _write_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_SET_PATH = PROJECT_ROOT / "eval" / "eval_set.json"
THRESHOLDS = (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


def main() -> int:
    parser = argparse.ArgumentParser(description="KnowBase 多知识库质量评估")
    parser.add_argument(
        "--retrieval",
        action="store_true",
        help="跳过回答 LLM；仍运行检索与本地 τ 扫描",
    )
    parser.add_argument("--top-k", type=int, default=None, help="检索候选数")
    parser.add_argument("--report", type=Path, default=None, help="写出结构化 JSON 报告")
    args = parser.parse_args()
    if args.top_k is not None and args.top_k < 1:
        parser.error("--top-k 必须是正整数。")

    try:
        validate_eval_environment(settings.database_url, settings.storage_dir)
        dataset = evaluation.load_eval_set(EVAL_SET_PATH)
        evaluation.assert_baseline_scale(dataset)
    except (UnsafeEvalEnvironmentError, evaluation.EvalDatasetError) as exc:
        parser.error(str(exc))

    db = SessionLocal()
    try:
        kb_ids = build_eval_kbs(db, dataset)
        print(
            f"[评估基线] {len(dataset.libraries)} 个知识库 / "
            f"{dataset.document_count} 篇文档 / {len(dataset.items)} 条样本"
        )
        query_vectors = evaluation.embed_eval_questions(dataset.items)

        print("\n=== Q1 检索质量 ===")
        retrieval_metrics = evaluation.evaluate_retrieval(
            db,
            dataset.items,
            kb_ids,
            top_k=args.top_k,
            query_vectors=query_vectors,
        )
        print(retrieval_metrics.summary())

        refusal_metrics: evaluation.RefusalMetrics | None = None
        if not args.retrieval:
            print("\n=== Q2 拒答质量（真 LLM） ===")
            refusal_metrics = evaluation.evaluate_refusal(db, dataset.items, kb_ids)
            print(refusal_metrics.summary())

        print("\n=== Q3 τ 扫描（本地模拟 L1，零回答 LLM 调用） ===")
        samples = evaluation.collect_similarities(
            db,
            dataset.items,
            kb_ids,
            query_vectors=query_vectors,
        )
        threshold_results = evaluation.simulate_thresholds(samples, list(THRESHOLDS))
        print(f"{'τ':>6} | {'库外拒答率':>10} | {'库内误拒率':>10}")
        for threshold, result in threshold_results:
            print(
                f"{threshold:>6.2f} | {result.out_of_kb_refusal_rate:>10.3f} | "
                f"{result.in_kb_false_refusal_rate:>10.3f}"
            )

        print("\n=== 相似度分布 ===")
        for item, similarity in samples:
            tag = "库内" if item.in_kb else "库外"
            value = f"{similarity:.3f}" if similarity is not None else "无候选"
            print(f"  [{item.library}/{tag}] {value}  {item.question}")

        report: dict[str, object] = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "retrieval" if args.retrieval else "full",
            "dataset_version": dataset.version,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "query_vector_count": len(query_vectors),
            "configured_refusal_threshold": settings.refusal_similarity_threshold,
            "top_k": settings.retrieval_top_k if args.top_k is None else args.top_k,
            "library_count": len(dataset.libraries),
            "document_count": dataset.document_count,
            "item_count": len(dataset.items),
            "retrieval": _retrieval_payload(retrieval_metrics),
            "refusal": _refusal_payload(refusal_metrics) if refusal_metrics else None,
            "thresholds": [
                {
                    "threshold": threshold,
                    **_refusal_payload(result),
                }
                for threshold, result in threshold_results
            ],
            "similarities": [
                {
                    "library": item.library,
                    "difficulty": item.difficulty,
                    "in_kb": item.in_kb,
                    "question": item.question,
                    "max_vector_similarity": similarity,
                }
                for item, similarity in samples
            ],
        }
        if args.report is not None:
            _write_report(args.report, report)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
