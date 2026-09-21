"""按数据库方言创建 SQLAlchemy 引擎。

连接池和 SQLite 连接级 PRAGMA 都集中在这里，业务模块不自行创建引擎。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def create_database_engine(
    database_url: str,
    *,
    pool_size: int,
    max_overflow: int,
    pool_recycle: int,
    pool_timeout: float,
) -> Engine:
    """创建应用引擎，并为 SQLite 打开外键约束和写锁等待。

    文件 SQLite 沿用 SQLAlchemy 2.x 的 QueuePool；内存 SQLite 使用
    StaticPool，保证同一进程的所有会话看到同一个数据库。
    """
    url = make_url(database_url)
    common: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_recycle": pool_recycle,
    }

    if url.get_backend_name() == "sqlite":
        _ensure_sqlite_parent(url.database)
        common["connect_args"] = {
            "check_same_thread": False,
            "timeout": pool_timeout,
        }
        if _is_memory_sqlite(url.database):
            common["poolclass"] = StaticPool
        else:
            common.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout,
            )
    else:
        common.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
        )

    engine = create_engine(database_url, **common)
    if url.get_backend_name() == "sqlite":
        _configure_sqlite_connections(engine, pool_timeout)
    return engine


def dialect_name(bind: Session | Engine) -> str:
    """返回会话或引擎的基础方言名，供基础设施层选择实现。"""
    engine = bind.get_bind() if isinstance(bind, Session) else bind
    return engine.dialect.name


def _is_memory_sqlite(database: str | None) -> bool:
    return database in {None, "", ":memory:"} or (
        database is not None and database.startswith("file::memory:")
    )


def _ensure_sqlite_parent(database: str | None) -> None:
    """让新用户配置嵌套数据库路径时无需预先手工创建目录。"""
    if _is_memory_sqlite(database) or database is None or database.startswith("file:"):
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite_connections(engine: Engine, timeout_seconds: float) -> None:
    busy_timeout_ms = max(1, round(timeout_seconds * 1000))

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        finally:
            cursor.close()
