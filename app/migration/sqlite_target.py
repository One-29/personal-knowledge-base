"""SQLite 候选库验真、WAL 收束、备份和原子发布。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.database import create_database_engine

from .errors import IntegrityError, MigrationError
from .integrity import digest_database
from .report import DatabaseDigest

DEFAULT_SQLITE_TIMEOUT_SECONDS = 5.0


def verify_sqlite(
    engine: Engine,
    *,
    embedding_dimension: int,
) -> DatabaseDigest:
    with engine.connect() as connection:
        quick_check = connection.exec_driver_sql("PRAGMA quick_check").scalar_one()
        if quick_check != "ok":
            raise IntegrityError(f"SQLite quick_check 失败：{quick_check}")

        foreign_key_errors = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise IntegrityError(
                f"SQLite 外键检查发现 {len(foreign_key_errors)} 条错误"
            )

        # rank=1 要求 FTS5 同时核对 external-content 表与全文索引内容。
        connection.exec_driver_sql(
            "INSERT INTO chunks_fts(chunks_fts, rank) "
            "VALUES('integrity-check', 1)"
        )
        chunk_count = int(connection.scalar(text("SELECT count(*) FROM chunks")) or 0)
        fts_count = int(
            connection.scalar(text("SELECT count(*) FROM chunks_fts")) or 0
        )
        if fts_count != chunk_count:
            raise IntegrityError(
                f"SQLite FTS 行数不一致：chunks={chunk_count}，fts={fts_count}"
            )
        return digest_database(
            connection,
            embedding_dimension=embedding_dimension,
        )


def seal_sqlite(engine: Engine) -> None:
    """把候选库的 WAL 全量并回主文件，确保单文件原子发布。"""
    with engine.connect() as connection:
        checkpoint = connection.exec_driver_sql(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).one()
        if int(checkpoint[0]) != 0:
            raise IntegrityError("SQLite 候选库仍有连接占用，无法收束 WAL")
        mode = str(
            connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one()
        ).lower()
        if mode != "delete":
            raise IntegrityError(f"SQLite 候选库无法切换单文件模式：{mode}")


def publish_candidate(
    candidate: Path,
    target: Path,
    *,
    replace_existing: bool,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> Path | None:
    backup: Path | None = None
    if target.exists():
        if not replace_existing:
            raise MigrationError(f"目标 SQLite 已存在：{target}")
        _quiesce_existing_database(target, timeout_seconds)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.pre-migration-{timestamp}.bak")
        if backup.exists():
            backup = target.with_name(
                f"{target.name}.pre-migration-{timestamp}-{uuid4().hex[:8]}.bak"
            )
        os.replace(target, backup)

    try:
        os.replace(candidate, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    return backup


def verify_published_database(
    target: Path,
    expected: DatabaseDigest,
    embedding_dimension: int,
) -> None:
    engine = sqlite_engine(target)
    try:
        actual = verify_sqlite(
            engine,
            embedding_dimension=embedding_dimension,
        )
        if actual != expected:
            raise IntegrityError("发布后的 SQLite 摘要发生变化")
    finally:
        engine.dispose()


def rollback_publication(target: Path, backup: Path | None) -> None:
    """最终复核失败时恢复旧目标；首次发布则撤下未获确认的新文件。"""
    remove_sqlite_files(target)
    if backup is not None and backup.exists():
        os.replace(backup, target)


def sqlite_engine(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> Engine:
    return create_database_engine(
        "sqlite+pysqlite:///" + path.as_posix(),
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=timeout_seconds,
    )


def remove_sqlite_files(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        candidate.unlink(missing_ok=True)


def _quiesce_existing_database(path: Path, timeout_seconds: float) -> None:
    """替换前 checkpoint 旧库；活跃进程或损坏文件都会使操作中止。"""
    connection = sqlite3.connect(path, timeout=timeout_seconds)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise MigrationError("已有 SQLite 正被使用，请先关闭 KnowBase")
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise MigrationError("已有 SQLite 无法进入可备份状态，请先关闭 KnowBase")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise MigrationError(f"已有 SQLite 仍存在事务侧文件：{sidecar.name}")
