"""会话与追问改写测试（D5 / 04 DR5）。

覆盖：会话存储的轮数截断与过期清理、改写提示构造、
改写失败退化（不改写也必须能检索）、端到端追问携带上文。
"""

import time

import pytest

from app import ask, generation, ingest, session, storage
from app.core.config import settings
from app.generation import LLMError
from app.models import Document
from app.session import SessionStore, Turn

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"


class _FakeLLM:
    """可编程假 LLM：按调用序返回不同内容（首次改写 → 再次生成）。"""

    def __init__(self, replies: list[str] | None = None, error: bool = False) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        if self.error:
            raise LLMError("模拟失败")
        return self.replies.pop(0) if self.replies else ""


# ── 会话存储 ────────────────────────────────────────────────

def test_store_keeps_recent_turns_only():
    """轮数上限：只保留最近 N 轮（防上下文无限增长）。"""
    store = SessionStore(ttl_seconds=60, max_turns=2)
    for i in range(4):
        store.append("s1", Turn(question=f"q{i}", answer=f"a{i}"))
    history = store.history("s1")
    assert [t.question for t in history] == ["q2", "q3"]


def test_store_evicts_expired_sessions():
    """TTL 过期：超时会话被清理（进程内存不无限增长）。"""
    store = SessionStore(ttl_seconds=0.01, max_turns=5)
    store.append("s1", Turn(question="q", answer="a"))
    time.sleep(0.02)
    assert store.history("s1") == []


def test_store_clear_ends_session():
    """显式结束会话（前端"新对话"）。"""
    store = SessionStore(ttl_seconds=60, max_turns=5)
    store.append("s1", Turn(question="q", answer="a"))
    store.clear("s1")
    assert store.history("s1") == []


def test_history_isolated_per_session():
    """不同会话互不串上下文。"""
    store = SessionStore(ttl_seconds=60, max_turns=5)
    store.append("s1", Turn(question="q1", answer="a1"))
    store.append("s2", Turn(question="q2", answer="a2"))
    assert [t.question for t in store.history("s1")] == ["q1"]
    assert [t.question for t in store.history("s2")] == ["q2"]


# ── 追问改写（DR5） ─────────────────────────────────────────

def test_rewrite_returns_original_without_history():
    """无历史 → 原样返回（不调 LLM）。"""
    llm = _FakeLLM(["不该被调用"])
    assert generation.rewrite_query("它怎么调？", [], provider=llm) == "它怎么调？"
    assert llm.calls == []


def test_rewrite_prompt_carries_history():
    """改写提示带上最近对话（指代句需要上文才能消解）。"""
    history = [Turn(question="TCP 拥塞控制是什么", answer="拥塞控制是……")]
    llm = _FakeLLM(["TCP 拥塞窗口如何调整？"])
    result = generation.rewrite_query("那它怎么调？", history, provider=llm)
    assert result == "TCP 拥塞窗口如何调整？"
    assert "TCP 拥塞控制是什么" in llm.calls[0]
    assert "那它怎么调？" in llm.calls[0]


def test_rewrite_empty_reply_falls_back():
    """模型返回空 → 退化为原问题（增强件不得成为链路单点）。"""
    history = [Turn(question="q", answer="a")]
    llm = _FakeLLM([""])
    assert generation.rewrite_query("它呢？", history, provider=llm) == "它呢？"


# ── 端到端：追问携带上文 ────────────────────────────────────

def _add_doc(db, kb_id: int, title: str, text: str) -> Document:
    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path=storage.save(kb_id, 9999, text.encode("utf-8")),
        content_hash=f"hash-{title}",
        char_count=len(text),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    ingest.process_document(doc.id, db)
    return doc


def test_followup_uses_rewritten_query(db, client, monkeypatch):
    """追问：检索用的是改写后的问题；无会话时用原问题。"""
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    store = SessionStore(ttl_seconds=60, max_turns=5)
    store.append("s1", Turn(question="TCP 三次握手是什么", answer="双方交换 SYN/ACK……"))

    llm = _FakeLLM(["TCP 三次握手的作用是什么？", "确认双方收发能力 [1]。"])
    result = ask.answer_question(db, "那它有什么用？", kb_id, session_id="s1",
                                 llm=llm, session_store=store)

    assert result.search_query == "TCP 三次握手的作用是什么？"   # 用了改写结果
    assert result.refused is False
    # 本轮已记录，供下一轮改写使用
    assert [t.question for t in store.history("s1")][-1] == "那它有什么用？"


def test_followup_without_session_uses_original(db, client, monkeypatch):
    """无会话：原问题直接检索（行为与 M3 单轮一致）。"""
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    store = SessionStore(ttl_seconds=60, max_turns=5)
    llm = _FakeLLM(["回答 [1]。"])
    result = ask.answer_question(db, "三次握手有什么用？", kb_id, llm=llm,
                                 session_store=store)
    assert result.search_query == "三次握手有什么用？"


def test_rewrite_failure_falls_back_to_original(db, client, monkeypatch):
    """改写失败（LLM 故障）→ 退化为原问题继续检索，不中断整条链路。"""
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    store = SessionStore(ttl_seconds=60, max_turns=5)
    store.append("s1", Turn(question="TCP 三次握手是什么", answer="……"))

    failing = _FakeLLM(error=True)          # 改写与生成都会失败
    result = ask.answer_question(db, "那它呢？", kb_id, session_id="s1",
                                 llm=failing, session_store=store)
    assert result.search_query == "那它呢？"                  # 退化用原问题
    assert result.refusal_reason == ask.REFUSAL_LLM_UNAVAILABLE  # 生成失败 → 拒答
