"""桌面壳的单实例、API 生命周期与无 GUI 编排回归。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

from fastapi import FastAPI
import pytest

from app.core.config import Settings
from app.desktop.environment import (
    DesktopEnvironment,
    DesktopEnvironmentError,
    open_configuration_file,
    prepare_desktop_environment,
)
from app.desktop.instance_lock import SingleInstanceError, SingleInstanceLock
from app.desktop.launcher import DesktopLaunchError, run_desktop
from app.desktop.server import DesktopServerError, ManagedServer
from app.diagnostics.local_logging import get_log_path


def _config(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        database_url=None,
        storage_dir=None,
        embedding_api_key="test-key",
        _env_file=None,
    )


def _project_root(root: Path) -> Path:
    index = root / "frontend" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text("<!doctype html><title>KnowBase</title>", encoding="utf-8")
    return root


def _package_root(root: Path) -> Path:
    project = _project_root(root)
    (project / ".env.example").write_text(
        "EMBEDDING_API_KEY=\nLLM_API_KEY=\n",
        encoding="utf-8",
    )
    return project


def test_packaged_environment_creates_config_once_without_overwriting(tmp_path):
    package_root = _package_root(tmp_path / "package")
    config_file = tmp_path / "profile" / "config.env"

    first = prepare_desktop_environment(
        package_root,
        packaged=True,
        config_file=config_file,
    )
    assert first.packaged is True
    assert first.config_created is True
    assert config_file.read_text(encoding="utf-8") == (
        "EMBEDDING_API_KEY=\nLLM_API_KEY=\n"
    )

    config_file.write_text("LLM_API_KEY=private\n", encoding="utf-8")
    second = prepare_desktop_environment(
        package_root,
        packaged=True,
        config_file=config_file,
    )
    assert second.config_created is False
    assert config_file.read_text(encoding="utf-8") == "LLM_API_KEY=private\n"


def test_packaged_environment_requires_template_and_opens_config(tmp_path):
    config_file = tmp_path / "profile" / "config.env"
    with pytest.raises(DesktopEnvironmentError, match="缺少默认配置模板"):
        prepare_desktop_environment(
            tmp_path / "empty-package",
            packaged=True,
            config_file=config_file,
        )

    config_file.parent.mkdir(parents=True)
    config_file.write_text("LLM_API_KEY=private\n", encoding="utf-8")
    opened: list[Path] = []
    open_configuration_file(
        DesktopEnvironment(tmp_path, config_file, True),
        opener=opened.append,
    )
    assert opened == [config_file]


def test_source_environment_resolves_dotenv_from_project_root(tmp_path):
    project_root = tmp_path / "project"
    environment = prepare_desktop_environment(project_root, packaged=False)

    assert environment.packaged is False
    assert environment.config_created is False
    assert environment.config_file == (project_root / ".env").resolve()


def test_single_instance_lock_blocks_other_process_and_releases(tmp_path):
    path = tmp_path / ".knowbase-instance.lock"
    probe = (
        "from pathlib import Path\n"
        "import sys\n"
        "from app.desktop.instance_lock import SingleInstanceLock, SingleInstanceError\n"
        "lock = SingleInstanceLock(Path(sys.argv[1]))\n"
        "try:\n"
        "    lock.acquire()\n"
        "except SingleInstanceError:\n"
        "    raise SystemExit(23)\n"
        "else:\n"
        "    lock.release()\n"
    )

    with SingleInstanceLock(path):
        with pytest.raises(SingleInstanceError):
            SingleInstanceLock(path).acquire()
        blocked = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            timeout=10,
        )
        assert blocked.returncode == 23

    released = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        timeout=10,
    )
    assert released.returncode == 0


def test_managed_server_waits_for_ready_and_stops_lifespan():
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("started")
        yield
        events.append("stopped")

    app = FastAPI(lifespan=lifespan)

    @app.get("/ready")
    def ready():
        return {"status": "ok"}

    server = ManagedServer(
        app,
        port=0,
        startup_timeout=5,
        shutdown_timeout=2,
        log_level="error",
    )
    server.start()
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"{server.base_url}/ready", timeout=2) as response:
            assert response.status == 200
        assert events == ["started"]
    finally:
        server.stop()

    assert events == ["started", "stopped"]
    server.stop()


def test_managed_server_starts_without_console_streams(monkeypatch):
    app = FastAPI()

    @app.get("/ready")
    def ready():
        return {"status": "ok"}

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    server = ManagedServer(
        app,
        port=0,
        startup_timeout=5,
        shutdown_timeout=2,
        log_level="error",
    )
    with server:
        assert server.port is not None


def test_managed_server_rejects_occupied_port():
    # 只使用它的严格预绑定端口，不启动默认应用。
    import socket

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    try:
        with pytest.raises(DesktopServerError, match="端口可能已被另一个程序占用"):
            ManagedServer(port=port).start()
    finally:
        blocker.close()


class _FakeServer:
    app_url = "http://127.0.0.1:8123/ui/"

    def __init__(self, *, port):
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None


class _FakeWebview:
    def __init__(self) -> None:
        self.window_args = None
        self.start_kwargs = None
        self.start_callback = None
        self.window = SimpleNamespace(
            events=SimpleNamespace(
                loaded=SimpleNamespace(wait=lambda _timeout: True),
            ),
            destroy=lambda: None,
        )

    def create_window(self, *args, **kwargs):
        self.window_args = (args, kwargs)
        return self.window

    def start(self, callback=None, **kwargs):
        self.start_callback = callback
        self.start_kwargs = kwargs
        if callback is not None:
            callback()


def test_desktop_launcher_prepares_runtime_and_owns_window_lifecycle(tmp_path):
    project_root = _project_root(tmp_path / "project")
    config = _config(tmp_path / "data")
    webview = _FakeWebview()

    result = run_desktop(
        project_root=project_root,
        config=config,
        port=8123,
        webview_module=webview,
        server_factory=_FakeServer,
    )

    assert result.window_opened is True
    assert result.database.is_file()
    assert (result.storage / ".knowbase-vault.json").is_file()
    assert webview.window_args is not None
    assert webview.window_args[0][1] == _FakeServer.app_url
    assert webview.start_kwargs == {
        "debug": False,
        "private_mode": False,
        "storage_path": str(result.database.parent / "webview"),
    }
    assert (result.database.parent / ".knowbase-instance.lock").read_bytes().startswith(
        b"\0"
    )
    assert get_log_path() is None


def test_desktop_window_check_loads_hidden_webview_and_closes(tmp_path):
    project_root = _project_root(tmp_path / "project")
    config = _config(tmp_path / "data")
    webview = _FakeWebview()
    destroyed: list[bool] = []
    webview.window.destroy = lambda: destroyed.append(True)

    result = run_desktop(
        project_root=project_root,
        config=config,
        port=0,
        window_check=True,
        webview_module=webview,
        server_factory=_FakeServer,
    )

    assert result.window_opened is True
    assert webview.window_args is not None
    assert webview.window_args[1]["hidden"] is True
    assert webview.start_callback is not None
    assert destroyed == [True]


def test_desktop_check_skips_gui_and_missing_frontend_fails_early(tmp_path):
    config = _config(tmp_path / "data")
    with pytest.raises(DesktopLaunchError, match="前端生产资源不存在"):
        run_desktop(
            project_root=tmp_path / "missing",
            config=config,
            check_only=True,
            server_factory=_FakeServer,
        )

    project_root = _project_root(tmp_path / "project")
    result = run_desktop(
        project_root=project_root,
        config=config,
        check_only=True,
        server_factory=_FakeServer,
    )
    assert result.window_opened is False
    assert get_log_path() is None
