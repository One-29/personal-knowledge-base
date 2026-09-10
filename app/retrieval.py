"""检索层（M3）：向量通道 + 关键词通道 + RRF 合并（04 §4 DR3/DR4）。

设计要点：
- 向量通道：pgvector cosine（`<=>` 运算符，与 HNSW 索引的 vector_cosine_ops 匹配，
  否则索引失效退化为全表扫描）；
- 关键词通道：pg_trgm `similarity()`（DR4：内置扩展零部署，中文 2 字词偏弱由向量通道互补）；
- 合并：RRF（Reciprocal Rank Fusion，k=60）——只吃排名不吃原始分，免权重标定。

本层只负责「找到候选块」，不生成回答（那属 M3 的生成层）。
"""

from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from .core.config import settings
from .models import Chunk

# RRF 平滑常数（业界惯用 60：压制头部排名的绝对优势，让两通道都能贡献）
RRF_K = 60


@dataclass(frozen=True)
class RetrievedChunk:
    """检索结果：块 + 融合分数（供阈值判定与引用标注）。"""

    chunk_id: int
    doc_id: int
    content: str
    char_start: int
    char_end: int
    rrf_score: float
    vector_rank: int | None      # 在向量通道中的排名（None = 未命中）
    keyword_rank: int | None     # 在关键词通道中的排名


def _kb_filter(stmt: Select, kb_id: int | None) -> Select:
    """按知识库过滤：用 chunks.kb_id 冗余列（DM3），免 join documents。"""
    return stmt if kb_id is None else stmt.where(Chunk.kb_id == kb_id)


def search_vector(
    db: Session,
    query_vector: list[float],
    kb_id: int | None,
    limit: int = 20,
) -> list[int]:
    """向量通道：cosine 距离升序取 top-N，返回块 id 列表（按相似度降序）。"""
    stmt = select(Chunk.id).order_by(Chunk.embedding.cosine_distance(query_vector)).limit(limit)
    stmt = _kb_filter(stmt, kb_id)
    return list(db.scalars(stmt).all())


def search_keyword(
    db: Session,
    query: str,
    kb_id: int | None,
    limit: int = 10,
) -> list[int]:
    """关键词通道：pg_trgm 相似度降序取 top-N（04 DR4），返回块 id 列表。"""
    stmt = select(Chunk.id).where(
        func.similarity(Chunk.content, query) > 0
    ).order_by(func.similarity(Chunk.content, query).desc()).limit(limit)
    stmt = _kb_filter(stmt, kb_id)
    return list(db.scalars(stmt).all())


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
    vector_ids = search_vector(db, query_vector, kb_id, limit=settings.vector_top_k)
    keyword_ids = search_keyword(db, query, kb_id, limit=settings.keyword_top_k)
    fused = fuse_rrf(vector_ids, keyword_ids)[:top_k]

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
            vector_rank=v_rank,
            keyword_rank=k_rank,
        )
        for cid, score, v_rank, k_rank in fused
        if cid in chunks
    ]
