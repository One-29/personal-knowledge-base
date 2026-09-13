"""评估指标的纯单元回归：未命中计零分，且来源文档必须匹配。"""

from unittest.mock import Mock

import pytest

from app import embedding, evaluation
from app.retrieval import RetrievedChunk


def _candidate(chunk_id: int, doc_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        content=content,
        char_start=0,
        char_end=len(content),
        rrf_score=0.1,
        vector_similarity=0.9,
        vector_rank=chunk_id,
        keyword_rank=None,
    )


def test_is_hit_requires_expected_document_and_keyword():
    assert evaluation.is_hit("tcp.md", "TCP.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "os.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "tcp.md", ["四次挥手"], "解释三次握手")


def test_mrr_includes_zero_for_misses_and_rejects_wrong_source(monkeypatch):
    items = [
        evaluation.EvalItem("问题一", "tcp.md", ["目标词"], True),
        evaluation.EvalItem("问题二", "tcp.md", ["不存在"], True),
    ]
    responses = [
        [
            _candidate(1, 2, "目标词但来源错误"),
            _candidate(2, 1, "正确来源的目标词"),
        ],
        [],
    ]

    class Provider:
        def embed_texts(self, texts):
            return [[1.0]]

    monkeypatch.setattr(embedding, "get_embedding_provider", lambda: Provider())
    monkeypatch.setattr(evaluation.retrieval, "retrieve", lambda *args, **kwargs: responses.pop(0))
    db = Mock()

    metrics = evaluation.evaluate_retrieval(
        db, items, kb_id=1, doc_titles={1: "tcp.md", 2: "os.md"}
    )

    assert metrics.total == 2
    assert metrics.hits == 1
    assert metrics.reciprocal_ranks == [0.5]
    assert metrics.recall_at_k == pytest.approx(0.5)
    assert metrics.mrr == pytest.approx(0.25)
    assert db.rollback.call_count == 2
