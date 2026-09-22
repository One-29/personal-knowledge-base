"""`python -m app.desktop` 命令行入口。"""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 KnowBase 桌面窗口")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="准备运行时并验证本地 API，不创建窗口",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        _show_error("端口必须在 1 到 65535 之间")
        return 2
    if args.width < 960 or args.height < 640:
        _show_error("窗口尺寸不能小于 960×640")
        return 2

    project_root = args.project_root.expanduser().resolve()
    try:
        os.chdir(project_root)
        # Settings 会读取项目根 .env，因此必须在切换工作目录后再导入。
        from .launcher import run_desktop

        result = run_desktop(
            project_root=project_root,
            port=args.port,
            width=args.width,
            height=args.height,
            debug=args.debug,
            check_only=args.check,
        )
    except Exception as exc:
        _show_error(str(exc) or exc.__class__.__name__)
        return 1

    if args.check:
        print("[KnowBase Desktop] check passed")
        print(f"  endpoint: {result.url}")
        print(f"  database: {result.database}")
        print(f"  storage: {result.storage}")
    return 0


def _show_error(message: str) -> None:
    text = f"KnowBase 无法启动：\n\n{message}"
    print(text, file=sys.stderr)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "KnowBase 启动失败", 0x10)
        except (AttributeError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
