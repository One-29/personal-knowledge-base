"""跨 async/线程任务传播的请求关联标识。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import uuid4

_request_id: ContextVar[str] = ContextVar("knowbase_request_id", default="-")


def new_request_id() -> str:
    """生成不含用户输入、可放入响应头与日志的短关联标识。"""
    return uuid4().hex[:16]


def current_request_id() -> str:
    return _request_id.get()


def bind_request_id(request_id: str) -> Token[str]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)
