"""``python -m app.runtime``：准备嵌入式 SQLite 运行时。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .configuration import RuntimeConfigurationError
from .initializer import RuntimeInitializationError, prepare_runtime
from .project_import import ProjectDataImportError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="准备 KnowBase 本地 SQLite 运行时")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="项目根目录（用于发现 data/knowbase.db 迁移产物）",
    )
    parser.add_argument(
        "--skip-project-import",
        action="store_true",
        help="不尝试首次导入项目 data/ 下的旧数据",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        initialized = prepare_runtime(
            project_root=args.project_root,
            import_existing_project_data=not args.skip_project_import,
        )
    except (
        ProjectDataImportError,
        RuntimeConfigurationError,
        RuntimeInitializationError,
    ) as exc:
        print(f"[KnowBase Runtime] ERROR: {exc}", file=sys.stderr)
        return 1

    status_messages = {
        "imported": "已把项目迁移数据完整导入用户目录",
        "target-exists": "复用已有用户数据库",
        "source-missing": "没有旧迁移数据，已创建新的用户数据库",
        "disabled": "已跳过项目数据导入",
    }
    print(f"[KnowBase Runtime] {status_messages[initialized.project_import.status]}")
    print(f"  database: {initialized.paths.database}")
    print(f"  storage: {initialized.paths.storage}")
    return 0
