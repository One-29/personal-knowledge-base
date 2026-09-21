"""PostgreSQL → SQLite 评估报告可比性与质量门。"""

import copy

import pytest

from eval.comparison import EvaluationGateError, compare_reports


def _report(*, recall: float = 1.0, mrr: float = 0.98) -> dict:
    return {
        "dataset_version": 2,
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "query_vector_count": 60,
        "configured_refusal_threshold": 0.5,
        "top_k": 8,
        "library_count": 5,
        "document_count": 20,
        "item_count": 60,
        "retrieval": {
            "recall_at_k": recall,
            "mrr": mrr,
            "by_library": {
                "calculus": {"recall_at_k": recall, "mrr": mrr},
            },
            "by_difficulty": {
                "hard": {"recall_at_k": recall, "mrr": mrr},
            },
        },
    }


def test_migration_gate_accepts_equal_recall_and_small_mrr_drop():
    result = compare_reports(_report(), _report(mrr=0.971), max_mrr_drop=0.01)

    assert result.mrr_drop == pytest.approx(0.009)
    assert "通过" in result.summary()


def test_migration_gate_rejects_recall_regression():
    with pytest.raises(EvaluationGateError, match="recall"):
        compare_reports(_report(), _report(recall=0.98))


def test_migration_gate_rejects_excessive_mrr_regression():
    with pytest.raises(EvaluationGateError, match="MRR"):
        compare_reports(_report(), _report(mrr=0.969))


def test_migration_gate_rejects_different_model_or_dataset():
    candidate = copy.deepcopy(_report())
    candidate["embedding_model"] = "another-model"
    candidate["dataset_version"] = 3

    with pytest.raises(EvaluationGateError, match="不可比较") as error:
        compare_reports(_report(), candidate)

    assert "embedding_model" in str(error.value)
    assert "dataset_version" in str(error.value)


def test_migration_gate_rejects_reports_missing_comparability_fields():
    reference = _report()
    candidate = _report()
    reference.pop("top_k")
    candidate.pop("top_k")

    with pytest.raises(EvaluationGateError, match="缺少可比字段"):
        compare_reports(reference, candidate)


def test_migration_gate_rejects_hidden_group_regression():
    candidate = _report(mrr=0.98)
    candidate["retrieval"]["by_library"]["calculus"]["mrr"] = 0.8

    with pytest.raises(EvaluationGateError, match="by_library/calculus"):
        compare_reports(_report(), candidate)
