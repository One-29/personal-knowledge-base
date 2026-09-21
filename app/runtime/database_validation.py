"""SQLite 业务库与原文树的联合校验和单文件收束。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.database import initialize_database
from app.migration.report import DatabaseDigest
from app.migration.snapshot_validation import SnapshotValidator, audit_storage
from app.migration.sqlite_target import seal_sqlite, sqlite_engine, verify_sqlite
from app.models import Chunk, Document, KnowledgeBase


def validate_and_seal_database(
    database: Path,
    storage: Path,
    *,
    embedding_dimension: int,
    timeout_seconds: float,
) -> DatabaseDigest:
    """校验 schema/FK/FTS、活动原文与 chunk 锚点，再 checkpoint WAL。"""
    engine = sqlite_engine(database, timeout_seconds=timeout_seconds)
    try:
        initialize_database(engine)
        digest = verify_sqlite(engine, embedding_dimension=embedding_dimension)
        with engine.connect() as connection:
            knowledge_bases = list(
                connection.execute(select(KnowledgeBase.__table__)).mappings()
            )
            documents = list(connection.execute(select(Document.__table__)).mappings())
            chunks = list(connection.execute(select(Chunk.__table__)).mappings())
        storage_audit = audit_storage(documents, storage)
        validator = SnapshotValidator(
            knowledge_bases,
            documents,
            storage_audit.document_texts,
        )
        for chunk in chunks:
            validator.check_chunk(chunk)
        validator.finish()
        seal_sqlite(engine)
        return digest
    finally:
        engine.dispose()
