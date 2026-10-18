"""语义核对的发布约束；模拟模型判断，不声称测试了真实模型准确率。"""

import json

import pytest

from app import ask, generation
from app.answer_stream import AnswerCompleted, stream_prepared_answer
from app.evidence import CitationData
from app.evidence import support
from app.retrieval import RetrievedChunk
from app.session import SessionStore
from app.core.config import settings, Settings


@pytest.fixture(autouse=True)
def verification_provider(_fake_support_model, monkeypatch):
    monkeypatch.setattr(support, "get_support_provider", lambda: generation.get_llm_provider())


def prepared():
    source = "关闭缓存会让重复查询更慢。启用缓存会让重复查询更快。"
    chunk = RetrievedChunk(1, 1, source, 0, len(source), .03, .9, 1, None)
    citation = CitationData(1, 1, 1, "cache.md", source, 0, len(source))
    return ask.AnswerPreparation("关闭缓存是否更快？", "关闭缓存是否更快？", [chunk], [citation],
                                 "support-test", SessionStore(60, 3))


class Draft:
    def complete(self, system, user):
        return "关闭缓存会让重复查询更快。[1]"


class Verifier:
    def __init__(self, verdict="supported", transform=None):
        self.verdict = verdict
        self.transform = transform
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        payload = json.loads(user)
        judgments = [{"unit_id": unit["id"], "verdict": self.verdict,
                      "evidence": [{"source_index": payload["sources"][0]["index"],
                                    "quote": payload["sources"][0]["text"]}]
                      if self.verdict == "supported" else []}
                     for unit in payload["units"]]
        response = {"judgments": judgments}
        return json.dumps(self.transform(response) if self.transform else response, ensure_ascii=False)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("verdict", ["contradicted", "unsupported"])
def test_unsupported_draft_has_no_formal_citations_or_followup_history(monkeypatch, streaming, verdict):
    verifier = Verifier(verdict)
    monkeypatch.setattr(generation, "get_llm_provider", lambda: verifier)
    snapshot = prepared()
    if streaming:
        events = list(stream_prepared_answer(snapshot, llm=Draft()))
        assert isinstance(events[-1], AnswerCompleted)
        result = events[-1].result
    else:
        result = ask.complete_prepared_answer(snapshot, llm=Draft())
    assert verifier.calls == 1
    assert result.status == "unverified"
    assert result.refusal_reason == "answer_not_supported"
    assert result.citations == []
    assert snapshot.session_store.history("support-test") == []


@pytest.mark.parametrize("corrupt", [
    lambda value: {"judgments": []},
    lambda value: {"judgments": value["judgments"] * 2},
    lambda value: {"judgments": [{**value["judgments"][0], "unit_id": True}]},
    lambda value: {"judgments": [{**value["judgments"][0], "verdict": "maybe"}]},
    lambda value: {"judgments": [{**value["judgments"][0], "evidence": []}]},
    lambda value: {"judgments": [{**value["judgments"][0], "evidence": [{"source_index": 99, "quote": "未知原文"}]}]},
    lambda value: {"judgments": [{**value["judgments"][0], "evidence": [{"source_index": 1, "quote": "凭空编造的证据摘录"}]}]},
])
def test_invalid_verification_never_becomes_a_normal_answer(monkeypatch, corrupt):
    monkeypatch.setattr(generation, "get_llm_provider", lambda: Verifier(transform=corrupt))
    result = ask.complete_prepared_answer(prepared(), llm=Draft())
    assert result.refused and result.refusal_reason == "support_verification_failed"
    assert result.citations == []


def test_every_nonempty_answer_line_must_be_checked(monkeypatch):
    verifier = Verifier(transform=lambda value: {"judgments": value["judgments"][:1]})
    monkeypatch.setattr(generation, "get_llm_provider", lambda: verifier)
    result = ask.finish_generated_answer(prepared(), "第一项结论。[1]\n第二项没有引用的结论。")
    assert result.refused and result.refusal_reason == "support_verification_failed"


def test_supported_response_publishes_after_verification_and_source_recheck(monkeypatch):
    verifier = Verifier()
    monkeypatch.setattr(generation, "get_llm_provider", lambda: verifier)
    snapshot = prepared()
    snapshot.validate_sources = lambda _: set()
    result = ask.finish_generated_answer(snapshot, "关闭缓存会让重复查询更慢。[1]")
    assert result.status == "answered" and verifier.calls == 1
    assert result.citations[0].chunk_id == 1
    assert snapshot.session_store.history("support-test")[0].answer == result.content


def test_source_change_during_verification_still_blocks_publication(monkeypatch):
    changed = set()
    def mutate(value):
        changed.add(1)
        return value
    monkeypatch.setattr(generation, "get_llm_provider", lambda: Verifier(transform=mutate))
    snapshot = prepared()
    snapshot.validate_sources = lambda _: changed
    result = ask.finish_generated_answer(snapshot, "关闭缓存会让重复查询更慢。[1]")
    assert result.refusal_reason == "source_changed" and result.citations == []


@pytest.mark.parametrize("response", ["not-json", "```json\n{}\n```", None])
def test_unparseable_verification_is_explicitly_unverified(monkeypatch, response):
    class Invalid:
        def complete(self, system, user):
            return response
    monkeypatch.setattr(generation, "get_llm_provider", lambda: Invalid())
    assert ask.complete_prepared_answer(prepared(), llm=Draft()).refusal_reason == "support_verification_failed"


def test_network_failure_is_not_replaced_by_a_successful_draft(monkeypatch):
    def unavailable():
        raise generation.LLMError("offline")
    monkeypatch.setattr(generation, "get_llm_provider", unavailable)
    result = ask.complete_prepared_answer(prepared(), llm=Draft())
    assert result.status == "unverified" and result.refusal_reason == "support_verification_failed"


def test_budget_is_checked_before_model_call_without_partial_verification(monkeypatch):
    monkeypatch.setattr(settings, "answer_support_max_chars", 10)
    verifier = Verifier()
    monkeypatch.setattr(generation, "get_llm_provider", lambda: verifier)
    result = ask.complete_prepared_answer(prepared(), llm=Draft())
    assert result.refusal_reason == "support_context_limit" and verifier.calls == 0


def test_verification_is_default_on_and_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("ANSWER_SUPPORT_CHECK_ENABLED", raising=False)
    assert Settings(_env_file=None).answer_support_check_enabled is True
    monkeypatch.setattr(settings, "answer_support_check_enabled", False)
    monkeypatch.setattr(generation, "get_llm_provider", lambda: pytest.fail("disabled verifier was called"))
    result = ask.complete_prepared_answer(prepared(), llm=Draft())
    assert result.status == "answered"


def test_verifier_cannot_borrow_an_unrelated_citation_for_a_claim(monkeypatch):
    snapshot = prepared()
    snapshot.candidates.append(RetrievedChunk(2, 2, "另一份完全不同的原文。", 0, 11, .02, .8, 2, None))
    snapshot.candidate_citations.append(CitationData(2, 2, 2, "other.md", "另一份完全不同的原文。", 0, 11))
    def borrow(value):
        value["judgments"][0]["evidence"] = [{"source_index": 2, "quote": "另一份完全不同的原文。"}]
        return value
    monkeypatch.setattr(generation, "get_llm_provider", lambda: Verifier(transform=borrow))
    result = ask.finish_generated_answer(snapshot, "第一个结论。[1]\n第二个结论。[2]")
    assert result.refusal_reason == "support_verification_failed"


@pytest.mark.parametrize("streaming", [False, True])
def test_real_http_path_replaces_unsupported_draft_with_unverified_result(monkeypatch, streaming):
    import hashlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from app.database import create_database_engine, initialize_database
    from app.db import get_db
    from app.models import KnowledgeBase, Document, Chunk
    from app.routers.ask import router

    engine = create_database_engine("sqlite+pysqlite://", pool_size=1, max_overflow=0,
                                    pool_recycle=1800, pool_timeout=2)
    initialize_database(engine)
    with Session(engine) as db:
        kb = KnowledgeBase(name="Semantic check")
        db.add(kb)
        db.flush()
        source = prepared().candidate_citations[0].chunk_text
        doc = Document(kb_id=kb.id, title="cache.md", file_path="unused/cache.md", status="ready",
                       content_hash=hashlib.sha256(source.encode()).hexdigest(), char_count=len(source), chunk_count=1)
        db.add(doc)
        db.flush()
        db.add(Chunk(kb_id=kb.id, doc_id=doc.id, chunk_index=0, content=source, char_start=0,
                     char_end=len(source), embedding=[1.] + [0.] * (settings.embedding_dimension - 1)))
        db.commit()
        kb_id = kb.id
    def get_session():
        with Session(engine) as db:
            yield db
    class Model:
        def complete(self, system, user):
            return Verifier("contradicted").complete(system, user) if system == support.SYSTEM_PROMPT else Draft().complete(system, user)
    monkeypatch.setattr(generation, "get_llm_provider", lambda: Model())
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -2.)
    monkeypatch.setattr(settings, "answer_similarity_threshold", -1.)
    api = FastAPI()
    api.include_router(router, prefix="/api/v1")
    api.dependency_overrides[get_db] = get_session
    try:
        with TestClient(api) as client:
            response = client.post("/api/v1/ask/stream" if streaming else "/api/v1/ask",
                                   json={"question": "关闭缓存是否更快？", "kb_id": kb_id})
        assert response.status_code == 200
        if streaming:
            events = response.text.split("\n\n")
            final = next(event for event in events if event.startswith("event: result"))
            result = json.loads(final.split("data: ", 1)[1])
            assert response.text.count("event: result") == 1
            assert response.text.index("event: delta") < response.text.index("event: result")
        else:
            result = response.json()
        assert {key: result[key] for key in ("status", "refused", "refusal_reason", "citations")} == {
            "status": "unverified", "refused": True, "refusal_reason": "answer_not_supported", "citations": []}
    finally:
        engine.dispose()
