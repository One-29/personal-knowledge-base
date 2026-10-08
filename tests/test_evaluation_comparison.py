"""PostgreSQL → SQLite 评估报告可比性与质量门。"""

import copy

import pytest

from eval.comparison import EvaluationGateError, compare_reports


def _report(*, recall: float = 1.0, mrr: float = 0.98) -> dict:
    return {
        "schema_version": 2,
        "dataset_version": 2,
        "embedding_model": "BAAI/bge-m3",
        "embedding_dimension": 1024,
        "query_vector_count": 60,
        "configured_refusal_threshold": 0.45,
        "configured_answer_threshold": 0.55,
        "top_k": 8,
        "library_count": 5,
        "document_count": 20,
        "item_count": 60,
        "threshold_pairs": [
            {
                "refusal_threshold": 0.45,
                "answer_threshold": 0.55,
                "out_of_kb_answer_rate": 0.0,
                "in_kb_hard_refusal_rate": 0.0,
            }
        ],
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


def test_migration_gate_rejects_out_of_kb_direct_answer_regression():
    candidate = _report()
    candidate["threshold_pairs"][0]["out_of_kb_answer_rate"] = 0.1

    with pytest.raises(EvaluationGateError, match="库外问题直接回答率"):
        compare_reports(_report(), candidate)


def test_migration_gate_rejects_in_kb_hard_refusal_regression():
    candidate = _report()
    candidate["threshold_pairs"][0]["in_kb_hard_refusal_rate"] = 0.1

    with pytest.raises(EvaluationGateError, match="库内问题明确拒答率"):
        compare_reports(_report(), candidate)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("configured_refusal_threshold", "not-a-number", "已配置双阈值无效"),
        ("configured_answer_threshold", "nan", "已配置双阈值无效"),
        ("configured_answer_threshold", 0.40, "必须满足"),
    ],
)
def test_migration_gate_reports_invalid_configured_thresholds_cleanly(
    field,
    value,
    message,
):
    report = _report()
    report[field] = value

    with pytest.raises(EvaluationGateError, match=message):
        compare_reports(report, copy.deepcopy(report))


def test_migration_gate_reports_malformed_threshold_pair_cleanly():
    report = _report()
    report["threshold_pairs"][0]["refusal_threshold"] = "invalid"

    with pytest.raises(EvaluationGateError, match="threshold_pairs 条目无效"):
        compare_reports(report, copy.deepcopy(report))
