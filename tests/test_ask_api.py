"""问答端点测试（M3）：同步/流式问答与引用溯源的 HTTP 契约。"""

import json

import pytest

from app import generation, ingest, session, storage
from app.core.config import settings
from app.models import Document

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"


class _FakeLLM:
    """替换生成层：返回预设回答。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, system: str, user: str) -> str:
        return self.reply


class _StreamingFakeLLM(_FakeLLM):
    def __init__(self, *parts: str) -> None:
        super().__init__("".join(parts))
        self.parts = parts

    def stream(self, system: str, user: str):
        yield from self.parts


def _sse_events(response) -> list[tuple[str, dict]]:
    events = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


@pytest.fixture()
def no_l1_threshold(monkeypatch) -> None:
    """关闭 L1 阈值（假向量彼此近似正交，默认 τ 会拦住所有测试）。"""
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)


@pytest.fixture()
def fake_llm(monkeypatch):
    """按测试需要设置假 LLM 回答。返回可调用对象：set_reply("...")。"""

    holder = {"llm": _FakeLLM("")}

    def _set(reply: str) -> None:
        holder["llm"] = _FakeLLM(reply)

    monkeypatch.setattr(generation, "get_llm_provider", lambda: holder["llm"])
    return _set


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


def test_ask_returns_answer_with_citations(client, db, fake_llm, no_l1_threshold):
    """正常问答：200 + 回答 + 引用列表（含文档标题与偏移）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("三次握手确认双方收发能力 [1]。")

    resp = client.post("/api/v1/ask", json={"question": "三次握手的作用？", "kb_id": kb_id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is False
    assert "[1]" in body["content"]
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["index"] == 1
    assert citation["doc_title"] == "tcp.md"
    assert citation["char_start"] == 0
    assert citation["chunk_id"] > 0


def test_ask_stream_returns_deltas_then_validated_result(
    client, db, no_l1_threshold, monkeypatch
):
    kb_id = client.post("/api/v1/kbs", json={"name": "流式网络库"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    monkeypatch.setattr(
        generation,
        "get_llm_provider",
        lambda: _StreamingFakeLLM("三次握手", "确认收发能力 [1]。"),
    )

    response = client.post(
        "/api/v1/ask/stream",
        json={"question": "三次握手的作用？", "kb_id": kb_id},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    events = _sse_events(response)
    assert [kind for kind, _ in events] == ["metadata", "delta", "delta", "result"]
    assert events[0][1]["search_query"] == "三次握手的作用？"
    assert [event[1]["content"] for event in events[1:3]] == [
        "三次握手",
        "确认收发能力 [1]。",
    ]
    result = events[-1][1]
    assert result["content"] == "三次握手确认收发能力 [1]。"
    assert result["refused"] is False
    assert result["citations"][0]["doc_title"] == "tcp.md"


def test_ask_stream_l1_refusal_sends_no_untrusted_delta(client):
    kb_id = client.post("/api/v1/kbs", json={"name": "流式空库"}).json()["id"]

    response = client.post(
        "/api/v1/ask/stream",
        json={"question": "这里有什么？", "kb_id": kb_id},
    )

    events = _sse_events(response)
    assert [kind for kind, _ in events] == ["metadata", "result"]
    assert events[-1][1]["refused"] is True
    assert events[-1][1]["refusal_reason"] == "empty_kb"


def test_ask_stream_replaces_invalid_citation_draft_with_refusal(
    client, db, no_l1_threshold, monkeypatch
):
    kb_id = client.post("/api/v1/kbs", json={"name": "流式引用校验"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    monkeypatch.setattr(
        generation,
        "get_llm_provider",
        lambda: _StreamingFakeLLM("尚未校验的内容 [9]。"),
    )

    response = client.post(
        "/api/v1/ask/stream",
        json={"question": "三次握手？", "kb_id": kb_id},
    )

    events = _sse_events(response)
    assert [kind for kind, _ in events] == ["metadata", "delta", "result"]
    assert events[1][1]["content"] == "尚未校验的内容 [9]。"
    assert events[-1][1]["refused"] is True
    assert events[-1][1]["refusal_reason"] == "invalid_citation"
    assert "[9]" not in events[-1][1]["content"]


def test_ask_stream_provider_failure_after_delta_finishes_as_refusal(
    client, db, no_l1_threshold, monkeypatch
):
    kb_id = client.post("/api/v1/kbs", json={"name": "流式供应商失败"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    class _FailingStream:
        def complete(self, system: str, user: str) -> str:
            raise AssertionError("流式路径不应调用 complete")

        def stream(self, system: str, user: str):
            yield "未完成草稿"
            raise generation.LLMError("模拟流中断")

    monkeypatch.setattr(generation, "get_llm_provider", lambda: _FailingStream())
    response = client.post(
        "/api/v1/ask/stream",
        json={"question": "三次握手？", "kb_id": kb_id},
    )

    events = _sse_events(response)
    assert [kind for kind, _ in events] == ["metadata", "delta", "result"]
    assert events[-1][1]["refused"] is True
    assert events[-1][1]["refusal_reason"] == "llm_unavailable"


def test_ask_stream_unexpected_failure_returns_safe_correlated_event_without_history(
    client, db, no_l1_threshold, monkeypatch
):
    kb_id = client.post("/api/v1/kbs", json={"name": "流式内部失败"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    session_id = "stream-internal-error"
    session.store.clear(session_id)

    class _BrokenStream:
        def complete(self, system: str, user: str) -> str:
            raise AssertionError("流式路径不应调用 complete")

        def stream(self, system: str, user: str):
            yield "草稿"
            raise ValueError("不得回显的内部细节")

    monkeypatch.setattr(generation, "get_llm_provider", lambda: _BrokenStream())
    response = client.post(
        "/api/v1/ask/stream",
        json={
            "question": "三次握手？",
            "kb_id": kb_id,
            "session_id": session_id,
        },
    )

    events = _sse_events(response)
    assert [kind for kind, _ in events] == ["metadata", "delta", "error"]
    assert events[-1][1] == {
        "detail": "流式回答中断，请复制诊断信息后重试",
        "request_id": response.headers["x-request-id"],
    }
    assert "不得回显" not in response.text
    assert session.store.history(session_id) == []


def test_ask_stream_unknown_kb_is_regular_404(client):
    response = client.post(
        "/api/v1/ask/stream",
        json={"question": "问题", "kb_id": 999999},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "知识库不存在"


def test_ask_with_history_rewrites_followup(client, db, no_l1_threshold, monkeypatch):
    """请求携带 history（前端持久化的会话）时，用它做追问改写的上下文。"""
    from app import generation

    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    replies = ["TCP 三次握手有什么好处？", "确认双方收发能力 [1]。"]

    class _Scripted:
        def complete(self, system: str, user: str) -> str:
            return replies.pop(0) if replies else ""

    monkeypatch.setattr(generation, "get_llm_provider", lambda: _Scripted())

    resp = client.post("/api/v1/ask", json={
        "question": "那它有什么好处？",
        "kb_id": kb_id,
        "history": [{"question": "TCP 为什么需要三次握手？", "answer": "确认双方收发能力 [1]。"}],
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["search_query"] == "TCP 三次握手有什么好处？"   # 用了 history 做改写
    assert body["refused"] is False


def test_ask_refusal_is_200(client, db, fake_llm):
    """拒答是正常业务结果：HTTP 200 + refused=true（非错误响应）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "空库"}).json()["id"]
    fake_llm("不该被调用")

    resp = client.post("/api/v1/ask", json={"question": "任何问题", "kb_id": kb_id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is True
    assert body["refusal_reason"] == "empty_kb"
    assert body["citations"] == []


def test_ask_invalid_citation_refused(client, db, fake_llm, no_l1_threshold):
    """越界引用 → L2 拒答（HTTP 200 + invalid_citation）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("编造的内容 [9]。")

    resp = client.post("/api/v1/ask", json={"question": "三次握手？", "kb_id": kb_id})

    assert resp.status_code == 200
    assert resp.json()["refusal_reason"] == "invalid_citation"


def test_ask_no_citation_refused(client, db, fake_llm, no_l1_threshold):
    """有实质内容却零引用 → L2 拒答（no_citation，不可溯源）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("三次握手用于确认双方收发能力。")        # 没有任何 [n]

    resp = client.post("/api/v1/ask", json={"question": "三次握手？", "kb_id": kb_id})

    assert resp.status_code == 200
    assert resp.json()["refusal_reason"] == "no_citation"


def test_ask_insufficient_answer_maps_to_low_relevance(client, db, fake_llm, no_l1_threshold):
    """模型自述「资料不足」→ 归入 low_relevance（文案更贴切）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("资料不足，无法回答。")

    resp = client.post("/api/v1/ask", json={"question": "三次握手？", "kb_id": kb_id})
    assert resp.json()["refusal_reason"] == "low_relevance"


def test_ask_unknown_kb_404(client, fake_llm):
    """指定不存在的知识库 → 404。"""
    resp = client.post("/api/v1/ask", json={"question": "问题", "kb_id": 999999})
    assert resp.status_code == 404


def test_ask_empty_question_422(client, fake_llm):
    """空问题 → 422（Pydantic 校验）。"""
    assert client.post("/api/v1/ask", json={"question": ""}).status_code == 422


def test_ask_all_kbs_without_kb_id(client, db, fake_llm, no_l1_threshold):
    """kb_id 缺省 = 全库检索。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("回答 [1]。")

    resp = client.post("/api/v1/ask", json={"question": "三次握手？"})
    assert resp.status_code == 200
    assert resp.json()["refused"] is False


def test_citation_endpoint_returns_span(client, db, fake_llm, no_l1_threshold):
    """溯源端点：按 chunk_id 取回原文片段与字符区间（US-M3-03）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("三次握手确认收发能力 [1]。")

    answer = client.post("/api/v1/ask", json={"question": "三次握手？", "kb_id": kb_id}).json()
    chunk_id = answer["citations"][0]["chunk_id"]

    resp = client.get(f"/api/v1/citations/{chunk_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["doc_title"] == "tcp.md"
    assert "三次握手" in detail["chunk_text"]
    assert detail["char_start"] == 0
    assert detail["char_end"] > 0


def test_citation_endpoint_404_for_missing_chunk(client):
    """引用不存在（块已被重传替换）→ 404。"""
    assert client.get("/api/v1/citations/999999").status_code == 404


def test_answer_keeps_citation_snapshot_after_source_is_deleted(
    client, db, fake_llm, no_l1_threshold
):
    """历史回答保留引用快照，原块删除后前端仍有可核对的原文。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "历史引用"}).json()["id"]
    doc = _add_doc(db, kb_id, "tcp.md", DOC_TCP)
    fake_llm("三次握手确认双方收发能力 [1]。")

    answer = client.post(
        "/api/v1/ask", json={"question": "三次握手的作用？", "kb_id": kb_id}
    ).json()
    citation = answer["citations"][0]

    assert client.delete(f"/api/v1/documents/{doc.id}").status_code == 204
    assert client.get(f"/api/v1/citations/{citation['chunk_id']}").status_code == 404
    assert citation["doc_id"] == doc.id
    assert citation["doc_title"] == "tcp.md"
    assert citation["chunk_text"] == DOC_TCP
    assert citation["char_start"] == 0
    assert citation["char_end"] == len(DOC_TCP)
