r"""隔离的多知识库评估 CLI。

Windows 推荐通过 ``scripts/run-eval.ps1`` 运行。直接执行模块时，数据库与原文
目录必须显式指向隔离的 ``knowbase_eval`` 或固定 SQLite 评估文件。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import sys

from sqlalchemy.engine import make_url

from app import evaluation
from app.core.config import settings
from app.database import initialize_database
from app.db import SessionLocal, engine
from app.embedding_profile import stored_embedding_profile

from .baseline import build_eval_kbs
from .comparison import EvaluationGateError, compare_reports, load_report
from .environment import (
    UnsafeEvalEnvironmentError,
    validate_eval_engine,
    validate_eval_environment,
)
from .reporting import _refusal_payload, _retrieval_payload, _write_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_SET_PATH = PROJECT_ROOT / "eval" / "eval_set.json"
REFUSAL_THRESHOLDS = (0.35, 0.40, 0.45, 0.50)
ANSWER_THRESHOLDS = (0.50, 0.55, 0.60)
THRESHOLD_PAIRS = tuple(
    (refusal, answer)
    for refusal in REFUSAL_THRESHOLDS
    for answer in ANSWER_THRESHOLDS
    if refusal < answer
)


def main() -> int:
    parser = argparse.ArgumentParser(description="KnowBase 多知识库质量评估")
    parser.add_argument(
        "--retrieval",
        action="store_true",
        help="跳过回答 LLM；仍运行检索与本地 τ 扫描",
    )
    parser.add_argument("--top-k", type=int, default=None, help="检索候选数")
    parser.add_argument("--report", type=Path, default=None, help="写出结构化 JSON 报告")
    parser.add_argument(
        "--reference-report",
        type=Path,
        default=None,
        help="与参考报告比较；recall 不得下降，MRR 最多下降 0.01",
    )
    args = parser.parse_args()
    if args.top_k is not None and args.top_k < 1:
        parser.error("--top-k 必须是正整数。")

    try:
        validate_eval_environment(settings.database_url, settings.storage_dir)
        validate_eval_engine(engine, settings.database_url, settings.storage_dir)
        dataset = evaluation.load_eval_set(EVAL_SET_PATH)
        evaluation.assert_baseline_scale(dataset)
    except (UnsafeEvalEnvironmentError, evaluation.EvalDatasetError) as exc:
        parser.error(str(exc))

    initialize_database(engine)
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

        print("\n=== Q3 双阈值扫描（本地模拟 L1，零回答 LLM 调用） ===")
        samples = evaluation.collect_similarities(
            db,
            dataset.items,
            kb_ids,
            query_vectors=query_vectors,
        )
        threshold_results = evaluation.simulate_threshold_pairs(
            samples,
            list(THRESHOLD_PAIRS),
        )
        print(
            f"{'拒答线':>6} | {'回答线':>6} | "
            f"{'库外 回答/灰区/拒答':>23} | {'库内 回答/灰区/拒答':>23}"
        )
        for refusal_threshold, answer_threshold, result in threshold_results:
            print(
                f"{refusal_threshold:>6.2f} | {answer_threshold:>6.2f} | "
                f"{result.out_of_kb_answer_rate:.3f}/"
                f"{result.out_of_kb_review_rate:.3f}/"
                f"{result.out_of_kb_hard_refusal_rate:.3f} | "
                f"{result.in_kb_answer_rate:.3f}/"
                f"{result.in_kb_review_rate:.3f}/"
                f"{result.in_kb_hard_refusal_rate:.3f}"
            )

        print("\n=== 相似度分布 ===")
        for sample in samples:
            item = sample.item
            tag = "库内" if item.in_kb else "库外"
            value = (
                f"{sample.max_vector_similarity:.3f}"
                if sample.max_vector_similarity is not None
                else f"无向量分数（候选 {sample.candidate_count}）"
            )
            print(f"  [{item.library}/{tag}] {value}  {item.question}")

        profile = stored_embedding_profile(db)
        report: dict[str, object] = {
            "schema_version": 2,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "retrieval" if args.retrieval else "full",
            "dataset_version": dataset.version,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "embedding_fingerprint": profile.fingerprint if profile else None,
            "database_backend": make_url(settings.database_url).get_backend_name(),
            "query_vector_count": len(query_vectors),
            "configured_refusal_threshold": settings.refusal_similarity_threshold,
            "configured_answer_threshold": settings.answer_similarity_threshold,
            "top_k": settings.retrieval_top_k if args.top_k is None else args.top_k,
            "library_count": len(dataset.libraries),
            "document_count": dataset.document_count,
            "item_count": len(dataset.items),
            "retrieval": _retrieval_payload(retrieval_metrics),
            "refusal": _refusal_payload(refusal_metrics) if refusal_metrics else None,
            "threshold_pairs": [
                {
                    "refusal_threshold": refusal_threshold,
                    "answer_threshold": answer_threshold,
                    **_refusal_payload(result),
                }
                for refusal_threshold, answer_threshold, result in threshold_results
            ],
            "similarities": [
                {
                    "library": sample.item.library,
                    "difficulty": sample.item.difficulty,
                    "in_kb": sample.item.in_kb,
                    "question": sample.item.question,
                    "candidate_count": sample.candidate_count,
                    "max_vector_similarity": sample.max_vector_similarity,
                }
                for sample in samples
            ],
        }
        if args.report is not None:
            _write_report(args.report, report)
        if args.reference_report is not None:
            reference_path = (
                args.reference_report
                if args.reference_report.is_absolute()
                else PROJECT_ROOT / args.reference_report
            )
            try:
                comparison = compare_reports(load_report(reference_path), report)
            except EvaluationGateError as exc:
                print(f"\n[评估迁移门] 失败：{exc}", file=sys.stderr)
                return 2
            print(f"\n[评估迁移门] {comparison.summary()}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
