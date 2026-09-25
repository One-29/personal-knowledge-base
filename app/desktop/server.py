"""在后台线程托管 Uvicorn，并以真实 /ready 响应作为启动门。"""

from __future__ import annotations

import socket
import time
from threading import Lock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import uvicorn


class DesktopServerError(RuntimeError):
    """桌面壳无法安全启动或停止本地 API。"""


class ManagedServer:
    def __init__(
        self,
        application: Any = "app.main:app",
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        readiness_path: str = "/ready",
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 10.0,
        log_level: str = "warning",
    ) -> None:
        self.application = application
        self.host = host
        self.requested_port = port
        self.readiness_path = readiness_path
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.log_level = log_level
        self.port: int | None = None
        self._listener: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: Thread | None = None
        self._failure: BaseException | None = None
        self._failure_lock = Lock()

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise DesktopServerError("KnowBase 本地 API 尚未启动")
        return f"http://{self.host}:{self.port}"

    @property
    def app_url(self) -> str:
        return f"{self.base_url}/ui/"

    def start(self) -> "ManagedServer":
        if self._thread is not None:
            raise DesktopServerError("KnowBase 本地 API 不能重复启动")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((self.host, self.requested_port))
            listener.listen(2048)
        except OSError as exc:
            listener.close()
            raise DesktopServerError(
                f"无法监听 {self.host}:{self.requested_port}；"
                "端口可能已被另一个程序占用"
            ) from exc

        self.port = int(listener.getsockname()[1])
        self._listener = listener
        config = uvicorn.Config(
            self.application,
            host=self.host,
            port=self.port,
            workers=1,
            log_level=self.log_level,
            # PyInstaller 的 windowed 进程没有 sys.stdout/sys.stderr；Uvicorn
            # 默认 formatter 会调用 isatty() 并在监听前崩溃。桌面入口已经配置
            # 统一滚动日志，因此这里直接复用现有 logging 树。
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=self.shutdown_timeout,
        )
        self._server = uvicorn.Server(config)
        self._thread = Thread(
            target=self._run,
            name="knowbase-api",
            daemon=True,
        )
        self._thread.start()
        try:
            self._wait_until_ready()
        except Exception:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(self.shutdown_timeout + 1.0)
            if thread.is_alive() and server is not None:
                server.force_exit = True
                thread.join(1.0)
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self._listener = None
        if thread is not None and thread.is_alive():
            raise DesktopServerError("KnowBase 本地 API 未能在超时内停止")

    def __enter__(self) -> "ManagedServer":
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.stop()

    def _run(self) -> None:
        assert self._server is not None
        assert self._listener is not None
        try:
            self._server.run(sockets=[self._listener])
        except BaseException as exc:
            with self._failure_lock:
                self._failure = exc

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        opener = build_opener(ProxyHandler({}))
        ready_url = f"{self.base_url}{self.readiness_path}"
        while time.monotonic() < deadline:
            with self._failure_lock:
                failure = self._failure
            if failure is not None:
                raise DesktopServerError(
                    f"KnowBase 本地 API 启动失败：{failure}"
                ) from failure
            if self._thread is None or not self._thread.is_alive():
                raise DesktopServerError("KnowBase 本地 API 在就绪前退出")
            try:
                with opener.open(ready_url, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (HTTPError, URLError, TimeoutError, OSError):
                pass
            time.sleep(0.05)
        raise DesktopServerError(
            f"KnowBase 本地 API 在 {self.startup_timeout:g} 秒内未就绪"
        )
