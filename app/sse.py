"""最小 SSE 编码器：所有业务数据以单行 JSON 传输。"""

from __future__ import annotations

import json
import re
from typing import Any

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def encode_event(event: str, data: Any) -> bytes:
    """编码一个命名事件；JSON 转义换行，避免破坏 SSE 帧边界。"""
    if _EVENT_NAME.fullmatch(event) is None:
        raise ValueError(f"非法 SSE 事件名: {event!r}")
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
