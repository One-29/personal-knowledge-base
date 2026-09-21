"""检索存储后端分派。

业务层只依赖这里的稳定接口；PostgreSQL 和 SQLite 的 SQL/计算细节分别
收敛在独立模块中。
"""

from sqlalchemy.orm import Session

from app.database import dialect_name

from . import postgresql, sqlite
from .types import GraphPair


def _backend(db: Session):
    name = dialect_name(db)
    if name == "sqlite":
        return sqlite
    if name == "postgresql":
        return postgresql
    raise RuntimeError(f"不支持的数据库方言：{name}")


def search_vector(
    db: Session,
    query_vector: list[float],
    kb_id: int | None,
    limit: int,
) -> list[tuple[int, float]]:
    return _backend(db).search_vector(db, query_vector, kb_id, limit)


def search_keyword(
    db: Session,
    query: str,
    kb_id: int | None,
    limit: int,
    *,
    similarity_threshold: float,
) -> list[int]:
    return _backend(db).search_keyword(
        db,
        query,
        kb_id,
        limit,
        similarity_threshold=similarity_threshold,
    )


def graph_pairs(
    db: Session,
    kb_id: int,
    *,
    top_k: int,
    min_similarity: float,
    max_chunks: int,
) -> list[GraphPair]:
    return _backend(db).graph_pairs(
        db,
        kb_id,
        top_k=top_k,
        min_similarity=min_similarity,
        max_chunks=max_chunks,
    )


__all__ = ["GraphPair", "graph_pairs", "search_keyword", "search_vector"]
