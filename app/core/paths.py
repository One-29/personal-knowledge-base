"""KnowBase 跨平台用户数据路径。

源码目录只保存程序与演示素材；日常数据库和原文默认进入操作系统用户数据
目录，避免安装到只读目录后无法写入，也避免不同 clone 各自产生一份数据。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_DIRECTORY_NAME = "KnowBase"
DATABASE_FILE_NAME = "knowbase.db"


def default_user_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    """返回 Windows/macOS/Linux 的默认用户数据目录。"""
    env = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name
    user_home = Path.home() if home is None else home

    if platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        return base / APP_DIRECTORY_NAME
    if platform == "darwin":
        return user_home / "Library" / "Application Support" / APP_DIRECTORY_NAME

    xdg_data_home = env.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else user_home / ".local" / "share"
    return base / APP_DIRECTORY_NAME.lower()


def sqlite_database_url(path: Path) -> str:
    """把绝对文件路径编码成 SQLAlchemy pysqlite URL。"""
    return "sqlite+pysqlite:///" + path.expanduser().resolve().as_posix()
