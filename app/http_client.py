"""LLM 与 embedding 共用的进程内 HTTP 连接池。"""

import atexit
from threading import Lock

import httpx

_client: httpx.Client | None = None
_lock = Lock()


def get_http_client() -> httpx.Client:
    """首次请求时创建；Client 可跨线程共享，关闭后的新生命周期可重新创建。"""
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
        return _client


def close_http_client() -> None:
    """在请求和后台任务结束后关闭；也供非 ASGI 命令在进程退出时清理。"""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_http_client)
