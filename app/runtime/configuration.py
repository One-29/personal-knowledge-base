"""日常桌面运行时的路径边界。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.database import DatabaseLocationError, parse_database_location


class RuntimeConfigurationError(RuntimeError):
    """日常启动配置不能安全映射到本地 SQLite。"""


@dataclass(frozen=True)
class RuntimePaths:
    database: Path
    storage: Path


def resolve_runtime_paths(config: Settings) -> RuntimePaths:
    """要求文件 SQLite，并拒绝数据库被放进原文目录。"""
    if config.database_url is None or config.storage_dir is None:
        raise RuntimeConfigurationError("数据库或原文目录配置尚未解析")
    try:
        location = parse_database_location(config.database_url)
    except DatabaseLocationError as exc:
        raise RuntimeConfigurationError(str(exc)) from exc
    if location.backend != "sqlite" or location.path is None:
        raise RuntimeConfigurationError(
            "普通启动只使用本地 SQLite。请先运行 PostgreSQL → SQLite 数据迁移，"
            "再从 .env 删除 DATABASE_URL 与 STORAGE_DIR；"
            "如需改位置，只设置 KNOWBASE_DATA_DIR。"
        )

    database = location.path
    storage = config.storage_dir.expanduser().resolve()
    if database == storage or storage in database.parents:
        raise RuntimeConfigurationError("SQLite 数据库文件不能放在原文存储目录内")
    return RuntimePaths(database=database, storage=storage)
