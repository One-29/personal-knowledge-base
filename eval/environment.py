"""评估数据库与原文目录的双重隔离校验。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_DATABASE_NAME = "knowbase_eval"
EVAL_SQLITE_RELATIVE_PATH = Path("data/eval/knowbase-eval.db")
EVAL_STORAGE_RELATIVE_PATH = Path("data/eval-storage")


class UnsafeEvalEnvironmentError(ValueError):
    """评估环境可能影响日常数据时抛出。"""


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def validate_eval_environment(
    database_url: str,
    storage_dir: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    working_directory: Path | None = None,
) -> None:
    """确认评估只会修改专用 PostgreSQL 库或固定 SQLite 文件。"""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise UnsafeEvalEnvironmentError("评估数据库连接地址无效。") from exc

    root = project_root.resolve(strict=False)
    current_directory = (working_directory or Path.cwd()).resolve(strict=False)
    backend = url.get_backend_name()
    if backend == "postgresql" and url.database != EVAL_DATABASE_NAME:
        raise UnsafeEvalEnvironmentError(
            f"评估只允许修改专用数据库 {EVAL_DATABASE_NAME}。"
        )
    if backend == "sqlite":
        configured_database = _sqlite_path(url.database, current_directory)
        expected_database = _resolve_path(EVAL_SQLITE_RELATIVE_PATH, root)
        if configured_database != expected_database:
            raise UnsafeEvalEnvironmentError(
                f"SQLite 评估只允许修改固定文件 {expected_database}。"
            )
    elif backend != "postgresql":
        raise UnsafeEvalEnvironmentError(
            "评估只允许使用 PostgreSQL 或 SQLite 数据库。"
        )

    configured_storage = _resolve_path(storage_dir, current_directory)
    expected_storage = _resolve_path(EVAL_STORAGE_RELATIVE_PATH, root)
    if configured_storage != expected_storage:
        raise UnsafeEvalEnvironmentError(
            f"评估原文目录必须精确使用隔离目录 {expected_storage}。"
        )


def validate_eval_database_session(
    db,
    database_url: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> None:
    """核对 Session 的真实后端与库/文件，防止连接工厂仍指向日常数据。"""
    configured_backend = make_url(database_url).get_backend_name()
    actual_backend = db.get_bind().dialect.name
    if actual_backend != configured_backend:
        raise UnsafeEvalEnvironmentError(
            f"评估配置使用 {configured_backend}，实际 Session 使用 {actual_backend}。"
        )
    if actual_backend == "postgresql":
        actual_database = str(db.scalar(select(func.current_database())) or "").lower()
        if actual_database != EVAL_DATABASE_NAME:
            raise UnsafeEvalEnvironmentError(
                f"当前数据库连接实际指向 {actual_database or '未知数据库'}；"
                f"评估只允许修改 {EVAL_DATABASE_NAME}。"
            )
        return

    if actual_backend == "sqlite":
        rows = db.execute(text("PRAGMA database_list")).all()
        actual_file = next(
            (str(row[2]) for row in rows if str(row[1]) == "main"),
            "",
        )
        expected_file = _resolve_path(EVAL_SQLITE_RELATIVE_PATH, project_root)
        if not actual_file or Path(actual_file).resolve(strict=False) != expected_file:
            raise UnsafeEvalEnvironmentError(
                f"当前 SQLite Session 实际指向 {actual_file or '未知文件'}；"
                f"评估只允许修改 {expected_file}。"
            )
        return

    raise UnsafeEvalEnvironmentError(f"评估不支持数据库方言 {actual_backend}。")


def validate_eval_engine(
    engine,
    database_url: str,
    storage_dir: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    working_directory: Path | None = None,
) -> None:
    """在建表前确认进程级 engine 与已校验配置指向同一隔离目标。"""
    configured_backend = make_url(database_url).get_backend_name()
    if engine.dialect.name != configured_backend:
        raise UnsafeEvalEnvironmentError(
            f"评估配置使用 {configured_backend}，实际 engine 使用 {engine.dialect.name}。"
        )
    validate_eval_environment(
        engine.url.render_as_string(hide_password=False),
        storage_dir,
        project_root=project_root,
        working_directory=working_directory,
    )


def _sqlite_path(database: str | None, working_directory: Path) -> Path:
    if not database or database == ":memory:" or database.startswith("file:"):
        raise UnsafeEvalEnvironmentError("SQLite 评估必须使用固定的本地文件数据库。")
    return _resolve_path(database, working_directory)
