"""问答编排测试（M3）：引用校验（纯函数）+ 两级拒答（真库 + 假 LLM）。"""

import pytest

from app import ask, generation, ingest, storage
from app.core.config import settings
from app.generation import LLMError
from app.models import Document

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"


class _FakeLLM:
    """可编程假 LLM：返回预设文本，或抛错。"""

    def __init__(self, reply: str = "", error: bool = False) -> None:
        self.reply = reply
        self.error = error
        self.last_user_prompt = ""

    def complete(self, system: str, user: str) -> str:
        self.last_user_prompt = user
        if self.error:
            raise LLMError("模拟生成失败")
        return self.reply


def _query_vector() -> list[float]:
    return [0.01] * settings.embedding_dimension


@pytest.fixture()
def no_l1_threshold(monkeypatch) -> None:
    """关闭 L1 阈值。

    假 provider 的伪向量彼此近似正交（相似度≈0），若沿用默认 τ=0.35，
    所有测试都会停在 L1 拒答、走不到生成路径。测 L1 本身时另用 τ=1 的反例。
    """
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)


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


# ── 引用校验（纯函数，04 §5 第二层） ──────────────────────────

def test_parse_citations_dedupes_and_keeps_order():
    assert generation.parse_citations("甲[2]乙[1]丙[2]") == [2, 1]


def test_no_citations_is_not_invalid():
    """没有引用不算越界（是否算「无依据」由 L1/自检判定）。"""
    assert generation.find_invalid_citations("这句没有引用。", provided_count=3) == []


def test_valid_citations_pass():
    assert generation.find_invalid_citations("甲[1]乙[3]", provided_count=3) == []


def test_out_of_range_citation_detected():
    """越界引用（幻觉引用）被纯规则检出。"""
    assert generation.find_invalid_citations("甲[1]乙[5]", provided_count=3) == [5]


def test_zero_citation_detected():
    """[0] 不是合法编号（编号从 1 起）。"""
    assert generation.find_invalid_citations("甲[0]", provided_count=3) == [0]


def test_prompt_numbers_chunks_from_one(db, client):
    """提示词按 [1..n] 编号候选块（编号即回答可引用范围）。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)
    from app import retrieval

    chunks = retrieval.retrieve(db, "三次握手", _query_vector(), kb)
    prompt = generation.build_user_prompt("什么是三次握手？", chunks)
    assert "[1]" in prompt
    assert "三次握手" in prompt


# ── 问答编排与两级拒答 ──────────────────────────────────────

def test_answer_success_with_citations(db, client, no_l1_threshold):
    """正常问答：返回回答 + 被引用的块（供溯源）。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)
    llm = _FakeLLM(reply="三次握手用于确认双方收发能力 [1]。")

    result = ask.answer_question(db, "三次握手的作用？", kb, llm=llm)

    assert result.refused is False
    assert "[1]" in result.content
    assert len(result.citations) == 1
    assert result.citations[0].chunk_id > 0


def test_l1_refuse_on_empty_kb(db, client):
    """空库 → L1 拒答（empty_kb）。"""
    kb = client.post("/api/v1/kbs", json={"name": "空库"}).json()["id"]
    result = ask.answer_question(db, "任何问题", kb, llm=_FakeLLM(reply="不该被调用"))
    assert result.refused is True
    assert result.refusal_reason == ask.REFUSAL_EMPTY_KB


def test_l1_refuse_on_low_similarity(db, client, monkeypatch):
    """最高相似度低于 τ → L1 拒答（low_relevance）。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)
    # 把 τ 抬到不可能达到的高度，模拟"素材与问题不够相关"
    monkeypatch.setattr(settings, "refusal_similarity_threshold", 0.999999)

    result = ask.answer_question(db, "完全无关的问题", kb, llm=_FakeLLM(reply="不该被调用"))
    assert result.refused is True
    assert result.refusal_reason == ask.REFUSAL_LOW_RELEVANCE


def test_l2_refuse_on_invalid_citation(db, client, no_l1_threshold):
    """引用越界 → L2 拒答（invalid_citation，严格策略）。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)
    llm = _FakeLLM(reply="这是编造的回答 [7]。")          # 只提供了 1 块，[7] 越界

    result = ask.answer_question(db, "三次握手？", kb, llm=llm)

    assert result.refused is True
    assert result.refusal_reason == ask.REFUSAL_INVALID_CITATION
    assert result.citations == []


def test_llm_unavailable_refuses(db, client, no_l1_threshold):
    """生成服务不可用 → 拒答（llm_unavailable），不抛 500。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)

    result = ask.answer_question(db, "三次握手？", kb, llm=_FakeLLM(error=True))
    assert result.refused is True
    assert result.refusal_reason == ask.REFUSAL_LLM_UNAVAILABLE


def test_unknown_kb_raises_lookup_error(db, client):
    """指定不存在的库 → LookupError（由路由层转 404）。"""
    with pytest.raises(LookupError):
        ask.answer_question(db, "问题", 999999, llm=_FakeLLM(reply="x"))
