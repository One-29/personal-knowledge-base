"""SQLite 默认运行时与项目迁移产物首次导入。"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database import create_database_engine, initialize_database
from app import storage
from app.ingest_tasks import stage_task
from app.models import Chunk, Document, IngestTask, KnowledgeBase
from app.runtime import RuntimeConfigurationError, prepare_runtime
from app.runtime import project_import as project_import_module
from app.runtime.configuration import resolve_runtime_paths
from app.runtime.project_import import ProjectDataImportError, import_project_data
from app.vault import VaultStore
from app.vault.coordinator import (
    bind_managed_store,
    commit_document,
    commit_knowledge_base,
)


def _engine(path: Path):
    return create_database_engine(
        "sqlite+pysqlite:///" + path.as_posix(),
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=1,
    )


def _create_project_snapshot(root: Path) -> tuple[Path, Path, str]:
    database = root / "data" / "knowbase.db"
    storage = root / "data" / "storage"
    source_text = "alpha beta gamma"
    source_bytes = source_text.encode("utf-8")
    engine = _engine(database)
    initialize_database(engine)
    with Session(engine, expire_on_commit=False) as db:
        kb = KnowledgeBase(name="import source", description="before WAL")
        db.add(kb)
        db.flush()
        document = Document(
            kb_id=kb.id,
            title="source.md",
            file_path="",
            content_hash=hashlib.sha256(source_bytes).hexdigest(),
            status="ready",
            char_count=len(source_text),
            chunk_count=1,
            processed_at=datetime.now(timezone.utc),
        )
        db.add(document)
        db.flush()
        relative = f"{kb.id}/{document.id}.md"
        source_file = storage / relative
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(source_bytes)
        document.file_path = relative
        db.add(
            Chunk(
                doc_id=document.id,
                kb_id=kb.id,
                chunk_index=0,
                content=source_text,
                char_start=0,
                char_end=len(source_text),
                embedding=[1.0, *([0.0] * 1023)],
            )
        )
        db.commit()
    (storage / "historical" / "kept.bin").parent.mkdir(parents=True)
    (storage / "historical" / "kept.bin").write_bytes(b"historical")
    engine.dispose()
    return database, storage, relative


def test_project_snapshot_import_includes_wal_and_full_storage_tree(tmp_path):
    project = tmp_path / "project"
    source_database, source_storage, relative = _create_project_snapshot(project)
    target_database = tmp_path / "user-data" / "knowbase.db"
    target_storage = tmp_path / "user-data" / "storage"

    # 保持连接打开，确保最新已提交内容可以仍位于 WAL；backup API 必须看到它。
    live = sqlite3.connect(source_database)
    try:
        assert live.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        live.execute(
            "UPDATE knowledge_bases SET description = ? WHERE name = ?",
            ("committed in WAL", "import source"),
        )
        live.commit()

        result = import_project_data(
            source_database=source_database,
            source_storage=source_storage,
            target_database=target_database,
            target_storage=target_storage,
            embedding_dimension=1024,
        )
    finally:
        live.close()

    assert result.status == "imported"
    assert result.database is not None
    assert result.storage is not None
    assert source_database.is_file()
    assert (source_storage / relative).is_file()
    assert (target_storage / relative).read_text(encoding="utf-8") == "alpha beta gamma"
    assert (target_storage / "historical" / "kept.bin").read_bytes() == b"historical"

    target_engine = _engine(target_database)
    try:
        with target_engine.connect() as connection:
            assert connection.scalar(
                select(KnowledgeBase.description).where(
                    KnowledgeBase.name == "import source"
                )
            ) == "committed in WAL"
            assert connection.exec_driver_sql("PRAGMA quick_check").scalar_one() == "ok"
    finally:
        target_engine.dispose()

    target_hash = hashlib.sha256(target_database.read_bytes()).hexdigest()
    repeated = import_project_data(
        source_database=source_database,
        source_storage=source_storage,
        target_database=target_database,
        target_storage=target_storage,
        embedding_dimension=1024,
    )
    assert repeated.status == "target-exists"
    assert hashlib.sha256(target_database.read_bytes()).hexdigest() == target_hash


def test_failed_import_restores_empty_target_and_removes_candidates(tmp_path):
    project = tmp_path / "project"
    source_database, source_storage, relative = _create_project_snapshot(project)
    (source_storage / relative).unlink()
    target_root = tmp_path / "user-data"
    target_database = target_root / "knowbase.db"
    target_storage = target_root / "storage"
    target_storage.mkdir(parents=True)

    with pytest.raises(ProjectDataImportError, match="活动原文不存在"):
        import_project_data(
            source_database=source_database,
            source_storage=source_storage,
            target_database=target_database,
            target_storage=target_storage,
            embedding_dimension=1024,
        )

    assert not target_database.exists()
    assert target_storage.is_dir()
    assert list(target_storage.iterdir()) == []
    assert not list(target_root.glob(".*.importing*"))


def test_concurrent_target_creation_is_never_overwritten(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source_database, source_storage, _ = _create_project_snapshot(project)
    target_root = tmp_path / "user-data"
    target_database = target_root / "knowbase.db"
    target_storage = target_root / "storage"
    real_link = project_import_module.os.link

    def race_to_publish(source, target):
        if Path(target) == target_database:
            target_database.write_bytes(b"published by another process")
            raise FileExistsError("simulated concurrent publisher")
        return real_link(source, target)

    monkeypatch.setattr(project_import_module.os, "link", race_to_publish)

    with pytest.raises(ProjectDataImportError, match="concurrent publisher"):
        import_project_data(
            source_database=source_database,
            source_storage=source_storage,
            target_database=target_database,
            target_storage=target_storage,
            embedding_dimension=1024,
        )

    assert target_database.read_bytes() == b"published by another process"
    assert not target_storage.exists()
    assert not list(target_root.glob(".*.importing*"))


def test_runtime_preparation_creates_and_reuses_user_sqlite(tmp_path):
    data_dir = tmp_path / "user-data"
    config = Settings(
        data_dir=data_dir,
        database_url=None,
        storage_dir=None,
        _env_file=None,
    )
    empty_project = tmp_path / "empty-project"

    first = prepare_runtime(config=config, project_root=empty_project)
    second = prepare_runtime(config=config, project_root=empty_project)

    assert first.project_import.status == "source-missing"
    assert second.project_import.status == "target-exists"
    assert first.paths.database.is_file()
    assert first.paths.storage.is_dir()
    assert VaultStore(first.paths.storage).exists()
    with sqlite3.connect(first.paths.database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'embedding_profile_v1'"
        ).fetchone() is not None


def test_runtime_recovers_persisted_ingest_task_after_vault_sync(tmp_path):
    data_dir = tmp_path / "recover-user-data"
    config = Settings(
        data_dir=data_dir,
        database_url=None,
        storage_dir=None,
        _env_file=None,
    )
    initialized = prepare_runtime(
        config=config,
        project_root=tmp_path / "empty-project",
    )
    engine = _engine(initialized.paths.database)
    text_value = "# 恢复任务\n应用退出后继续完成索引。"
    source_bytes = text_value.encode("utf-8")
    try:
        with Session(engine, expire_on_commit=False) as db:
            store = VaultStore(initialized.paths.storage)
            bind_managed_store(db, store)
            kb = KnowledgeBase(name="持久任务恢复")
            db.add(kb)
            db.flush()
            commit_knowledge_base(db, kb)
            document = Document(
                kb_id=kb.id,
                title="recover.md",
                file_path="placeholder",
                content_hash=hashlib.sha256(source_bytes).hexdigest(),
                char_count=len(text_value),
                status="pending",
            )
            db.add(document)
            db.flush()
            candidate = storage.save_version(
                kb.id,
                document.id,
                document.ingest_version,
                document.content_hash,
                source_bytes,
                storage_root=initialized.paths.storage,
            )
            document.file_path = candidate
            document.pending_file_path = candidate
            document.pending_content_hash = document.content_hash
            document.pending_char_count = document.char_count
            stage_task(db, document)
            commit_document(db, document)
            document_id = document.id
    finally:
        engine.dispose()

    recovered = prepare_runtime(
        config=config,
        project_root=tmp_path / "empty-project",
    )

    assert recovered.ingest_recovery.recovered == 1
    assert recovered.ingest_recovery.succeeded == 1
    verify_engine = _engine(recovered.paths.database)
    try:
        with Session(verify_engine) as db:
            document = db.get(Document, document_id)
            task = db.get(IngestTask, document_id)
            assert document is not None and document.status == "ready"
            assert document.pending_file_path is None
            assert task is not None and task.status == "succeeded"
            assert task.recovery_count == 1
    finally:
        verify_engine.dispose()

    clean_restart = prepare_runtime(
        config=config,
        project_root=tmp_path / "empty-project",
    )
    assert clean_restart.ingest_recovery.recovered == 0
    assert clean_restart.ingest_recovery.reconciled == 0


def test_runtime_rejects_service_database_and_unsafe_layout(tmp_path):
    postgres = Settings(
        database_url="postgresql+psycopg://localhost/knowbase",
        storage_dir=tmp_path / "storage",
        _env_file=None,
    )
    with pytest.raises(RuntimeConfigurationError, match="普通启动只使用本地 SQLite"):
        resolve_runtime_paths(postgres)

    storage = tmp_path / "storage"
    nested_database = Settings(
        database_url="sqlite+pysqlite:///" + (storage / "knowbase.db").as_posix(),
        storage_dir=storage,
        _env_file=None,
    )
    with pytest.raises(RuntimeConfigurationError, match="不能放在原文存储目录内"):
        resolve_runtime_paths(nested_database)
