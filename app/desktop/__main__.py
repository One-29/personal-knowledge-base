"""`python -m app.desktop` 命令行入口。"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
from pathlib import Path
import sys

from .environment import (
    default_resource_root,
    open_configuration_file,
    prepare_desktop_environment,
)

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 KnowBase 桌面窗口")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=default_resource_root(),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="本地 API 端口；0 表示自动选择空闲端口",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--debug", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="准备运行时并验证本地 API，不创建窗口",
    )
    mode.add_argument(
        "--window-check",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mode.add_argument(
        "--open-config",
        action="store_true",
        help="创建并打开桌面包的用户配置，然后退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    automated_check = args.check or args.window_check
    if not 0 <= args.port <= 65535:
        _show_error("端口必须在 0 到 65535 之间", show_dialog=not automated_check)
        return 2
    if args.width < 960 or args.height < 640:
        _show_error("窗口尺寸不能小于 960×640", show_dialog=not automated_check)
        return 2

    project_root = args.project_root.expanduser().resolve()
    try:
        environment = prepare_desktop_environment(project_root)
        if args.open_config:
            open_configuration_file(environment)
            return 0
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
            window_check=args.window_check,
        )
    except Exception as exc:
        logger.exception("KnowBase 桌面启动失败")
        _show_error(
            str(exc) or exc.__class__.__name__,
            show_dialog=not automated_check,
        )
        return 1

    if automated_check and sys.stdout is not None:
        print("[KnowBase Desktop] check passed")
        print(f"  endpoint: {result.url}")
        print(f"  database: {result.database}")
        print(f"  storage: {result.storage}")
    return 0


def _show_error(message: str, *, show_dialog: bool = True) -> None:
    text = f"KnowBase 无法启动：\n\n{message}"
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    if os.name == "nt" and show_dialog:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "KnowBase 启动失败", 0x10)
        except (AttributeError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
