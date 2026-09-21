"""PostgreSQL 到 SQLite 的一致性快照、验真与原子发布。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import initialize_database
from app.embedding_profile import (
    EmbeddingProfile,
    EmbeddingProfileError,
    ensure_embedding_profile,
)
from app.models import Chunk

from .errors import IntegrityError, MigrationError
from .integrity import (
    TABLE_SPECS,
    DatabaseDigestBuilder,
    portable_row,
    select_rows,
    validate_model_contract,
)
from .report import EmbeddingProfileSummary, MigrationReport
from .snapshot_validation import SnapshotValidator, audit_storage
from .sqlite_target import (
    publish_candidate,
    remove_sqlite_files,
    rollback_publication,
    seal_sqlite,
    sqlite_engine,
    verify_published_database,
    verify_sqlite,
)

DEFAULT_BATCH_SIZE = 250
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0


def migrate_postgresql_to_sqlite(
    source_engine: Engine,
    target_path: Path,
    storage_root: Path,
    *,
    current_profile: EmbeddingProfile | None = None,
    replace_existing: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> MigrationReport:
    """把一个静止的 PostgreSQL 业务快照发布成独立 SQLite 文件。

    目标始终先写入同目录候选文件。数据库摘要、原文锚点、外键、FTS 与
    SQLite 自检全部通过后才执行原子替换；默认拒绝覆盖已有目标。
    """
    if source_engine.dialect.name != "postgresql":
        raise MigrationError("迁移源必须是 PostgreSQL 数据库")
    if batch_size < 1:
        raise ValueError("batch_size 必须大于 0")
    if lock_timeout_seconds <= 0:
        raise ValueError("lock_timeout_seconds 必须大于 0")

    target = target_path.expanduser().resolve()
    storage = storage_root.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace_existing:
        raise MigrationError(
            f"目标 SQLite 已存在：{target}。如需替换，请显式启用 replace_existing"
        )
    if target.is_dir():
        raise MigrationError(f"目标路径是目录，不能写入 SQLite：{target}")

    candidate = target.with_name(f".{target.name}.{uuid4().hex}.migrating")
    candidate_engine: Engine | None = None
    published = False
    backup: Path | None = None

    try:
        profile = _claim_source_profile(
            source_engine,
            current_profile or EmbeddingProfile.configured(),
        )
        candidate_engine = sqlite_engine(
            candidate,
            timeout_seconds=lock_timeout_seconds,
        )
        initialize_database(candidate_engine)

        with source_engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as source:
            with source.begin():
                _lock_source_snapshot(source, lock_timeout_seconds)
                source_digest, file_summary = _copy_locked_snapshot(
                    source,
                    candidate_engine,
                    storage,
                    profile,
                    batch_size,
                )
                target_digest = verify_sqlite(
                    candidate_engine,
                    embedding_dimension=profile.dimension,
                )
                if target_digest != source_digest:
                    raise IntegrityError(
                        "SQLite 与 PostgreSQL 的确定性摘要不一致，拒绝发布候选库"
                    )

                seal_sqlite(candidate_engine)
                candidate_engine.dispose()
                candidate_engine = None
                backup = publish_candidate(
                    candidate,
                    target,
                    replace_existing=replace_existing,
                    timeout_seconds=lock_timeout_seconds,
                )
                published = True
                try:
                    verify_published_database(
                        target,
                        source_digest,
                        profile.dimension,
                    )
                except Exception:
                    rollback_publication(target, backup)
                    published = False
                    raise

        return MigrationReport(
            format_version=1,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source_backend="postgresql",
            target_database=str(target),
            backup_database=str(backup) if backup is not None else None,
            database=source_digest,
            files=file_summary,
            embedding=EmbeddingProfileSummary(
                model=profile.model,
                dimension=profile.dimension,
                fingerprint=profile.fingerprint,
            ),
        )
    except (IntegrityError, SQLAlchemyError, OSError, sqlite3.Error) as exc:
        if published:
            raise MigrationError(
                f"SQLite 已发布，但最终复核失败：{exc}"
            ) from exc
        raise MigrationError(f"PostgreSQL → SQLite 迁移失败：{exc}") from exc
    finally:
        if candidate_engine is not None:
            candidate_engine.dispose()
        if not published:
            remove_sqlite_files(candidate)


def _claim_source_profile(
    source_engine: Engine,
    profile: EmbeddingProfile,
) -> EmbeddingProfile:
    """在只读快照前完成旧库 TOFU，保证指纹也进入同一快照。"""
    try:
        with Session(source_engine, expire_on_commit=False) as session:
            stored = ensure_embedding_profile(session, profile)
            count = int(session.scalar(select(func.count()).select_from(Chunk)) or 0)
            session.rollback()
            if count and stored.dimension != profile.dimension:
                raise IntegrityError("已有向量与待迁移模型指纹维度不一致")
            return stored
    except EmbeddingProfileError as exc:
        raise MigrationError(f"embedding 模型指纹不兼容：{exc}") from exc
    except SQLAlchemyError as exc:
        raise MigrationError(
            "无法读取 PostgreSQL 迁移元数据；请先执行 alembic upgrade head"
        ) from exc


def _lock_source_snapshot(connection: Connection, timeout_seconds: float) -> None:
    milliseconds = max(1, round(timeout_seconds * 1000))
    connection.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{milliseconds}ms"},
    )
    # SHARE 与应用的 INSERT/UPDATE/DELETE（ROW EXCLUSIVE）冲突。固定顺序取锁，
    # 让数据库行与随后校验的不可变活动文件在发布完成前保持同一版本。
    connection.execute(text(
        "LOCK TABLE app_metadata, knowledge_bases, documents, chunks IN SHARE MODE"
    ))


def _copy_locked_snapshot(
    source: Connection,
    target_engine: Engine,
    storage_root: Path,
    profile: EmbeddingProfile,
    batch_size: int,
):
    validate_model_contract()
    prefetched: dict[str, list[dict]] = {}
    specs = {spec.name: spec for spec in TABLE_SPECS}
    for table_name in ("app_metadata", "knowledge_bases", "documents"):
        spec = specs[table_name]
        prefetched[table_name] = [
            portable
            for row in select_rows(source, spec).mappings()
            if (
                portable := portable_row(
                    spec,
                    row,
                    embedding_dimension=profile.dimension,
                )
            )
            is not None
        ]

    storage_audit = audit_storage(prefetched["documents"], storage_root)
    validator = SnapshotValidator(
        prefetched["knowledge_bases"],
        prefetched["documents"],
        storage_audit.document_texts,
    )
    digest = DatabaseDigestBuilder()

    with target_engine.begin() as target:
        for spec in TABLE_SPECS:
            if spec.name == "chunks":
                rows: Iterable[dict] = _chunk_rows(source, spec, profile.dimension)
            else:
                rows = prefetched[spec.name]
            batch: list[dict] = []
            for row in rows:
                if spec.name == "chunks":
                    validator.check_chunk(row)
                digest.add(spec.name, row)
                batch.append(row)
                if len(batch) >= batch_size:
                    target.execute(spec.table.insert(), batch)
                    batch.clear()
            if batch:
                target.execute(spec.table.insert(), batch)

    validator.finish()
    final_storage_audit = audit_storage(prefetched["documents"], storage_root)
    if final_storage_audit.summary != storage_audit.summary:
        raise IntegrityError("迁移期间原文文件发生变化，拒绝发布候选库")
    return digest.finish(), storage_audit.summary


def _chunk_rows(source: Connection, spec, embedding_dimension: int):
    for row in select_rows(source, spec).mappings():
        portable = portable_row(
            spec,
            row,
            embedding_dimension=embedding_dimension,
        )
        if portable is not None:
            yield portable
