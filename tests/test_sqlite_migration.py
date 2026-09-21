"""PostgreSQL → SQLite：一致性快照、验真、发布和失败保护。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.embedding_profile import EmbeddingProfile, PROFILE_KEY
from app.migration import MigrationError, migrate_postgresql_to_sqlite
from app.migration.errors import IntegrityError
from app.migration.snapshot_validation import audit_storage
from app.models import AppMetadata, Chunk, Document, KnowledgeBase


def _sqlite_engine(path):
    return create_database_engine(
        "sqlite+pysqlite:///" + path.as_posix(),
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=1,
    )


def test_postgresql_snapshot_is_atomically_migrated_and_verified(db, tmp_path):
    source_engine = db.get_bind().engine
    storage_root = tmp_path / "storage"
    target = tmp_path / "knowbase.db"
    report_path = tmp_path / "migration-report.json"
    profile = EmbeddingProfile(
        base_url="https://credentials-must-not-leak.example.invalid/v1",
        model="migration-test-model",
        dimension=1024,
    )
    source_text = "alpha beta gamma"
    source_bytes = source_text.encode("utf-8")
    original_profile: str | None = None
    kb_id: int | None = None

    with Session(source_engine, expire_on_commit=False) as source:
        existing = source.get(AppMetadata, PROFILE_KEY)
        original_profile = existing.value if existing is not None else None
        kb = KnowledgeBase(name="SQLite migration integration")
        source.add(kb)
        source.flush()
        doc = Document(
            kb_id=kb.id,
            title="migration.md",
            file_path="",
            content_hash=hashlib.sha256(source_bytes).hexdigest(),
            ingest_version=1,
            status="ready",
            char_count=len(source_text),
            chunk_count=2,
            processed_at=datetime.now(timezone.utc),
        )
        source.add(doc)
        source.flush()
        rel_path = f"{kb.id}/{doc.id}/v1-{doc.content_hash[:16]}.md"
        path = storage_root / rel_path
        path.parent.mkdir(parents=True)
        path.write_bytes(source_bytes)
        doc.file_path = rel_path
        source.add_all([
            Chunk(
                doc_id=doc.id,
                kb_id=kb.id,
                chunk_index=0,
                content=source_text[0:10],
                char_start=0,
                char_end=10,
                embedding=[1.0, *([0.0] * 1023)],
            ),
            Chunk(
                doc_id=doc.id,
                kb_id=kb.id,
                chunk_index=1,
                content=source_text[6:16],
                char_start=6,
                char_end=16,
                embedding=[0.0, 1.0, *([0.0] * 1022)],
            ),
        ])
        source.commit()
        kb_id = kb.id

    try:
        report = migrate_postgresql_to_sqlite(
            source_engine,
            target,
            storage_root,
            current_profile=profile,
        )
        report.write(report_path)

        assert target.is_file()
        assert report.database.counts == {
            "app_metadata": 1,
            "knowledge_bases": 1,
            "documents": 1,
            "chunks": 2,
        }
        assert report.files.documents == 1
        assert report.files.files == 1
        assert report.embedding.fingerprint == profile.fingerprint
        serialized_report = report_path.read_text(encoding="utf-8")
        assert "credentials-must-not-leak" not in serialized_report
        assert profile.base_url not in serialized_report

        migrated_engine = _sqlite_engine(target)
        try:
            with Session(migrated_engine) as migrated:
                migrated_doc = migrated.scalar(select(Document))
                assert migrated_doc is not None
                assert migrated_doc.id == doc.id
                assert migrated_doc.file_path == rel_path
                chunks = migrated.scalars(
                    select(Chunk).order_by(Chunk.chunk_index)
                ).all()
                assert [item.content for item in chunks] == [
                    source_text[0:10],
                    source_text[6:16],
                ]
                assert chunks[0].embedding == [1.0, *([0.0] * 1023)]
            with migrated_engine.connect() as connection:
                assert connection.scalar(text(
                    "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'alpha'"
                )) == 1
                assert connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall() == []
        finally:
            migrated_engine.dispose()

        original_target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        with pytest.raises(MigrationError, match="已存在"):
            migrate_postgresql_to_sqlite(
                source_engine,
                target,
                storage_root,
                current_profile=profile,
            )
        assert hashlib.sha256(target.read_bytes()).hexdigest() == original_target_hash

        replacement = migrate_postgresql_to_sqlite(
            source_engine,
            target,
            storage_root,
            current_profile=profile,
            replace_existing=True,
        )
        assert replacement.backup_database is not None
        assert Path(replacement.backup_database).is_file()

        verified_target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        path.write_bytes(b"tampered")
        with pytest.raises(MigrationError, match="摘要不匹配"):
            migrate_postgresql_to_sqlite(
                source_engine,
                target,
                storage_root,
                current_profile=profile,
                replace_existing=True,
            )
        assert hashlib.sha256(target.read_bytes()).hexdigest() == verified_target_hash
        assert not list(tmp_path.glob(f".{target.name}.*.migrating*"))
    finally:
        if kb_id is not None:
            with source_engine.begin() as connection:
                connection.execute(
                    delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
                )
                if original_profile is None:
                    connection.execute(
                        delete(AppMetadata).where(AppMetadata.key == PROFILE_KEY)
                    )
                else:
                    connection.execute(
                        AppMetadata.__table__.update()
                        .where(AppMetadata.key == PROFILE_KEY)
                        .values(value=original_profile)
                    )


def test_storage_audit_rejects_inflight_document(tmp_path):
    with pytest.raises(IntegrityError, match="pending"):
        audit_storage(
            [{
                "id": 1,
                "kb_id": 1,
                "status": "pending",
                "pending_file_path": "1/1/candidate.md",
                "file_path": "",
                "content_hash": "x" * 64,
                "char_count": 0,
                "chunk_count": 0,
                "ingest_version": 1,
            }],
            tmp_path,
        )


def test_storage_audit_rejects_stale_pending_metadata(tmp_path):
    with pytest.raises(IntegrityError, match="候选原文状态"):
        audit_storage(
            [{
                "id": 1,
                "kb_id": 1,
                "status": "failed",
                "pending_file_path": None,
                "pending_content_hash": "x" * 64,
                "pending_char_count": None,
                "file_path": "",
                "content_hash": "x" * 64,
                "char_count": 0,
                "chunk_count": 0,
                "ingest_version": 1,
            }],
            tmp_path,
        )


def test_migration_report_is_valid_json(tmp_path):
    """报告值对象本身只使用 JSON 基本类型。"""
    # 使用一个小的现有值，避免这个约束只由 CLI 的 happy path 覆盖。
    from app.migration.report import (
        DatabaseDigest,
        EmbeddingProfileSummary,
        FileAuditSummary,
        MigrationReport,
    )

    report = MigrationReport(
        format_version=1,
        completed_at="2026-09-21T00:00:00+00:00",
        source_backend="postgresql",
        target_database="data/knowbase.db",
        backup_database=None,
        database=DatabaseDigest(
            counts={"chunks": 0},
            table_sha256={"chunks": "0" * 64},
            combined_sha256="1" * 64,
        ),
        files=FileAuditSummary(0, 0, 0, 0, 0, "2" * 64),
        embedding=EmbeddingProfileSummary("model", 3, "3" * 64),
    )
    output = tmp_path / "report.json"
    report.write(output)
    assert json.loads(output.read_text(encoding="utf-8"))["format_version"] == 1
