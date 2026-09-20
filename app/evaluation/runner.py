"""批量问题向量、隔离检索、拒答与阈值扫描。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ask as ask_service
from .. import retrieval
from ..core.config import settings
from ..models import Document
from .dataset import EvalDifficulty, EvalItem
from .metrics import RefusalMetrics, RetrievalMetrics, is_hit

logger = logging.getLogger(__name__)


class EvalExecutionError(RuntimeError):
    """评估执行失败，当前结果不能作为有效基线。"""


KbScopes = int | Mapping[str, int]
QueryVectorKey = tuple[str, str]
QueryVectors = Mapping[QueryVectorKey, list[float]]


def _resolve_kb_scopes(items: list[EvalItem], scopes: KbScopes) -> dict[str, int]:
    libraries = {item.library for item in items}
    if isinstance(scopes, int):
        return {library: scopes for library in libraries}
    missing = sorted(libraries - set(scopes))
    if missing:
        raise ValueError(f"缺少评估知识库 id：{missing}")
    return {library: int(scopes[library]) for library in libraries}


def embed_eval_questions(items: list[EvalItem]) -> dict[QueryVectorKey, list[float]]:
    """一次批量向量化全部唯一问题，供 Q1 与 Q3 共用。"""
    from ..embedding import get_embedding_provider

    questions = list(dict.fromkeys(item.question for item in items))
    vectors = get_embedding_provider().embed_texts(questions)
    if len(vectors) != len(questions):
        raise EvalExecutionError(
            f"评估问题向量数量不匹配：期望 {len(questions)}，实际 {len(vectors)}。"
        )
    by_question = dict(zip(questions, vectors, strict=True))
    return {
        (item.library, item.question): by_question[item.question]
        for item in items
    }


def _query_vector(item: EvalItem, query_vectors: QueryVectors) -> list[float]:
    try:
        return query_vectors[(item.library, item.question)]
    except KeyError:
        raise ValueError(f"缺少评估问题向量：{item.library}/{item.question}") from None


def evaluate_retrieval(
    db: Session,
    items: list[EvalItem],
    kb_ids: KbScopes,
    top_k: int | None = None,
    doc_titles: dict[int, str] | None = None,
    query_vectors: QueryVectors | None = None,
) -> RetrievalMetrics:
    """按样本所属知识库检索，并计算整体与逐库指标。"""
    relevant = [
        item
        for item in items
        if item.in_kb and item.expected_doc is not None and item.expected_keywords
    ]
    scopes = _resolve_kb_scopes(relevant, kb_ids)
    if doc_titles is None:
        ids = sorted(set(scopes.values()))
        titles = dict(
            db.execute(
                select(Document.id, Document.title).where(Document.kb_id.in_(ids))
            ).all()
        )
        db.rollback()
    else:
        titles = doc_titles
    vectors = embed_eval_questions(relevant) if query_vectors is None else query_vectors
    metrics = RetrievalMetrics()
    candidate_count = settings.retrieval_top_k if top_k is None else top_k

    for item in relevant:
        vector = _query_vector(item, vectors)
        try:
            candidates = retrieval.retrieve(
                db,
                item.question,
                vector,
                scopes[item.library],
                top_k=candidate_count,
            )
        finally:
            db.rollback()
        rank = next(
            (
                candidate_rank
                for candidate_rank, candidate in enumerate(candidates, start=1)
                if is_hit(
                    item.expected_doc or "",
                    titles.get(candidate.doc_id, ""),
                    item.expected_keywords,
                    candidate.content,
                )
            ),
            None,
        )
        metrics.record(item.library, item.difficulty, rank)
    return metrics


def evaluate_refusal(
    db: Session,
    items: list[EvalItem],
    kb_ids: KbScopes,
) -> RefusalMetrics:
    """跑完整问答，统计每个检索范围的库外拒答与库内误拒。"""
    scopes = _resolve_kb_scopes(items, kb_ids)
    metrics = RefusalMetrics()
    for item in items:
        try:
            result = ask_service.answer_question(
                db,
                item.question,
                scopes[item.library],
            )
        except Exception as exc:
            db.rollback()
            raise EvalExecutionError(
                f"评估问题执行失败，结果未写入基线："
                f"{item.library}/{item.question}（{type(exc).__name__}: {exc}）"
            ) from exc
        db.rollback()
        metrics.record(
            item.library,
            item.difficulty,
            in_kb=item.in_kb,
            refused=result.refused,
            reason=result.refusal_reason,
        )
    return metrics


def collect_similarities(
    db: Session,
    items: list[EvalItem],
    kb_ids: KbScopes,
    query_vectors: QueryVectors | None = None,
) -> list[tuple[EvalItem, float | None]]:
    """按知识库收集最高向量相似度，不调用回答模型。"""
    scopes = _resolve_kb_scopes(items, kb_ids)
    vectors = embed_eval_questions(items) if query_vectors is None else query_vectors
    samples: list[tuple[EvalItem, float | None]] = []
    for item in items:
        vector = _query_vector(item, vectors)
        try:
            candidates = retrieval.retrieve(
                db,
                item.question,
                vector,
                scopes[item.library],
                top_k=settings.retrieval_top_k,
            )
        finally:
            db.rollback()
        similarities = [
            candidate.vector_similarity
            for candidate in candidates
            if candidate.vector_similarity is not None
        ]
        samples.append((item, max(similarities) if similarities else None))
    return samples


def simulate_thresholds(
    samples: list[tuple[EvalItem, float | None]],
    thresholds: list[float],
) -> list[tuple[float, RefusalMetrics]]:
    """按 τ 本地模拟 L1 拒答；无候选或最高相似度低于阈值即拒答。"""
    results: list[tuple[float, RefusalMetrics]] = []
    for threshold in thresholds:
        metrics = RefusalMetrics()
        for item, similarity in samples:
            refused = similarity is None or similarity < threshold
            metrics.record(
                item.library,
                item.difficulty,
                in_kb=item.in_kb,
                refused=refused,
                reason="low_relevance(l1)" if refused else None,
            )
        results.append((threshold, metrics))
    return results
