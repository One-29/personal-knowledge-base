"""已登记原文的外部编辑检测、增量重建与中断续接。"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import embedding, ingest, package_storage
from app.core.config import Settings, settings
from app.database import create_database_engine, initialize_database
from app.document_io import prepare_upload
from app.embedding import EmbeddingError
from app.models import Chunk, Document, KnowledgeBase
from app.runtime import RuntimeInitializationError, prepare_runtime
from app.vault.external_sync import (
    ExternalSourceSyncError,
    reconcile_external_sources,
)
from app.vault.store import VaultStore
from app.vault.sync import snapshot_database


@dataclass(frozen=True)
class _Harness:
    config: Settings
    store: VaultStore
    document_ids: tuple[int, ...]
    source_paths: tuple[Path, ...]


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


def _seed_plain_vault(root: Path, *, documents: int = 1) -> _Harness:
    config = _config(root)
    assert config.storage_dir is not None
    engine = _engine(config)
    initialize_database(engine)
    document_ids: list[int] = []
    source_paths: list[Path] = []
    try:
        with Session(engine, expire_on_commit=False) as db:
            kb = KnowledgeBase(name="外部编辑测试库", description="external sync")
            db.add(kb)
            db.flush()
            for index in range(1, documents + 1):
                text = f"# 第 {index} 篇\n\n这是第 {index} 篇原始内容。\n"
                data = text.encode("utf-8")
                document = Document(
                    kb_id=kb.id,
                    title=f"note-{index}.md",
                    file_path="",
                    content_hash=hashlib.sha256(data).hexdigest(),
                    char_count=len(text),
                )
                db.add(document)
                db.flush()
                relative = f"{kb.id}/{document.id}/note.md"
                source = config.storage_dir / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(data)
                document.file_path = relative
                db.commit()
                ingest.process_document(
                    document.id,
                    db,
                    config=config,
                    storage_root=config.storage_dir,
                )
                db.refresh(document)
                assert document.status == ingest.STATUS_READY
                document_ids.append(document.id)
                source_paths.append(source)

            store = VaultStore(config.storage_dir)
            snapshot_database(db, store=store)
    finally:
        engine.dispose()
    return _Harness(
        config=config,
        store=store,
        document_ids=tuple(document_ids),
        source_paths=tuple(source_paths),
    )


class _RecordingProvider:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [
            [1.0, *([0.0] * (self.dimension - 1))]
            for _text in texts
        ]


def _document_state(config: Settings, doc_id: int):
    engine = _engine(config)
    try:
        with Session(engine) as db:
            document = db.get(Document, doc_id)
            assert document is not None
            chunks = list(
                db.scalars(
                    select(Chunk)
                    .where(Chunk.doc_id == doc_id)
                    .order_by(Chunk.chunk_index)
                )
            )
            return (
                document.content_hash,
                document.char_count,
                document.ingest_version,
                document.status,
                tuple((chunk.id, chunk.content) for chunk in chunks),
            )
    finally:
        engine.dispose()


def test_runtime_reindexes_only_changed_plain_document_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "changed", documents=2)
    changed_id, untouched_id = harness.document_ids
    changed_source = harness.source_paths[0]
    untouched_before = _document_state(harness.config, untouched_id)
    changed_before = _document_state(harness.config, changed_id)
    new_text = "# 第一篇\n\n外部编辑后的导数与微分内容，索引必须只重建这一篇。\n"
    changed_source.write_bytes(new_text.encode("utf-8"))

    provider = _RecordingProvider(harness.config.embedding_dimension)
    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: provider,
    )
    initialized = prepare_runtime(
        config=harness.config,
        project_root=tmp_path / "empty-project",
    )

    assert initialized.external_sync.checked == 2
    assert initialized.external_sync.reindexed == 1
    assert initialized.external_sync.resumed == 0
    assert len(provider.calls) == 1
    assert "外部编辑后的导数" in "".join(provider.calls[0])

    changed_after = _document_state(harness.config, changed_id)
    untouched_after = _document_state(harness.config, untouched_id)
    assert changed_after[0] == hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    assert changed_after[1] == len(new_text)
    assert changed_after[2] == changed_before[2] + 1
    assert any("外部编辑后的导数" in content for _id, content in changed_after[4])
    assert untouched_after == untouched_before

    catalog = harness.store.load(required=True)
    assert catalog is not None
    changed_record = next(item for item in catalog.documents if item.id == changed_id)
    assert changed_record.active.content_hash == changed_after[0]
    assert changed_record.ingest_version == changed_after[2]

    second = prepare_runtime(
        config=harness.config,
        project_root=tmp_path / "empty-project",
    )
    assert second.external_sync.reindexed == 0
    assert second.external_sync.resumed == 0
    assert len(provider.calls) == 1
    assert _document_state(harness.config, changed_id) == changed_after


def test_external_embedding_wait_does_not_keep_database_transaction_open(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "released-transaction")
    harness.source_paths[0].write_text(
        "# 外部修改\n\n远程模型等待前必须归还数据库连接。\n",
        encoding="utf-8",
    )
    engine = _engine(harness.config)
    try:
        with Session(engine, expire_on_commit=False) as db:
            class _TransactionCheckingProvider:
                def embed_texts(self, texts):
                    assert not db.in_transaction()
                    return [
                        [
                            1.0,
                            *([0.0] * (harness.config.embedding_dimension - 1)),
                        ]
                        for _text in texts
                    ]

            monkeypatch.setattr(
                embedding,
                "get_embedding_provider",
                lambda _config=None: _TransactionCheckingProvider(),
            )
            snapshot = harness.store.load(required=True)
            assert snapshot is not None
            _updated, report = reconcile_external_sources(
                db,
                snapshot,
                store=harness.store,
                config=harness.config,
            )
    finally:
        engine.dispose()

    assert report.reindexed == 1


def test_mtime_only_change_refreshes_vault_without_embedding_or_chunk_rewrite(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "mtime")
    doc_id = harness.document_ids[0]
    source = harness.source_paths[0]
    before_state = _document_state(harness.config, doc_id)
    before_catalog = harness.store.load(required=True)
    assert before_catalog is not None
    old_source_record = before_catalog.documents[0].active

    stat = source.stat()
    os.utime(
        source,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
    )

    class _ForbiddenProvider:
        def embed_texts(self, _texts):
            raise AssertionError("mtime-only refresh must not call embedding")

    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: _ForbiddenProvider(),
    )
    initialized = prepare_runtime(
        config=harness.config,
        project_root=tmp_path / "empty-project",
    )

    assert initialized.external_sync.refreshed == 1
    assert initialized.external_sync.reindexed == 0
    assert _document_state(harness.config, doc_id) == before_state
    refreshed = harness.store.load(required=True)
    assert refreshed is not None
    assert refreshed.documents[0].active.content_hash == old_source_record.content_hash
    assert refreshed.documents[0].active.mtime_ns == source.stat().st_mtime_ns
    assert refreshed.documents[0].active.mtime_ns != old_source_record.mtime_ns


def test_embedding_failure_preserves_old_database_and_catalog_until_retry(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "provider-failure")
    doc_id = harness.document_ids[0]
    before_state = _document_state(harness.config, doc_id)
    before_catalog = harness.store.path.read_bytes()
    new_text = "# 新内容\n\n模型暂时不可用时不能推进数据库或 Vault。\n"
    harness.source_paths[0].write_bytes(new_text.encode("utf-8"))

    class _BrokenProvider:
        def embed_texts(self, _texts):
            raise EmbeddingError("simulated outage")

    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: _BrokenProvider(),
    )
    with pytest.raises(RuntimeInitializationError, match="暂时无法向量化"):
        prepare_runtime(
            config=harness.config,
            project_root=tmp_path / "empty-project",
        )

    assert _document_state(harness.config, doc_id) == before_state
    assert harness.store.path.read_bytes() == before_catalog

    provider = _RecordingProvider(harness.config.embedding_dimension)
    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: provider,
    )
    recovered = prepare_runtime(
        config=harness.config,
        project_root=tmp_path / "empty-project",
    )
    assert recovered.external_sync.reindexed == 1
    assert _document_state(harness.config, doc_id)[0] == hashlib.sha256(
        new_text.encode("utf-8")
    ).hexdigest()


def test_source_change_during_embedding_never_pairs_stale_vectors_with_new_text(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "mid-sync-change")
    doc_id = harness.document_ids[0]
    source = harness.source_paths[0]
    before_state = _document_state(harness.config, doc_id)
    before_catalog = harness.store.path.read_bytes()
    source.write_text(
        "# 第一次编辑\n\n这份文本用于生成即将过期的向量。\n",
        encoding="utf-8",
    )

    class _ChangingProvider:
        def embed_texts(self, texts):
            source.write_text(
                "# 第二次编辑\n\n模型调用期间保存了更新版本。\n",
                encoding="utf-8",
            )
            return [
                [1.0, *([0.0] * (harness.config.embedding_dimension - 1))]
                for _text in texts
            ]

    monkeypatch.setattr(
        embedding,
        "get_embedding_provider",
        lambda _config=None: _ChangingProvider(),
    )
    with pytest.raises(RuntimeInitializationError, match="同步期间再次发生变化"):
        prepare_runtime(
            config=harness.config,
            project_root=tmp_path / "empty-project",
        )

    assert _document_state(harness.config, doc_id) == before_state
    assert harness.store.path.read_bytes() == before_catalog


def test_catalog_ahead_of_failed_database_commit_is_resumed_without_version_jump(
    tmp_path,
    monkeypatch,
):
    harness = _seed_plain_vault(tmp_path / "resume")
    doc_id = harness.document_ids[0]
    before_state = _document_state(harness.config, doc_id)
    new_text = "# 可续接修改\n\nVault 已发布而数据库提交失败时，下次启动继续。\n"
    harness.source_paths[0].write_bytes(new_text.encode("utf-8"))

    engine = _engine(harness.config)
    try:
        with Session(engine, expire_on_commit=False) as db:
            snapshot = harness.store.load(required=True)
            assert snapshot is not None
            real_commit = db.commit
            attempts = 0

            def fail_once():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("simulated commit interruption")
                return real_commit()

            monkeypatch.setattr(db, "commit", fail_once)
            with pytest.raises(ExternalSourceSyncError, match="下次启动会继续"):
                reconcile_external_sources(
                    db,
                    snapshot,
                    store=harness.store,
                    config=harness.config,
                )
    finally:
        engine.dispose()

    # Vault 已成为新真相，SQLite 仍完整保留旧索引。
    ahead = harness.store.load(required=True)
    assert ahead is not None
    assert ahead.documents[0].ingest_version == before_state[2] + 1
    assert ahead.documents[0].active.content_hash == hashlib.sha256(
        new_text.encode("utf-8")
    ).hexdigest()
    assert _document_state(harness.config, doc_id) == before_state

    resumed = prepare_runtime(
        config=harness.config,
        project_root=tmp_path / "empty-project",
    )
    after_state = _document_state(harness.config, doc_id)
    assert resumed.external_sync.resumed == 1
    assert resumed.external_sync.reindexed == 0
    assert after_state[2] == before_state[2] + 1
    assert after_state[0] == ahead.documents[0].active.content_hash


def test_missing_registered_source_fails_closed_without_deleting_metadata(tmp_path):
    harness = _seed_plain_vault(tmp_path / "missing")
    doc_id = harness.document_ids[0]
    before_state = _document_state(harness.config, doc_id)
    before_catalog = harness.store.path.read_bytes()
    harness.source_paths[0].unlink()

    with pytest.raises(RuntimeInitializationError, match="不会猜测外部删除或重命名"):
        prepare_runtime(
            config=harness.config,
            project_root=tmp_path / "empty-project",
        )

    assert _document_state(harness.config, doc_id) == before_state
    assert harness.store.path.read_bytes() == before_catalog


def test_direct_package_edit_requires_zip_reupload(tmp_path, monkeypatch):
    root = tmp_path / "package"
    config = _config(root)
    assert config.storage_dir is not None
    monkeypatch.setattr(settings, "storage_dir", config.storage_dir)
    engine = _engine(config)
    initialize_database(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            kb = KnowledgeBase(name="图片包测试库")
            db.add(kb)
            db.flush()
            document = Document(
                kb_id=kb.id,
                title="chapter.md",
                file_path="",
                content_hash="0" * 64,
                char_count=0,
            )
            db.add(document)
            db.flush()

            image_buffer = io.BytesIO()
            Image.new("RGB", (2, 2), (20, 80, 140)).save(
                image_buffer,
                format="PNG",
            )
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(
                archive_buffer,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("chapter.md", "# 图示\n\n![示例](image.png)\n")
                archive.writestr("image.png", image_buffer.getvalue())
            prepared = prepare_upload("chapter.zip", archive_buffer.getvalue())
            source_path = package_storage.save_package(
                kb.id,
                document.id,
                1,
                prepared,
            )
            document.file_path = source_path
            document.content_hash = prepared.content_hash
            document.char_count = len(prepared.text)
            db.commit()
            ingest.process_document(
                document.id,
                db,
                config=config,
                storage_root=config.storage_dir,
            )
            db.refresh(document)
            assert document.status == ingest.STATUS_READY
            store = VaultStore(config.storage_dir)
            snapshot_database(db, store=store)
            doc_id = document.id
            source = config.storage_dir / source_path
    finally:
        engine.dispose()

    before_state = _document_state(config, doc_id)
    before_catalog = store.path.read_bytes()
    source.write_text(source.read_text(encoding="utf-8") + "\n外部改动\n", encoding="utf-8")

    with pytest.raises(RuntimeInitializationError, match="必须通过 ZIP 重传"):
        prepare_runtime(
            config=config,
            project_root=tmp_path / "empty-project",
        )

    assert _document_state(config, doc_id) == before_state
    assert store.path.read_bytes() == before_catalog
