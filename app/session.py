"""会话上下文（决策 D5）：进程内存、不落库、带 TTL 与轮数上限。

边界规则（02 §3）：会话是 M3 的内部状态，不引入新模块、不建数据库表。
用途（04 DR5）：追问（指代句）先按上文改写为自包含问题，再走检索。

销毁策略：TTL 过期 + 轮数上限——个人单用户场景下内存占用可忽略；
V1.0 若要持久化问答历史，另起 conversations 表（03 §2.3 演进路线）。
"""

import time
from dataclasses import dataclass, field

from .core.config import settings


@dataclass
class Turn:
    """一轮问答：问题 + 回答（改写时作为上下文，需截断后使用）。"""

    question: str
    answer: str


@dataclass
class _Session:
    turns: list[Turn] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """进程内会话存储（单用户场景的轻量实现）。"""

    def __init__(self, ttl_seconds: float, max_turns: int) -> None:
        self._ttl = ttl_seconds
        self._max_turns = max_turns
        self._sessions: dict[str, _Session] = {}

    def history(self, session_id: str) -> list[Turn]:
        """取会话历史（触发一次过期清理）。"""
        self._evict_expired()
        session = self._sessions.get(session_id)
        return list(session.turns) if session is not None else []

    def append(self, session_id: str, turn: Turn) -> None:
        """追加一轮，并只保留最近 max_turns 轮（防上下文无限增长）。"""
        session = self._sessions.setdefault(session_id, _Session())
        session.turns.append(turn)
        session.turns = session.turns[-self._max_turns :]
        session.updated_at = time.monotonic()

    def clear(self, session_id: str) -> None:
        """显式结束会话（前端"新对话"时调用）。"""
        self._sessions.pop(session_id, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, session in self._sessions.items()
            if now - session.updated_at > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]


# 进程内单例：应用共享（app 内 import 即用）
store = SessionStore(
    ttl_seconds=settings.session_ttl_seconds,
    max_turns=settings.session_max_turns,
)
