"""SQLite schema 初始化与版本守卫。

PostgreSQL 在迁移期继续由 Alembic 管理；SQLite 使用单调递增的 schema
版本和可重建的 FTS5 索引，便于后续桌面安装包原地升级。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .engine import dialect_name

SQLITE_SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(RuntimeError):
    """数据库 schema 版本与当前程序不兼容，不能安全打开。"""


def initialize_database(engine: Engine) -> None:
    """初始化当前方言需要的 schema；重复调用是幂等的。"""
    if dialect_name(engine) != "sqlite":
        return

    # 已有库必须先读版本再执行任何 DDL，避免旧应用用 create_all() 触碰由
    # 新版应用创建的数据库。
    _validate_schema_version(_read_schema_version(engine))

    # 延迟导入可避免 db.Base -> database -> models -> db.Base 的循环。
    from app import models  # noqa: F401
    from app.db import Base

    _enable_wal(engine)
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """))
        connection.execute(
            text("""
                INSERT OR IGNORE INTO app_metadata(key, value)
                VALUES ('schema_version', :version)
            """),
            {"version": str(SQLITE_SCHEMA_VERSION)},
        )
        _validate_schema_version(connection.scalar(text(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        )))

        fts_exists = bool(connection.scalar(text("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'chunks_fts'
        """)))
        _create_fts_schema(connection)
        if not fts_exists:
            # external-content FTS 表不会自动收录创建前已有的 chunks。
            connection.execute(text(
                "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"
            ))


def _read_schema_version(engine: Engine) -> str | None:
    with engine.connect() as connection:
        metadata_exists = connection.scalar(text("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'app_metadata'
        """))
        if not metadata_exists:
            return None
        return connection.scalar(text(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        ))


def _validate_schema_version(version: str | None) -> None:
    if version is None:
        return
    try:
        parsed = int(version)
    except (TypeError, ValueError):
        raise UnsupportedSchemaVersion(
            f"知识库数据库版本无效：{version!r}"
        ) from None
    if parsed > SQLITE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            "知识库数据库由更高版本的 KnowBase 创建："
            f"数据库版本 {parsed}，当前支持 {SQLITE_SCHEMA_VERSION}"
        )
    if parsed < SQLITE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            "知识库数据库需要升级："
            f"数据库版本 {parsed}，当前版本 {SQLITE_SCHEMA_VERSION}"
        )


def _enable_wal(engine: Engine) -> None:
    """文件数据库启用 WAL；内存库会返回 memory，属于 SQLite 预期行为。"""
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()


def _create_fts_schema(connection) -> None:
    connection.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            content='chunks',
            content_rowid='id',
            tokenize='trigram'
        )
    """))
    connection.execute(text("""
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
        END
    """))
    connection.execute(text("""
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END
    """))
    connection.execute(text("""
        CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF content ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
        END
    """))
