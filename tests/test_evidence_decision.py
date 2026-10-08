"""双阈值证据门的边界与异常分数回归。"""

from types import SimpleNamespace

import pytest

from app.evidence import EvidenceBand, classify_retrieval_evidence, classify_similarity


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        (0.449999, EvidenceBand.INSUFFICIENT),
        (0.45, EvidenceBand.BORDERLINE),
        (0.549999, EvidenceBand.BORDERLINE),
        (0.55, EvidenceBand.SUFFICIENT),
    ],
)
def test_double_threshold_boundaries_are_explicit(similarity, expected):
    result = classify_similarity(
        candidate_count=1,
        max_vector_similarity=similarity,
        refusal_threshold=0.45,
        answer_threshold=0.55,
    )

    assert result.band is expected
    assert result.max_vector_similarity == similarity


def test_empty_results_are_insufficient_but_keyword_only_candidates_need_review():
    empty = classify_similarity(
        candidate_count=0,
        max_vector_similarity=None,
        refusal_threshold=0.45,
        answer_threshold=0.55,
    )
    keyword_only = classify_retrieval_evidence(
        [SimpleNamespace(vector_similarity=None)],
        refusal_threshold=0.45,
        answer_threshold=0.55,
    )

    assert empty.band is EvidenceBand.INSUFFICIENT
    assert keyword_only.band is EvidenceBand.BORDERLINE


def test_non_finite_scores_cannot_bypass_the_answer_gate():
    result = classify_retrieval_evidence(
        [SimpleNamespace(vector_similarity=float("nan"))],
        refusal_threshold=0.45,
        answer_threshold=0.55,
    )

    assert result.band is EvidenceBand.BORDERLINE
    assert result.max_vector_similarity is None


@pytest.mark.parametrize(
    ("candidate_count", "lower", "upper"),
    [
        (-1, 0.45, 0.55),
        (True, 0.45, 0.55),
        (1, 0.55, 0.55),
        (1, 0.56, 0.55),
        (1, float("nan"), 0.55),
        (1, 0.45, float("inf")),
        (1, True, 0.55),
    ],
)
def test_invalid_decision_inputs_fail_closed(candidate_count, lower, upper):
    with pytest.raises(ValueError):
        classify_similarity(
            candidate_count=candidate_count,
            max_vector_similarity=0.5,
            refusal_threshold=lower,
            answer_threshold=upper,
        )


@pytest.mark.parametrize("similarity", [float("nan"), float("inf"), 1.01, -1.01])
def test_invalid_similarity_is_downgraded_to_manual_review(similarity):
    result = classify_similarity(
        candidate_count=1,
        max_vector_similarity=similarity,
        refusal_threshold=0.45,
        answer_threshold=0.55,
    )

    assert result.band is EvidenceBand.BORDERLINE
    assert result.max_vector_similarity is None
