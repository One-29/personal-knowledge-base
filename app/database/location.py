"""数据库目标解析与真实连接身份核对。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


class DatabaseLocationError(ValueError):
    """连接串没有指向受支持且可核对的数据库目标。"""


@dataclass(frozen=True)
class DatabaseLocation:
    backend: str
    name: str | None = None
    path: Path | None = None


def parse_database_location(
    database_url: str,
    *,
    cwd: Path | None = None,
) -> DatabaseLocation:
    """解析 PostgreSQL 库名或 SQLite 文件的规范绝对路径。"""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseLocationError("DATABASE_URL 格式无效") from exc

    backend = url.get_backend_name()
    if backend == "postgresql":
        name = (url.database or "").strip().lower()
        if not name:
            raise DatabaseLocationError("PostgreSQL 连接串缺少数据库名")
        return DatabaseLocation(backend=backend, name=name)

    if backend != "sqlite":
        raise DatabaseLocationError(f"不支持的数据库后端：{backend}")

    database = (url.database or "").strip()
    if (
        database in {"", ":memory:"}
        or database.startswith("file:")
        or str(url.query.get("mode", "")).lower() == "memory"
    ):
        raise DatabaseLocationError("日常数据必须使用 SQLite 文件，不能使用内存库")
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return DatabaseLocation(backend=backend, path=path.resolve())


def session_database_location(
    db: Session,
    configured_url: str,
) -> DatabaseLocation:
    """从活动 Session 查询真实目标，防止配置与连接工厂错位。"""
    configured = parse_database_location(configured_url)
    if configured.backend == "postgresql":
        actual = str(
            db.scalar(text("SELECT current_database()")) or ""
        ).strip().lower()
        return DatabaseLocation(backend="postgresql", name=actual)

    rows = db.execute(text("PRAGMA database_list")).all()
    main_files = [str(row[2]) for row in rows if str(row[1]) == "main"]
    if len(main_files) != 1 or not main_files[0]:
        raise DatabaseLocationError("当前 SQLite Session 没有可核对的主数据库文件")
    return DatabaseLocation(
        backend="sqlite",
        path=Path(main_files[0]).expanduser().resolve(),
    )
