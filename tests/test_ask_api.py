"""问答端点测试（M3）：POST /ask 与 GET /citations/{chunk_id} 的 HTTP 契约。"""

import pytest

from app import generation, ingest, storage
from app.core.config import settings
from app.models import Document

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"


class _FakeLLM:
    """替换生成层：返回预设回答。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, system: str, user: str) -> str:
        return self.reply


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
