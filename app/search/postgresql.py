"""PostgreSQL 检索实现：pgvector、pg_trgm 与 LATERAL 近邻。"""

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from app.models import Chunk

from .types import GraphPair


def _kb_filter(stmt: Select, kb_id: int | None) -> Select:
    return stmt if kb_id is None else stmt.where(Chunk.kb_id == kb_id)


def search_vector(
    db: Session,
    query_vector: list[float],
    kb_id: int | None,
    limit: int,
) -> list[tuple[int, float]]:
    distance = Chunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(Chunk.id, (1 - distance).label("similarity"))
        # pgvector 索引要求 ORDER BY 只有距离表达式并直接 LIMIT。
        .order_by(distance)
        .limit(limit)
    )
    stmt = _kb_filter(stmt, kb_id)
    return [(row[0], float(row[1])) for row in db.execute(stmt).all()]


def search_keyword(
    db: Session,
    query: str,
    kb_id: int | None,
    limit: int,
    *,
    similarity_threshold: float,
) -> list[int]:
    # set_config(..., true) 等价 SET LOCAL；连接归还池后不会泄漏本次阈值。
    db.execute(
        select(func.set_config(
            "pg_trgm.similarity_threshold", str(similarity_threshold), True
        ))
    )
    stmt = (
        select(Chunk.id)
        .where(Chunk.content.bool_op("%")(query))
        .order_by(func.similarity(Chunk.content, query).desc(), Chunk.id)
        .limit(limit)
    )
    stmt = _kb_filter(stmt, kb_id)
    return list(db.scalars(stmt).all())


GRAPH_PAIRS_SQL = text("""
    WITH ranked AS (
        SELECT id, doc_id, embedding,
               ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY chunk_index, id) AS chunk_rank
        FROM chunks
        WHERE kb_id = :kb_id
    ),
    pool AS (
        SELECT id, doc_id, embedding
        FROM ranked
        ORDER BY chunk_rank, doc_id
        LIMIT :max_chunks
    )
    SELECT p.doc_id AS source_doc,
           n.doc_id AS target_doc,
           1 - (p.embedding <=> n.embedding) AS similarity
    FROM pool p
    JOIN LATERAL (
        SELECT c2.doc_id, c2.embedding
        FROM chunks c2
        WHERE c2.kb_id = :kb_id AND c2.doc_id <> p.doc_id
        ORDER BY c2.embedding <=> p.embedding
        LIMIT :top_k
    ) n ON true
    WHERE 1 - (p.embedding <=> n.embedding) >= :min_similarity
""")


def graph_pairs(
    db: Session,
    kb_id: int,
    *,
    top_k: int,
    min_similarity: float,
    max_chunks: int,
) -> list[GraphPair]:
    rows = db.execute(
        GRAPH_PAIRS_SQL,
        {
            "kb_id": kb_id,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "max_chunks": max_chunks,
        },
    ).all()
    return [
        GraphPair(
            source_doc=row.source_doc,
            target_doc=row.target_doc,
            similarity=float(row.similarity),
        )
        for row in rows
    ]
