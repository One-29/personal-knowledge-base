"""把仓库内已验真的 SQLite/原文复制到用户数据目录。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.database import UnsupportedSchemaVersion
from app.migration.errors import IntegrityError
from app.migration.report import DatabaseDigest
from app.migration.sqlite_target import (
    remove_sqlite_files,
    verify_published_database,
)

from .database_validation import validate_and_seal_database
from .storage_tree import (
    StorageTreeError,
    TreeDigest,
    copy_tree_verified,
    digest_tree,
)


class ProjectDataImportError(RuntimeError):
    """旧项目数据没有完整发布到用户数据目录。"""


@dataclass(frozen=True)
class ProjectDataImport:
    status: str
    database: DatabaseDigest | None = None
    storage: TreeDigest | None = None


def import_project_data(
    *,
    source_database: Path,
    source_storage: Path,
    target_database: Path,
    target_storage: Path,
    embedding_dimension: int,
    timeout_seconds: float = 5.0,
) -> ProjectDataImport:
    """只在目标库不存在时发布；源数据始终保持原样。"""
    source_database = _absolute_path(source_database)
    source_storage = _absolute_path(source_storage)
    target_database = _absolute_path(target_database)
    target_storage = _absolute_path(target_storage)

    if target_database.exists():
        if not target_database.is_file():
            raise ProjectDataImportError(f"SQLite 目标不是文件：{target_database}")
        return ProjectDataImport(status="target-exists")
    if not source_database.exists():
        return ProjectDataImport(status="source-missing")
    if not source_database.is_file() or _is_link(source_database):
        raise ProjectDataImportError("项目 SQLite 源必须是普通文件")
    _validate_distinct_paths(
        source_database,
        source_storage,
        target_database,
        target_storage,
    )
    _reject_orphan_sidecars(target_database)

    target_storage_was_empty = False
    if target_storage.exists():
        if not target_storage.is_dir() or _is_link(target_storage):
            raise ProjectDataImportError(f"原文目标不是普通目录：{target_storage}")
        if any(target_storage.iterdir()):
            raise ProjectDataImportError(
                f"SQLite 目标尚不存在，但原文目标非空，拒绝混合数据：{target_storage}"
            )
        target_storage_was_empty = True

    target_database.parent.mkdir(parents=True, exist_ok=True)
    target_storage.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    database_candidate = target_database.with_name(
        f".{target_database.name}.{token}.importing"
    )
    storage_candidate = target_storage.with_name(
        f".{target_storage.name}.{token}.importing"
    )
    storage_published = False
    database_published = False
    success = False
    try:
        _backup_sqlite(
            source_database,
            database_candidate,
            timeout_seconds=timeout_seconds,
        )
        storage_digest = copy_tree_verified(source_storage, storage_candidate)
        database_digest = validate_and_seal_database(
            database_candidate,
            storage_candidate,
            embedding_dimension=embedding_dimension,
            timeout_seconds=timeout_seconds,
        )

        if target_storage_was_empty:
            target_storage.rmdir()
        # rename 对非空目录不会覆盖；数据库再用硬链接完成“目标不存在”条件下
        # 的原子认领，避免两个启动进程竞态时覆盖先发布的用户库。
        os.rename(storage_candidate, target_storage)
        storage_published = True
        os.link(database_candidate, target_database)
        database_published = True
        database_candidate.unlink()

        verify_published_database(
            target_database,
            database_digest,
            embedding_dimension,
        )
        if digest_tree(target_storage) != storage_digest:
            raise IntegrityError("发布后的原文目录摘要发生变化")
        result = ProjectDataImport(
            status="imported",
            database=database_digest,
            storage=storage_digest,
        )
        success = True
        return result
    except ProjectDataImportError:
        raise
    except (
        IntegrityError,
        OSError,
        SQLAlchemyError,
        sqlite3.Error,
        StorageTreeError,
        UnsupportedSchemaVersion,
    ) as exc:
        raise ProjectDataImportError(f"项目数据首次导入失败：{exc}") from exc
    finally:
        if database_published and not success:
            remove_sqlite_files(target_database)
        if storage_published and not success:
            shutil.rmtree(target_storage, ignore_errors=True)
        remove_sqlite_files(database_candidate)
        shutil.rmtree(storage_candidate, ignore_errors=True)
        if (
            target_storage_was_empty
            and not target_storage.exists()
            and not success
        ):
            target_storage.mkdir(parents=True, exist_ok=True)


def _backup_sqlite(source: Path, candidate: Path, *, timeout_seconds: float) -> None:
    """SQLite backup API 会包含源库已提交但仍位于 WAL 的页面。"""
    source_uri = source.as_uri() + "?mode=ro"
    with closing(
        sqlite3.connect(
            source_uri,
            uri=True,
            timeout=timeout_seconds,
        )
    ) as source_connection:
        with closing(
            sqlite3.connect(candidate, timeout=timeout_seconds)
        ) as target_connection:
            source_connection.backup(target_connection)


def _validate_distinct_paths(
    source_database: Path,
    source_storage: Path,
    target_database: Path,
    target_storage: Path,
) -> None:
    source_database = source_database.resolve()
    source_storage = source_storage.resolve()
    target_database = target_database.resolve()
    target_storage = target_storage.resolve()
    if source_database == target_database or source_storage == target_storage:
        raise ProjectDataImportError("项目数据源与用户数据目标不能是同一路径")
    if (
        source_storage in target_storage.parents
        or target_storage in source_storage.parents
    ):
        raise ProjectDataImportError("项目原文目录与用户原文目录不能互相包含")
    if target_storage in target_database.parents:
        raise ProjectDataImportError("SQLite 目标不能放在原文目标目录内")


def _reject_orphan_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            raise ProjectDataImportError(f"目标存在孤立事务文件：{sidecar.name}")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))
