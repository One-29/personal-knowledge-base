"""数据库基础设施：引擎创建与嵌入式 SQLite schema 管理。"""

from .engine import create_database_engine, dialect_name
from .location import (
    DatabaseLocation,
    DatabaseLocationError,
    parse_database_location,
    session_database_location,
)
from .schema import SQLITE_SCHEMA_VERSION, UnsupportedSchemaVersion, initialize_database

__all__ = [
    "SQLITE_SCHEMA_VERSION",
    "UnsupportedSchemaVersion",
    "DatabaseLocation",
    "DatabaseLocationError",
    "create_database_engine",
    "dialect_name",
    "initialize_database",
    "parse_database_location",
    "session_database_location",
]
