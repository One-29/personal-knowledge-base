"""从 Vault 清单和原文构建新的 SQLite 候选库。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import ingest
from app.core.config import Settings, settings
from app.database import UnsupportedSchemaVersion, initialize_database
from app.migration.errors import IntegrityError
from app.migration.report import DatabaseDigest
from app.migration.sqlite_target import (
    remove_sqlite_files,
    sqlite_engine,
    verify_published_database,
)
from app.models import Document, KnowledgeBase
from app.runtime.database_validation import validate_and_seal_database

from .models import VaultSnapshot
from .store import VaultError, VaultStore
from .sync import verify_snapshot_sources


class VaultRebuildError(RuntimeError):
    """Vault 未能完整重建并发布为 SQLite。"""


@dataclass(frozen=True)
class VaultRebuild:
    knowledge_bases: int
    documents: int
    database: DatabaseDigest


def rebuild_database(
    target_database: Path,
    *,
    store: VaultStore | None = None,
    config: Settings = settings,
) -> VaultRebuild:
    """构建候选、逐文档重建向量并验真；目标必须不存在。"""
    catalog = store or VaultStore()
    target = target_database.expanduser().resolve()
    candidate = target.with_name(f".{target.name}.{uuid4().hex}.rebuilding")
    candidate_engine = None
    target_created = False
    success = False
    try:
        if target.exists() or _sidecars(target):
            raise VaultRebuildError(f"重建目标已存在或带事务侧文件：{target}")
        snapshot = catalog.load(required=True)
        assert snapshot is not None
        verify_snapshot_sources(snapshot, store=catalog)
        rebuilt_snapshot = snapshot.with_pending_sources_promoted()

        target.parent.mkdir(parents=True, exist_ok=True)
        candidate_engine = sqlite_engine(
            candidate,
            timeout_seconds=config.db_pool_timeout,
        )
        initialize_database(candidate_engine)
        _restore_metadata_and_index(
            candidate_engine,
            rebuilt_snapshot,
            storage_root=catalog.root,
            config=config,
        )
        candidate_engine.dispose()
        candidate_engine = None

        digest = validate_and_seal_database(
            candidate,
            catalog.root,
            embedding_dimension=config.embedding_dimension,
            timeout_seconds=config.db_pool_timeout,
        )
        # pending 候选已完整重建并验真后，先推进文件真相，再发布派生库。
        # 若进程在两步之间退出，下次启动仍可从推进后的清单安全重建。
        catalog.write(rebuilt_snapshot)
        os.link(candidate, target)
        target_created = True
        candidate.unlink()
        verify_published_database(target, digest, config.embedding_dimension)
        result = VaultRebuild(
            knowledge_bases=len(rebuilt_snapshot.knowledge_bases),
            documents=len(rebuilt_snapshot.documents),
            database=digest,
        )
        success = True
        return result
    except VaultRebuildError:
        raise
    except (
        IntegrityError,
        OSError,
        SQLAlchemyError,
        sqlite3.Error,
        UnsupportedSchemaVersion,
        VaultError,
    ) as exc:
        raise VaultRebuildError(f"Vault → SQLite 重建失败：{exc}") from exc
    finally:
        if candidate_engine is not None:
            candidate_engine.dispose()
        remove_sqlite_files(candidate)
        if target_created and not success:
            remove_sqlite_files(target)


def _restore_metadata_and_index(
    engine,
    snapshot: VaultSnapshot,
    *,
    storage_root: Path,
    config: Settings,
) -> None:
    with Session(engine, expire_on_commit=False) as db:
        for record in snapshot.knowledge_bases:
            db.add(KnowledgeBase(
                id=record.id,
                name=record.name,
                description=record.description,
                created_at=record.created_at,
                updated_at=record.updated_at,
            ))
        db.flush()
        for record in snapshot.documents:
            db.add(Document(
                id=record.id,
                kb_id=record.kb_id,
                title=record.title,
                file_path=record.active.path,
                content_hash=record.active.content_hash,
                ingest_version=record.ingest_version,
                pending_file_path=None,
                pending_content_hash=None,
                pending_char_count=None,
                status="pending",
                char_count=record.active.char_count,
                chunk_count=0,
                created_at=record.created_at,
                updated_at=record.updated_at,
            ))
        db.commit()

        for record in snapshot.documents:
            ingest.process_document(
                record.id,
                db,
                config=config,
                storage_root=storage_root,
            )
            rebuilt = db.get(Document, record.id)
            if rebuilt is None or rebuilt.status != ingest.STATUS_READY:
                detail = rebuilt.last_error_message if rebuilt is not None else "文档消失"
                raise VaultRebuildError(
                    f"文档 {record.id} 未能重建为 ready：{detail or '未知错误'}"
                )
            # 入库会刷新 updated_at；用户元数据时间属于 Vault，需要恢复原值。
            rebuilt.created_at = record.created_at
            rebuilt.updated_at = record.updated_at
            db.commit()


def _sidecars(path: Path) -> list[Path]:
    return [
        candidate
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(path) + suffix)).exists()
    ]
