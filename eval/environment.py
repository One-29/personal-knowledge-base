"""评估数据库与原文目录的双重隔离校验。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_DATABASE_NAME = "knowbase_eval"


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
    """确认评估只会修改专用 PostgreSQL 数据库和隔离原文目录。"""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise UnsafeEvalEnvironmentError("评估数据库连接地址无效。") from exc

    if url.get_backend_name() != "postgresql":
        raise UnsafeEvalEnvironmentError("评估只允许使用 PostgreSQL 数据库。")
    if url.database != EVAL_DATABASE_NAME:
        raise UnsafeEvalEnvironmentError(
            f"评估只允许修改专用数据库 {EVAL_DATABASE_NAME}。"
        )

    root = project_root.resolve(strict=False)
    current_directory = (working_directory or Path.cwd()).resolve(strict=False)
    configured_storage = _resolve_path(storage_dir, current_directory)
    expected_storage = _resolve_path(Path("data/eval-storage"), root)
    if configured_storage != expected_storage:
        raise UnsafeEvalEnvironmentError(
            f"评估原文目录必须精确使用隔离目录 {expected_storage}。"
        )


def validate_eval_database_session(db) -> None:
    """核对 Session 的真实数据库，防止连接工厂仍指向日常库。"""
    actual_database = str(db.scalar(select(func.current_database())) or "").lower()
    if actual_database != EVAL_DATABASE_NAME:
        raise UnsafeEvalEnvironmentError(
            f"当前数据库连接实际指向 {actual_database or '未知数据库'}；"
            f"评估只允许修改 {EVAL_DATABASE_NAME}。"
        )
