"""SQLite 检索实现：FTS5 trigram + 个人库规模的余弦扫描。"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Chunk

from .types import GraphPair
from .vector_math import cosine_similarity

MAX_FTS_TERMS = 64


def search_vector(
    db: Session,
    query_vector: list[float],
    kb_id: int | None,
    limit: int,
) -> list[tuple[int, float]]:
    stmt = select(Chunk.id, Chunk.embedding)
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)

    hits = [
        (chunk_id, cosine_similarity(embedding, query_vector))
        for chunk_id, embedding in db.execute(stmt)
    ]
    hits.sort(key=lambda item: (-item[1], item[0]))
    return hits[:limit]


def search_keyword(
    db: Session,
    query: str,
    kb_id: int | None,
    limit: int,
    *,
    similarity_threshold: float,
) -> list[int]:
    """用 trigram FTS 召回；不足三字符时退回可转义的 LIKE。

    SQLite FTS5 不提供 pg_trgm 的相似度阈值，因此该参数只为统一后端契约；
    排序由 bm25 对命中的查询 trigram 数量和频率共同决定。
    """
    del similarity_threshold
    if limit <= 0 or not query.strip():
        return []

    expression = _fts_expression(query)
    params: dict[str, object] = {"limit": limit}
    kb_clause = ""
    if kb_id is not None:
        kb_clause = "AND c.kb_id = :kb_id"
        params["kb_id"] = kb_id

    if expression:
        params["expression"] = expression
        statement = text(f"""
            SELECT c.id
            FROM chunks_fts
            JOIN chunks AS c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH :expression
              {kb_clause}
            ORDER BY bm25(chunks_fts), c.id
            LIMIT :limit
        """)
    else:
        params["pattern"] = f"%{_escape_like(query.strip())}%"
        statement = text(f"""
            SELECT c.id
            FROM chunks AS c
            WHERE c.content LIKE :pattern ESCAPE '\\'
              {kb_clause}
            ORDER BY c.id
            LIMIT :limit
        """)
    return [int(row.id) for row in db.execute(statement, params)]


def graph_pairs(
    db: Session,
    kb_id: int,
    *,
    top_k: int,
    min_similarity: float,
    max_chunks: int,
) -> list[GraphPair]:
    rows = list(
        db.execute(
            select(
                Chunk.id,
                Chunk.doc_id,
                Chunk.chunk_index,
                Chunk.embedding,
            ).where(Chunk.kb_id == kb_id)
        )
    )
    pool = sorted(rows, key=lambda row: (row.chunk_index, row.doc_id, row.id))[
        :max_chunks
    ]

    pairs: list[GraphPair] = []
    for source in pool:
        neighbors = [
            (
                candidate,
                cosine_similarity(source.embedding, candidate.embedding),
            )
            for candidate in rows
            if candidate.doc_id != source.doc_id
        ]
        neighbors.sort(key=lambda item: (-item[1], item[0].id))
        for candidate, similarity in neighbors[:top_k]:
            if similarity >= min_similarity:
                pairs.append(
                    GraphPair(
                        source_doc=source.doc_id,
                        target_doc=candidate.doc_id,
                        similarity=similarity,
                    )
                )
    return pairs


def _fts_expression(query: str) -> str | None:
    """把用户文本变成纯字面 trigram OR 查询，隔离 FTS5 查询语法。"""
    literal_terms: list[str] = []
    seen: set[str] = set()
    for index in range(max(0, len(query) - 2)):
        term = query[index : index + 3]
        if "\x00" in term or term.isspace() or term in seen:
            continue
        seen.add(term)
        literal_terms.append(term)
    if len(literal_terms) > MAX_FTS_TERMS:
        # 均匀取样，避免长问题只按开头 64 个 trigram 检索而忽略结尾实体。
        last = len(literal_terms) - 1
        literal_terms = [
            literal_terms[round(index * last / (MAX_FTS_TERMS - 1))]
            for index in range(MAX_FTS_TERMS)
        ]
    terms = ['"' + term.replace('"', '""') + '"' for term in literal_terms]
    return " OR ".join(terms) or None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
