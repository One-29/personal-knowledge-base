"""检索编排层（M3）：向量通道 + 关键词通道 + RRF 合并。

设计要点：
- 方言实现位于 ``app.search``：PostgreSQL 使用 pgvector/pg_trgm，SQLite 使用
  JSON 小规模余弦扫描/FTS5 trigram；
- 合并：RRF（Reciprocal Rank Fusion，k=60）——只吃排名不吃原始分，免权重标定。

本层只负责「找到候选块」，不生成回答（那属 M3 的生成层）。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .core.config import settings
from .models import Chunk
from .search import search_keyword as _backend_search_keyword
from .search import search_vector as _backend_search_vector

# RRF 平滑常数（业界惯用 60：压制头部排名的绝对优势，让两通道都能贡献）
RRF_K = 60


@dataclass(frozen=True)
class RetrievedChunk:
    """检索结果：块 + 融合分数 + 向量相似度（供阈值判定与引用标注）。"""

    chunk_id: int
    doc_id: int
    content: str
    char_start: int
    char_end: int
    rrf_score: float
    vector_similarity: float | None   # 1 - cosine 距离（L1 拒答判定依据）
    vector_rank: int | None           # 在向量通道中的排名（None = 未命中）
    keyword_rank: int | None          # 在关键词通道中的排名


def search_vector(
    db: Session,
    query_vector: list[float],
    kb_id: int | None,
    limit: int = 20,
) -> list[tuple[int, float]]:
    """向量通道：cosine 距离升序取 top-N。

    :return: [(chunk_id, 相似度)]，相似度 = 1 - cosine 距离（降序排列）
    """
    return _backend_search_vector(db, query_vector, kb_id, limit)


def search_keyword(
    db: Session,
    query: str,
    kb_id: int | None,
    limit: int = 10,
) -> list[int]:
    """关键词通道：按当前数据库选择索引实现，返回块 id 列表。"""
    return _backend_search_keyword(
        db,
        query,
        kb_id,
        limit,
        similarity_threshold=settings.keyword_similarity_threshold,
    )


def fuse_rrf(
    vector_ids: list[int],
    keyword_ids: list[int],
    k: int = RRF_K,
) -> list[tuple[int, float, int | None, int | None]]:
    """RRF 合并：score = Σ 1/(k + rank)，rank 从 1 起。

    :return: [(chunk_id, rrf_score, vector_rank, keyword_rank)]，按分数降序
    """
    scores: dict[int, float] = {}
    v_rank: dict[int, int] = {}
    k_rank: dict[int, int] = {}

    for rank, chunk_id in enumerate(vector_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        v_rank[chunk_id] = rank
    for rank, chunk_id in enumerate(keyword_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        k_rank[chunk_id] = rank

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [(cid, score, v_rank.get(cid), k_rank.get(cid)) for cid, score in ordered]


def retrieve(
    db: Session,
    query: str,
    query_vector: list[float],
    kb_id: int | None,
    top_k: int = 8,
) -> list[RetrievedChunk]:
    """完整检索：双通道召回 → RRF 合并 → 取 top_k 候选块。

    :param top_k: 交给生成层的候选块数（向量 20 + 关键词 10 的召回量见 04 §4.2）
    """
    vector_hits = search_vector(db, query_vector, kb_id, limit=settings.vector_top_k)
    keyword_ids = search_keyword(db, query, kb_id, limit=settings.keyword_top_k)
    similarity = {cid: sim for cid, sim in vector_hits}
    fused = fuse_rrf([cid for cid, _ in vector_hits], keyword_ids)[:top_k]

    if not fused:
        return []

    chunk_ids = [cid for cid, *_ in fused]
    chunks = {
        c.id: c for c in db.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all()
    }
    return [
        RetrievedChunk(
            chunk_id=cid,
            doc_id=chunks[cid].doc_id,
            content=chunks[cid].content,
            char_start=chunks[cid].char_start,
            char_end=chunks[cid].char_end,
            rrf_score=score,
            vector_similarity=similarity.get(cid),
            vector_rank=v_rank,
            keyword_rank=k_rank,
        )
        for cid, score, v_rank, k_rank in fused
        if cid in chunks
    ]
