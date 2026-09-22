"""本地 Web UI 的最小同源写请求保护。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
TRUSTED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TRUSTED_DEVELOPMENT_ORIGINS = frozenset(
    {
        ("http", "127.0.0.1", 5173),
        ("http", "localhost", 5173),
    }
)


async def local_browser_write_guard(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """拒绝非本地浏览器发出的修改请求；无 Origin 的 CLI 调用保持可用。"""
    origin = request.headers.get("origin")
    if (
        request.method.upper() not in SAFE_METHODS
        and origin is not None
        and not _browser_write_is_allowed(request, origin)
    ):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "拒绝来自其它站点的本地数据修改请求",
                "code": "CROSS_ORIGIN_WRITE_BLOCKED",
            },
        )
    return await call_next(request)


def _browser_write_is_allowed(request: Request, origin: str) -> bool:
    request_origin = _request_origin(request)
    browser_origin = _origin(origin)
    request_host = (request.url.hostname or "").lower()
    return (
        request_host in TRUSTED_LOCAL_HOSTS
        and browser_origin is not None
        and (
            browser_origin == request_origin
            or browser_origin in TRUSTED_DEVELOPMENT_ORIGINS
        )
    )


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def _request_origin(request: Request) -> tuple[str, str, int] | None:
    scheme = request.url.scheme.lower()
    hostname = request.url.hostname
    if scheme not in {"http", "https"} or hostname is None:
        return None
    port = request.url.port or (443 if scheme == "https" else 80)
    return scheme, hostname.lower(), port
