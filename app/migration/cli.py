"""``python -m app.migration.cli`` 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import settings
from app.db import engine

from .postgresql_to_sqlite import MigrationError, migrate_postgresql_to_sqlite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把当前 KnowBase PostgreSQL 数据完整迁移到 SQLite",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("data/knowbase.db"),
        help="目标 SQLite 文件（默认 data/knowbase.db）",
    )
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=settings.storage_dir,
        help="原文目录（默认使用 STORAGE_DIR 配置）",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/knowbase-migration-report.json"),
        help="完整性报告路径",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="校验候选库后备份并替换已有目标",
    )
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--lock-timeout", type=float, default=5.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = migrate_postgresql_to_sqlite(
            engine,
            args.target,
            args.storage_dir,
            replace_existing=args.replace_existing,
            batch_size=args.batch_size,
            lock_timeout_seconds=args.lock_timeout,
        )
    except (MigrationError, ValueError) as exc:
        print(f"[KnowBase Migration] ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        report.write(args.report)
    except OSError as exc:
        print(
            "[KnowBase Migration] SQLite 已成功发布，但报告写入失败："
            f"{exc}",
            file=sys.stderr,
        )
        return 2

    counts = report.database.counts
    print("[KnowBase Migration] PostgreSQL → SQLite 迁移完成")
    print(f"  target: {report.target_database}")
    print(f"  report: {args.report.expanduser().resolve()}")
    print(
        "  rows: "
        f"knowledge_bases={counts['knowledge_bases']}, "
        f"documents={counts['documents']}, chunks={counts['chunks']}"
    )
    print(f"  database_sha256: {report.database.combined_sha256}")
    print(f"  files_sha256: {report.files.combined_sha256}")
    print(f"  embedding: {report.embedding.model} / {report.embedding.dimension} 维")
    if report.backup_database:
        print(f"  previous_target_backup: {report.backup_database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
