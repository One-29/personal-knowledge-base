"""数据库基础设施：引擎创建与嵌入式 SQLite schema 管理。"""

from .engine import create_database_engine, dialect_name
from .schema import SQLITE_SCHEMA_VERSION, initialize_database

__all__ = [
    "SQLITE_SCHEMA_VERSION",
    "create_database_engine",
    "dialect_name",
    "initialize_database",
]
