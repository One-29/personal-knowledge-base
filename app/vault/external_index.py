"""外部编辑后的单文档 SQLite 索引原子发布。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import chunking
from app.models import Chunk as ChunkRow
from app.models import Document

from .external_source import ExternalSourceSyncError
from .models import DocumentRecord, VaultSnapshot
from .store import VaultError, VaultStore

__all__ = ["document_fingerprint", "publish_index_update"]


def document_fingerprint(document: Document) -> tuple[object, ...]:
    """捕获所有会影响单文档索引发布的数据库状态。"""
    return (
        document.id,
        document.kb_id,
        document.title,
        document.file_path,
        document.content_hash,
        document.char_count,
        document.ingest_version,
        document.pending_file_path,
        document.pending_content_hash,
        document.pending_char_count,
        document.status,
        document.chunk_count,
        document.last_error_code,
        document.last_error_message,
        document.processed_at,
        document.created_at,
        document.updated_at,
    )


def publish_index_update(
    db: Session,
    *,
    store: VaultStore,
    snapshot: VaultSnapshot,
    expected_database: tuple[object, ...],
    target: DocumentRecord,
    text: str,
    chunks: list[chunking.Chunk],
    vectors: list[list[float]],
    embedding_dimension: int,
) -> None:
    """先推进 Vault，再在一个数据库事务内替换单篇索引。"""
    db.rollback()
    document = db.scalar(
        select(Document).where(Document.id == target.id).with_for_update()
    )
    if document is None or document_fingerprint(document) != expected_database:
        db.rollback()
        raise ExternalSourceSyncError(
            f"文档 {target.id} 在外部同步期间被另一项任务修改；请重新启动"
        )

    catalog_published = False
    try:
        store.write(snapshot)
        catalog_published = True

        db.execute(delete(ChunkRow).where(ChunkRow.doc_id == document.id))
        db.add_all(
            ChunkRow(
                doc_id=document.id,
                kb_id=document.kb_id,
                chunk_index=chunk.index,
                content=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        document.file_path = target.active.path
        document.content_hash = target.active.content_hash
        document.char_count = target.active.char_count
        document.ingest_version = target.ingest_version
        document.pending_file_path = None
        document.pending_content_hash = None
        document.pending_char_count = None
        document.chunk_count = len(chunks)
        document.status = "ready"
        document.last_error_code = None
        document.last_error_message = None
        document.processed_at = datetime.now(timezone.utc)
        document.updated_at = target.updated_at
        db.flush()
        _validate_staged_index(
            db,
            document,
            text=text,
            expected_chunks=chunks,
            embedding_dimension=embedding_dimension,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, ExternalSourceSyncError):
            if catalog_published:
                raise ExternalSourceSyncError(
                    f"文档 {target.id} 的 Vault 已记录新原文，但 SQLite 校验失败："
                    f"{exc}；原文已保留，下次启动会继续同步"
                ) from exc
            raise
        if catalog_published:
            raise ExternalSourceSyncError(
                f"文档 {target.id} 的 Vault 已记录新原文，但 SQLite 尚未完成；"
                "原文已保留，下次启动会继续同步"
            ) from exc
        if isinstance(exc, (OSError, SQLAlchemyError, VaultError)):
            raise ExternalSourceSyncError(
                f"文档 {target.id} 的外部修改无法提交：{exc}"
            ) from exc
        raise


def _validate_staged_index(
    db: Session,
    document: Document,
    *,
    text: str,
    expected_chunks: list[chunking.Chunk],
    embedding_dimension: int,
) -> None:
    persisted = list(
        db.scalars(
            select(ChunkRow)
            .where(ChunkRow.doc_id == document.id)
            .order_by(ChunkRow.chunk_index)
        )
    )
    if len(persisted) != len(expected_chunks):
        raise ExternalSourceSyncError("外部同步后的 chunk 数量不一致")
    for row, expected in zip(persisted, expected_chunks, strict=True):
        if (
            row.kb_id != document.kb_id
            or row.chunk_index != expected.index
            or row.char_start != expected.char_start
            or row.char_end != expected.char_end
            or row.content != expected.text
            or text[row.char_start : row.char_end] != row.content
            or len(row.embedding) != embedding_dimension
        ):
            raise ExternalSourceSyncError("外部同步后的 chunk 或溯源区间不一致")

    # FTS5 external-content 索引由触发器维护；在提交前同时核对索引内部一致性
    # 与总行数，失败即可回滚本次文档替换。
    db.connection().exec_driver_sql(
        "INSERT INTO chunks_fts(chunks_fts, rank) "
        "VALUES('integrity-check', 1)"
    )
    chunk_count = int(db.scalar(select(func.count()).select_from(ChunkRow)) or 0)
    fts_count = int(
        db.connection().exec_driver_sql("SELECT count(*) FROM chunks_fts").scalar_one()
    )
    if chunk_count != fts_count:
        raise ExternalSourceSyncError(
            f"外部同步后的 FTS 行数不一致：chunks={chunk_count}，fts={fts_count}"
        )
