"""桌面包资源与用户配置的边界。

本模块不导入 ``app.core.config``，入口可先准备用户配置，再让全应用创建唯一
的 Settings 实例。这样冻结包不会把密钥写进发布物，也不会因工作目录变化而
读错配置。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

from app.core.paths import runtime_config_file


class DesktopEnvironmentError(RuntimeError):
    """桌面资源或用户配置无法安全准备。"""


@dataclass(frozen=True)
class DesktopEnvironment:
    resource_root: Path
    config_file: Path
    packaged: bool
    config_created: bool = False


def is_packaged() -> bool:
    """PyInstaller 会在冻结进程设置 ``sys.frozen``。"""
    return bool(getattr(sys, "frozen", False))


def default_resource_root() -> Path:
    """返回包含 ``frontend/dist`` 的源码根或冻结包资源根。"""
    return Path(__file__).resolve().parents[2]


def prepare_desktop_environment(
    resource_root: Path,
    *,
    packaged: bool | None = None,
    config_file: Path | None = None,
) -> DesktopEnvironment:
    """在导入全局 Settings 前准备冻结包的用户配置。

    文件以独占创建方式发布；两个首次启动进程发生竞态时，已有文件保持原样。
    源码模式不自动生成 ``.env``，继续由开发者显式配置。
    """
    root = resource_root.expanduser().resolve()
    frozen = is_packaged() if packaged is None else packaged
    candidate_config = (
        config_file.expanduser()
        if config_file is not None
        else runtime_config_file(packaged=frozen)
    )
    resolved_config = (
        candidate_config.resolve()
        if candidate_config.is_absolute()
        else (root / candidate_config).resolve()
    )
    if not frozen:
        return DesktopEnvironment(root, resolved_config, False)

    template = root / ".env.example"
    if not template.is_file():
        raise DesktopEnvironmentError("安装包缺少默认配置模板，请重新下载安装包")

    try:
        resolved_config.parent.mkdir(parents=True, exist_ok=True)
        content = template.read_text(encoding="utf-8")
        try:
            with resolved_config.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            created = True
        except FileExistsError:
            if not resolved_config.is_file():
                raise DesktopEnvironmentError(
                    f"配置路径不是普通文件：{resolved_config}"
                ) from None
            created = False
    except DesktopEnvironmentError:
        raise
    except OSError as exc:
        raise DesktopEnvironmentError(f"无法准备用户配置：{resolved_config}") from exc

    return DesktopEnvironment(root, resolved_config, True, created)


def open_configuration_file(
    environment: DesktopEnvironment,
    *,
    opener: Callable[[Path], None] | None = None,
) -> None:
    """用操作系统默认编辑器打开用户配置文件。"""
    if not environment.config_file.is_file():
        raise DesktopEnvironmentError(
            f"配置文件不存在：{environment.config_file}"
        )
    if opener is not None:
        opener(environment.config_file)
        return
    try:
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(environment.config_file)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(environment.config_file)])
        else:
            subprocess.Popen(["xdg-open", str(environment.config_file)])
    except OSError as exc:
        raise DesktopEnvironmentError(
            f"无法打开配置文件：{environment.config_file}"
        ) from exc
