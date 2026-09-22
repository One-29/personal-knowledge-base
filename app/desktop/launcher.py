"""准备 SQLite、托管 API，并在主线程运行 pywebview 窗口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from app.core.config import Settings, settings
from app.runtime import prepare_runtime
from app.runtime.configuration import resolve_runtime_paths

from .instance_lock import SingleInstanceLock, lock_path
from .server import ManagedServer


class DesktopLaunchError(RuntimeError):
    """桌面窗口启动条件不完整。"""


@dataclass(frozen=True)
class DesktopRun:
    url: str
    database: Path
    storage: Path
    window_opened: bool


def run_desktop(
    *,
    project_root: Path,
    config: Settings = settings,
    port: int = 8000,
    width: int = 1280,
    height: int = 820,
    debug: bool = False,
    check_only: bool = False,
    webview_module: Any | None = None,
    server_factory: Callable[..., ManagedServer] | None = None,
) -> DesktopRun:
    """运行一个桌面实例；check_only 会走通服务器但不创建 GUI。"""
    root = project_root.expanduser().resolve()
    _require_frontend(root)
    paths = resolve_runtime_paths(config)

    with SingleInstanceLock(lock_path(paths.database)):
        initialized = prepare_runtime(config=config, project_root=root)
        server_type = server_factory or ManagedServer
        with server_type(port=port) as server:
            if check_only:
                return DesktopRun(
                    url=server.app_url,
                    database=initialized.paths.database,
                    storage=initialized.paths.storage,
                    window_opened=False,
                )

            webview = webview_module or _load_webview()
            webview.create_window(
                "KnowBase · 个人知识库",
                server.app_url,
                width=width,
                height=height,
                min_size=(960, 640),
                background_color="#f4f1ea",
                text_select=True,
            )
            profile_dir = initialized.paths.database.parent / "webview"
            profile_dir.mkdir(parents=True, exist_ok=True)
            webview.start(
                debug=debug,
                private_mode=False,
                storage_path=str(profile_dir),
            )
            return DesktopRun(
                url=server.app_url,
                database=initialized.paths.database,
                storage=initialized.paths.storage,
                window_opened=True,
            )


def _require_frontend(project_root: Path) -> None:
    index = project_root / "frontend" / "dist" / "index.html"
    if not index.is_file():
        raise DesktopLaunchError(
            "前端生产资源不存在。请先运行 scripts/build-frontend.ps1"
        )


def _load_webview() -> ModuleType:
    try:
        import webview
    except ImportError as exc:
        raise DesktopLaunchError(
            '桌面组件未安装。请运行：.\\.venv\\Scripts\\python.exe -m pip '
            'install -e ".[desktop]"'
        ) from exc
    return webview
