"""文件系统 Vault 清单、事务协调与 SQLite 全量重建。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud, embedding, ingest
from app.core.config import Settings, settings
from app.database import create_database_engine, initialize_database
from app.embedding import EmbeddingError
from app.migration.sqlite_target import remove_sqlite_files
from app.models import Chunk, Document, KnowledgeBase
from app.runtime import RuntimeInitializationError, prepare_runtime
from app.schemas import KnowledgeBaseCreate
from app.vault import VaultError, VaultSnapshot, VaultStore
from app.vault import rebuild as rebuild_module
from app.vault.models import KnowledgeBaseRecord
from app.vault.rebuild import VaultRebuildError, rebuild_database
from app.vault.sync import snapshot_database


def _config(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        database_url=None,
        storage_dir=None,
        embedding_api_key="test-key",
        _env_file=None,
    )


def _engine(config: Settings):
    assert config.database_url is not None
    return create_database_engine(
        config.database_url,
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=1,
    )


def _write_source(root: Path, relative: str, text: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _seed_vault(
    root: Path,
    *,
    with_pending: bool = False,
) -> tuple[Settings, VaultStore, int, int, str]:
    config = _config(root)
    assert config.storage_dir is not None
    engine = _engine(config)
    initialize_database(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            kb = KnowledgeBase(name="可重建高数库", description="Vault rebuild")
            db.add(kb)
            db.flush()
            document = Document(
                kb_id=kb.id,
                title="极限.md",
                file_path="",
                content_hash="0" * 64,
                char_count=0,
            )
            db.add(document)
            db.flush()
            initial_text = "# 极限\n\n数列收敛刻画无限逼近。\n"
            initial_path = f"{kb.id}/{document.id}/v1-initial.md"
            document.content_hash = _write_source(
                config.storage_dir,
                initial_path,
                initial_text,
            )
            document.file_path = initial_path
            document.char_count = len(initial_text)
            db.commit()

            ingest.process_document(
                document.id,
                db,
                config=config,
                storage_root=config.storage_dir,
            )
            db.refresh(document)
            assert document.status == ingest.STATUS_READY

            expected_text = initial_text
            if with_pending:
                expected_text = "# 连续\n\n函数连续要求极限等于函数值。\n"
                pending_path = f"{kb.id}/{document.id}/v2-pending.md"
                pending_hash = _write_source(
                    config.storage_dir,
                    pending_path,
                    expected_text,
                )
                document.ingest_version = 2
                document.pending_file_path = pending_path
                document.pending_content_hash = pending_hash
                document.pending_char_count = len(expected_text)
                document.status = "pending"
                db.commit()

            store = VaultStore(config.storage_dir)
            snapshot_database(db, store=store)
            return config, store, kb.id, document.id, expected_text
    finally:
        engine.dispose()


def test_vault_store_rejects_tampering_and_preserves_previous_atomic_file(
    tmp_path,
    monkeypatch,
):
    store = VaultStore(tmp_path / "storage")
    first = VaultSnapshot(
        knowledge_bases=(
            KnowledgeBaseRecord(
                id=1,
                name="first",
                description=None,
                created_at="2026-09-21T00:00:00Z",
                updated_at="2026-09-21T00:00:00Z",
            ),
        )
    )
    store.write(first)
    original = store.path.read_bytes()

    real_replace = rebuild_module.os.replace

    def fail_catalog_replace(source, target):
        if Path(target) == store.path:
            raise OSError("simulated atomic publication failure")
        return real_replace(source, target)

    monkeypatch.setattr("app.vault.store.os.replace", fail_catalog_replace)
    with pytest.raises(VaultError, match="原子写入"):
        store.write(VaultSnapshot())

    assert store.path.read_bytes() == original
    assert store.load(required=True) == first
    assert not list(store.root.glob(".*.tmp"))

    envelope = json.loads(original)
    envelope["payload"]["knowledge_bases"][0]["name"] = "tampered"
    store.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(VaultError, match="checksum"):
        store.load(required=True)

    envelope["format"] = True
    envelope["sha256"] = hashlib.sha256(
        json.dumps(
            envelope["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    store.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(VaultError, match="unsupported vault format"):
        store.load(required=True)


def test_malformed_catalog_entry_fails_before_creating_empty_database(tmp_path):
    root = tmp_path / "malformed-catalog"
    config = _config(root)
    assert config.storage_dir is not None
    store = VaultStore(config.storage_dir)
    store.path.mkdir(parents=True)

    with pytest.raises(RuntimeInitializationError, match="Vault 清单必须是普通文件"):
        prepare_runtime(
            config=config,
            project_root=tmp_path / "empty-project",
        )

    assert not (root / "knowbase.db").exists()


def test_daily_sqlite_crud_updates_vault_and_failed_commit_is_compensated(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path / "daily")
    assert config.database_url is not None
    assert config.storage_dir is not None
    monkeypatch.setattr(settings, "database_url", config.database_url)
    monkeypatch.setattr(settings, "storage_dir", config.storage_dir)
    engine = _engine(config)
    initialize_database(engine)
    store = VaultStore(config.storage_dir)
    try:
        with Session(engine, expire_on_commit=False) as db:
            kb = crud.create_kb(db, KnowledgeBaseCreate(name="同步库"))
            relative = f"{kb.id}/manual.md"
            text = "事务协调器同步这篇文档。"
            content_hash = _write_source(config.storage_dir, relative, text)
            document = crud.create_document(
                db,
                kb.id,
                "manual.md",
                relative,
                content_hash,
                len(text),
            )
            snapshot = store.load(required=True)
            assert snapshot is not None
            assert [item.id for item in snapshot.knowledge_bases] == [kb.id]
            assert [item.id for item in snapshot.documents] == [document.id]

            assert crud.delete_document(db, document.id)
            assert store.load(required=True).documents == ()
            assert crud.delete_kb(db, kb.id)
            assert store.load(required=True) == VaultSnapshot()

        with Session(engine, expire_on_commit=False) as db:
            real_commit = db.commit
            attempts = 0

            def fail_once():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("simulated database commit failure")
                return real_commit()

            monkeypatch.setattr(db, "commit", fail_once)
            with pytest.raises(RuntimeError, match="simulated database commit failure"):
                crud.create_kb(db, KnowledgeBaseCreate(name="不得残留"))

            assert store.load(required=True) == VaultSnapshot()
            assert db.scalar(select(KnowledgeBase).where(
                KnowledgeBase.name == "不得残留"
            )) is None
    finally:
        engine.dispose()


def test_runtime_rebuilds_deleted_database_and_promotes_pending_source(tmp_path):
    root = tmp_path / "rebuild-success"
    config, store, kb_id, doc_id, expected_text = _seed_vault(
        root,
        with_pending=True,
    )
    database = root / "knowbase.db"
    remove_sqlite_files(database)

    initialized = prepare_runtime(
        config=config,
        project_root=tmp_path / "empty-project",
    )

    assert initialized.project_import.status == "vault-rebuilt"
    assert database.is_file()
    rebuilt_snapshot = store.load(required=True)
    assert rebuilt_snapshot is not None
    rebuilt_record = rebuilt_snapshot.documents[0]
    assert rebuilt_record.pending is None
    assert rebuilt_record.active.path.endswith("v2-pending.md")

    engine = _engine(config)
    try:
        with Session(engine) as db:
            kb = db.get(KnowledgeBase, kb_id)
            document = db.get(Document, doc_id)
            assert kb is not None and kb.name == "可重建高数库"
            assert document is not None
            assert document.status == ingest.STATUS_READY
            assert document.pending_file_path is None
            assert document.file_path == rebuilt_record.active.path
            chunks = list(db.scalars(
                select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index)
            ))
            assert chunks
            assert any("函数连续" in chunk.content for chunk in chunks)
            assert "".join(chunk.content for chunk in chunks).strip() in expected_text
    finally:
        engine.dispose()


def test_failed_rebuild_keeps_catalog_and_target_absent(tmp_path, monkeypatch):
    root = tmp_path / "rebuild-failure"
    config, store, _kb_id, _doc_id, _text = _seed_vault(root)
    database = root / "knowbase.db"
    remove_sqlite_files(database)
    catalog_before = store.path.read_bytes()

    class BrokenProvider:
        def embed_texts(self, _texts):
            raise EmbeddingError("simulated provider outage")

    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: BrokenProvider(),
    )

    with pytest.raises(VaultRebuildError, match="未能重建为 ready"):
        rebuild_database(database, store=store, config=config)

    assert not database.exists()
    assert store.path.read_bytes() == catalog_before
    assert not list(root.glob(".*.rebuilding*"))


def test_concurrent_rebuild_target_is_never_removed(tmp_path, monkeypatch):
    root = tmp_path / "rebuild-race"
    config, store, _kb_id, _doc_id, _text = _seed_vault(root)
    database = root / "knowbase.db"
    remove_sqlite_files(database)
    real_link = rebuild_module.os.link

    def race_to_publish(source, target):
        if Path(target) == database:
            database.write_bytes(b"published by another process")
            raise FileExistsError("simulated concurrent publisher")
        return real_link(source, target)

    monkeypatch.setattr(rebuild_module.os, "link", race_to_publish)
    with pytest.raises(VaultRebuildError, match="concurrent publisher"):
        rebuild_database(database, store=store, config=config)

    assert database.read_bytes() == b"published by another process"
    assert not list(root.glob(".*.rebuilding*"))
