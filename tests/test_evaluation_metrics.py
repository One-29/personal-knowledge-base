"""跨库检索、拒答、难度聚合与阈值模拟回归。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import embedding, evaluation
from app.evaluation import runner as evaluation_runner
from tests.evaluation_helpers import _candidate

def test_is_hit_requires_expected_document_and_keyword():
    assert evaluation.is_hit("tcp.md", "TCP.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "os.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "tcp.md", ["四次挥手"], "解释三次握手")


def test_retrieval_metrics_use_library_scopes_and_count_misses_as_zero(monkeypatch):
    items = [
        evaluation.EvalItem(
            question="alpha-rank-two",
            expected_doc="alpha.md",
            expected_keywords=["目标词"],
            in_kb=True,
            library="alpha",
            difficulty="smoke",
        ),
        evaluation.EvalItem(
            question="alpha-miss",
            expected_doc="alpha.md",
            expected_keywords=["不存在"],
            in_kb=True,
            library="alpha",
            difficulty="regular",
        ),
        evaluation.EvalItem(
            question="beta-rank-one",
            expected_doc="beta.md",
            expected_keywords=["命中词"],
            in_kb=True,
            library="beta",
            difficulty="hard",
        ),
    ]
    responses = {
        "alpha-rank-two": [
            _candidate(1, 2, "目标词但来源错误"),
            _candidate(2, 1, "正确来源的目标词"),
        ],
        "alpha-miss": [],
        "beta-rank-one": [_candidate(3, 3, "命中词")],
    }
    expected_scopes = {"alpha": 101, "beta": 202}
    calls: list[tuple[str, int, int]] = []

    class Provider:
        def embed_texts(self, texts):
            return [[1.0] for _ in texts]

    def retrieve(_db, question, _vector, kb_id, *, top_k):
        calls.append((question, kb_id, top_k))
        return responses[question]

    monkeypatch.setattr(embedding, "get_embedding_provider", lambda: Provider())
    monkeypatch.setattr(evaluation_runner.retrieval, "retrieve", retrieve)
    db = Mock()

    metrics = evaluation.evaluate_retrieval(
        db,
        items,
        expected_scopes,
        top_k=8,
        doc_titles={1: "alpha.md", 2: "other.md", 3: "beta.md"},
    )

    assert calls == [
        ("alpha-rank-two", 101, 8),
        ("alpha-miss", 101, 8),
        ("beta-rank-one", 202, 8),
    ]
    assert metrics.total == 3
    assert metrics.hits == 2
    assert metrics.reciprocal_ranks == [0.5, 1.0]
    assert metrics.recall_at_k == pytest.approx(2 / 3)
    assert metrics.mrr == pytest.approx(0.5)
    assert metrics.by_library["alpha"].recall_at_k == pytest.approx(0.5)
    assert metrics.by_library["alpha"].mrr == pytest.approx(0.25)
    assert metrics.by_library["beta"].mrr == pytest.approx(1.0)
    assert metrics.by_difficulty["regular"].mrr == 0.0
    assert db.rollback.call_count == 3


def test_eval_question_vectors_are_deduplicated_and_reused_by_scope(monkeypatch):
    items = [
        evaluation.EvalItem("相同问题", "a.md", ["a"], True, "alpha", "smoke"),
        evaluation.EvalItem("相同问题", None, [], False, "beta", "hard"),
        evaluation.EvalItem("另一个问题", "b.md", ["b"], True, "beta", "regular"),
    ]
    provider = Mock()
    provider.embed_texts.return_value = [[1.0], [2.0]]
    monkeypatch.setattr(embedding, "get_embedding_provider", lambda: provider)

    vectors = evaluation.embed_eval_questions(items)

    provider.embed_texts.assert_called_once_with(["相同问题", "另一个问题"])
    assert vectors[("alpha", "相同问题")] == [1.0]
    assert vectors[("beta", "相同问题")] == [1.0]
    assert vectors[("beta", "另一个问题")] == [2.0]


def test_missing_library_scope_fails_before_embedding_call(monkeypatch):
    items = [
        evaluation.EvalItem(
            question="问题",
            expected_doc="alpha.md",
            expected_keywords=["目标"],
            in_kb=True,
            library="alpha",
        )
    ]
    provider_factory = Mock(side_effect=AssertionError("不应请求向量服务"))
    monkeypatch.setattr(embedding, "get_embedding_provider", provider_factory)

    with pytest.raises(ValueError, match="alpha"):
        evaluation.evaluate_retrieval(Mock(), items, {"beta": 2})

    provider_factory.assert_not_called()


def test_refusal_metrics_are_aggregated_by_library_and_difficulty(monkeypatch):
    items = [
        evaluation.EvalItem("a-in", "a.md", ["a"], True, "alpha", "smoke"),
        evaluation.EvalItem("a-out", None, [], False, "alpha", "hard"),
        evaluation.EvalItem("b-in", "b.md", ["b"], True, "beta", "regular"),
        evaluation.EvalItem("b-out", None, [], False, "beta", "hard"),
    ]
    outcomes = {
        "a-in": (False, None),
        "a-out": (True, "low_relevance(l1)"),
        "b-in": (True, "low_relevance(l1)"),
        "b-out": (False, None),
    }
    calls: list[tuple[str, int]] = []

    def answer(_db, question, kb_id):
        calls.append((question, kb_id))
        refused, reason = outcomes[question]
        return SimpleNamespace(refused=refused, refusal_reason=reason)

    monkeypatch.setattr(evaluation_runner.ask_service, "answer_question", answer)
    db = Mock()
    metrics = evaluation.evaluate_refusal(
        db,
        items,
        {"alpha": 11, "beta": 22},
    )

    assert calls == [
        ("a-in", 11),
        ("a-out", 11),
        ("b-in", 22),
        ("b-out", 22),
    ]
    assert metrics.out_of_kb_refusal_rate == pytest.approx(0.5)
    assert metrics.in_kb_false_refusal_rate == pytest.approx(0.5)
    assert metrics.by_library["alpha"].out_of_kb_refusal_rate == 1.0
    assert metrics.by_library["alpha"].in_kb_false_refusal_rate == 0.0
    assert metrics.by_library["beta"].out_of_kb_refusal_rate == 0.0
    assert metrics.by_library["beta"].in_kb_false_refusal_rate == 1.0
    assert metrics.by_difficulty["hard"].out_of_kb_total == 2
    assert metrics.by_difficulty["hard"].out_of_kb_refused == 1
    assert db.rollback.call_count == 4


def test_refusal_evaluation_fails_instead_of_publishing_partial_metrics(monkeypatch):
    item = evaluation.EvalItem(
        "会失败的问题",
        "a.md",
        ["a"],
        True,
        "alpha",
        "regular",
    )
    monkeypatch.setattr(
        evaluation_runner.ask_service,
        "answer_question",
        Mock(side_effect=RuntimeError("模型超时")),
    )
    db = Mock()

    with pytest.raises(evaluation.EvalExecutionError, match="模型超时"):
        evaluation.evaluate_refusal(db, [item], {"alpha": 1})

    db.rollback.assert_called_once()


def test_threshold_simulation_preserves_library_and_difficulty_breakdown():
    samples = [
        (
            evaluation.EvalItem(
                "a-in", "a.md", ["a"], True, "alpha", "smoke"
            ),
            0.8,
        ),
        (
            evaluation.EvalItem("a-out", None, [], False, "alpha", "hard"),
            0.2,
        ),
        (
            evaluation.EvalItem(
                "b-in", "b.md", ["b"], True, "beta", "regular"
            ),
            None,
        ),
        (
            evaluation.EvalItem("b-out", None, [], False, "beta", "hard"),
            0.6,
        ),
    ]

    [(threshold, metrics)] = evaluation.simulate_thresholds(samples, [0.5])

    assert threshold == 0.5
    assert metrics.out_of_kb_refusal_rate == pytest.approx(0.5)
    assert metrics.in_kb_false_refusal_rate == pytest.approx(0.5)
    assert metrics.by_library["alpha"].out_of_kb_refusal_rate == 1.0
    assert metrics.by_library["beta"].in_kb_false_refusal_rate == 1.0
    assert metrics.by_difficulty["hard"].out_of_kb_total == 2
