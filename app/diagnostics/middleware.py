"""为每个 HTTP 请求注入关联 ID，并记录不含正文的完成日志。"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context import bind_request_id, new_request_id, reset_request_id

logger = logging.getLogger("app.diagnostics.request")


class RequestDiagnosticsMiddleware:
    """纯 ASGI 中间件，避免读取或缓冲请求/响应正文。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = bind_request_id(request_id)
        started = perf_counter()
        status_code = 500
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except asyncio.CancelledError:
            logger.warning(
                "request method=%s path=%s status=cancelled duration_ms=%.1f",
                method,
                path,
                (perf_counter() - started) * 1000,
            )
            raise
        except Exception:
            # 具体堆栈由应用级异常处理器记录；这里保留总耗时且避免重复堆栈。
            logger.error(
                "request method=%s path=%s status=500 duration_ms=%.1f",
                method,
                path,
                (perf_counter() - started) * 1000,
            )
            raise
        else:
            if _should_log(path):
                level = logging.WARNING if status_code >= 500 else logging.INFO
                logger.log(
                    level,
                    "request method=%s path=%s status=%d duration_ms=%.1f",
                    method,
                    path,
                    status_code,
                    (perf_counter() - started) * 1000,
                )
        finally:
            reset_request_id(token)


def _should_log(path: str) -> bool:
    return path.startswith("/api/") or path in {"/health", "/ready"}
