"""已有原文出处核对：真实 SQLite 检索、依赖、版本与接口回归。"""

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import ask, generation, retrieval, workflow
from app.core.config import settings
from app.database import create_database_engine, initialize_database
from app.db import get_db
from app.evidence import AnswerStatus
from app.models import Chunk, Document, KnowledgeBase
from app.routers import ask as ask_router
from app.routers import workflow as workflow_router

SEQUENCE = "数列极限：对任意正数 ε，存在正整数 N，当 n>N 时 |a_n-A|<ε。"
FUNCTION = "函数在 x₀ 处的双侧极限存在，当且仅当左右极限存在且相等。"
VECTOR = [1.0] + [0.0] * (settings.embedding_dimension - 1)


@pytest.fixture()
def sources_db():
    engine = create_database_engine(
        "sqlite+pysqlite://", pool_size=2, max_overflow=1, pool_recycle=1800, pool_timeout=2,
    )
    initialize_database(engine)
    with Session(engine, expire_on_commit=False) as db:
        kb = KnowledgeBase(name="出处回归库")
        db.add(kb)
        db.flush()
        doc = Document(
            kb_id=kb.id, title="01-函数与极限.md", file_path="unused/limits.md",
            content_hash="published-limits", status="ready", chunk_count=2,
            char_count=len(SEQUENCE) + len(FUNCTION),
        )
        db.add(doc)
        db.flush()
        for index, content in enumerate([SEQUENCE, FUNCTION]):
            start = 0 if index == 0 else len(SEQUENCE)
            db.add(Chunk(
                kb_id=kb.id, doc_id=doc.id, chunk_index=index, content=content,
                char_start=start, char_end=start + len(content), embedding=VECTOR,
            ))
        db.commit()
        yield db, kb.id, doc.id
    engine.dispose()


class SourceLLM:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        if "拆解成知识查询" in system:
            return json.dumps(self.plan, ensure_ascii=False)
        if "source_matches" in system:
            context, question = user.split("\n\n问题：", 1)
            needle = "n>N" if "数列" in question else "左右极限"
            matches = re.findall(
                r"\[(\d+)\] 文档名称：(.*?)(?=\n\n\[\d+\]|$)", context, re.S,
            )
            selected = [
                {"source_index": int(index), "subject": "数列" if "数列" in question else "函数",
                 "quote": body.split("原文：\n", 1)[1].strip()}
                for index, body in matches if needle in body
            ]
            return json.dumps({"source_matches": selected})
        return "数列使用 ε–N 定义；函数在点处可核对左右极限。[1][2]"


def plan_with_sources():
    return [
        {"goal": "比较极限", "query": "比较数列极限和函数极限并给出处", "kind": "research"},
        {"goal": "数列出处", "query": "列出数列极限定义的出处", "kind": "source_lookup", "depends_on": [1]},
        {"goal": "函数出处", "query": "列出函数双侧极限判据的出处", "kind": "source_lookup", "depends_on": [1]},
    ]


def counters(monkeypatch):
    counts = {"embedding": 0, "retrieval": 0}
    original = retrieval.retrieve

    def embed(query):
        counts["embedding"] += 1
        return VECTOR

    def retrieve(*args, **kwargs):
        counts["retrieval"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ask, "_embed_query", embed)
    monkeypatch.setattr(retrieval, "retrieve", retrieve)
    return counts


def test_comparison_source_steps_reuse_correct_spans_without_retrieval(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counts = counters(monkeypatch)
    llm = SourceLLM(plan_with_sources())
    result = workflow.run_workflow(db, "比较数列极限和函数极限，并且给出出处", kb_id, llm=llm)

    assert [step.status for step in result.steps] == ["answered"] * 3
    assert counts == {"embedding": 1, "retrieval": 1}
    assert len(llm.calls) == 4  # 规划、比较、两次局部原文核对。
    assert "n>N" in result.steps[1].citations[0].chunk_text
    assert "左右极限" in result.steps[2].citations[0].chunk_text
    assert len(result.citations) == 2
    assert len({c.doc_id for c in result.citations}) == 1
    for step in result.steps[1:]:
        assert step.search_query is None and step.evidence is None
        assert step.evidence_origin == "previous_step" and step.depends_on == [1]
        citation = step.citations[0]
        assert f"[{citation.index}]" in step.conclusion
        assert citation.chunk_id == result.citations[citation.index - 1].chunk_id
    assert "知识库无相关内容" not in result.answer
    assert not db.in_transaction()


def test_single_comparison_plan_keeps_sources_in_one_answer(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counts = counters(monkeypatch)
    llm = SourceLLM(plan_with_sources()[:1])
    result = workflow.run_workflow(db, "比较数列极限和函数极限并给出处", kb_id, llm=llm)
    assert len(result.steps) == 1 and result.steps[0].status == "answered"
    assert len(result.citations) == 2 and len(llm.calls) == 2
    assert counts == {"embedding": 1, "retrieval": 1}
    assert result.steps[0].evidence.max_vector_similarity >= 0.55


def test_all_sources_are_metadata_only_and_deduplicated(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))

    class NoLLM:
        def complete(self, *args):
            pytest.fail("列出全部已有引用不需要模型调用")

    monkeypatch.setattr(ask, "_embed_query", lambda *args: pytest.fail("出处整理不能向量化"))
    monkeypatch.setattr(retrieval, "retrieve", lambda *args, **kwargs: pytest.fail("出处整理不能检索"))
    result = ask.answer_from_sources(
        db, "列出上一步全部出处", kb_id,
        parent.source_snapshots * 2, source_scope="all", llm=NoLLM(),
    )
    assert result.status == AnswerStatus.ANSWERED and len(result.citations) == 2
    assert "01-函数与极限.md" in result.content
    assert result.search_query is None and result.evidence is None


@pytest.mark.parametrize("response,reason", [
    ('{"source_matches": []}', "source_not_supported"),
    ('{"source_matches": [{"source_index": 99, "subject": "数列", "quote": "数列"}]}', "invalid_source_selection"),
    ('{"source_matches": [{"source_index": true, "subject": "数列", "quote": "数列"}]}', "invalid_source_selection"),
    ('{"source_matches": [], "extra": "invented"}', "invalid_source_selection"),
    ("资料不足，无法回答。", "invalid_source_selection"),
])
def test_uncovered_or_invalid_source_selection_is_not_answered(sources_db, monkeypatch, response, reason):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))

    class Selection:
        def complete(self, system, user):
            return response

    result = ask.answer_from_sources(db, "所有数列极限判别方法的出处", kb_id, parent.source_snapshots, llm=Selection())
    assert result.refused and result.refusal_reason == reason and result.citations == []
    assert result.evidence is None and "知识库无相关内容" not in result.content


@pytest.mark.parametrize("mismatch", ["other_subject", "invented_quote", "subject_absent_from_source", "short_quote"])
def test_source_selection_requires_question_subject_and_literal_source_evidence(sources_db, monkeypatch, mismatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))
    sequence = next(c for c in parent.citations if "n>N" in c.chunk_text)
    match = {"source_index": sequence.index, "subject": "数列", "quote": sequence.chunk_text}
    if mismatch == "other_subject":
        match.update(subject="函数", quote=FUNCTION)
    elif mismatch == "invented_quote":
        match["quote"] = "数列极限可使用不存在的判别方法。"
    elif mismatch == "subject_absent_from_source":
        match["subject"] = "定义"
    else:
        match["quote"] = "n>N"

    class UnsupportedSelection:
        def complete(self, system, user):
            assert "先给结论" not in user and "比较多个对象" not in user
            return json.dumps({"source_matches": [match]})

    result = ask.answer_from_sources(db, "数列极限定义的出处", kb_id, parent.source_snapshots, llm=UnsupportedSelection())
    assert result.refusal_reason == "invalid_source_selection" and result.citations == []


def test_source_quote_can_use_subject_in_chunk_heading_and_ignore_whitespace(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))
    sequence = next(c for c in parent.citations if "n>N" in c.chunk_text)

    class DefinitionSelection:
        def complete(self, system, user):
            quote = sequence.chunk_text.split("：", 1)[1].replace(" ", "\n")
            return json.dumps({"source_matches": [{
                "source_index": sequence.index, "subject": "数列极限", "quote": quote,
            }]})

    result = ask.answer_from_sources(db, "数列极限判别方法的出处", kb_id, parent.source_snapshots, llm=DefinitionSelection())
    assert result.status == AnswerStatus.ANSWERED and len(result.citations) == 1
    assert result.citations[0].chunk_id == sequence.chunk_id


def test_source_scope_and_published_version_are_checked(sources_db, monkeypatch):
    db, kb_id, doc_id = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))
    db.execute(update(Document).where(Document.id == doc_id).values(
        ingest_version=2, status="processing", pending_content_hash="pending-new-content",
    ))
    db.commit()
    # 更新尚未发布，旧索引的 content_hash 与块仍可使用。
    result = ask.answer_from_sources(db, "全部出处", kb_id, parent.source_snapshots, source_scope="all")
    assert result.status == AnswerStatus.ANSWERED

    other = KnowledgeBase(name="其它库")
    db.add(other)
    db.commit()
    assert ask.answer_from_sources(db, "出处", other.id, parent.source_snapshots, source_scope="all").refusal_reason == "source_changed"
    db.execute(update(Document).where(Document.id == doc_id).values(content_hash="published-new-content"))
    db.commit()
    assert ask.answer_from_sources(db, "出处", kb_id, parent.source_snapshots, source_scope="all").refusal_reason == "source_changed"


def test_deleted_chunk_is_not_reused(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))
    db.execute(delete(Chunk).where(Chunk.id == parent.citations[0].chunk_id))
    db.commit()
    assert ask.answer_from_sources(db, "出处", kb_id, parent.source_snapshots, source_scope="all").refusal_reason == "source_changed"


def test_changed_source_during_model_call_invalidates_parent_and_dependent(sources_db, monkeypatch):
    db, kb_id, doc_id = sources_db
    counters(monkeypatch)

    class ChangingLLM(SourceLLM):
        def complete(self, system, user):
            if "source_matches" in system:
                assert not db.in_transaction()
                db.execute(update(Document).where(Document.id == doc_id).values(content_hash="changed"))
                db.commit()
            return super().complete(system, user)

    result = workflow.run_workflow(db, "比较并给出处", kb_id, llm=ChangingLLM(plan_with_sources()[:2]))
    assert [step.status for step in result.steps] == ["error", "error"]
    assert result.citations == [] and "请重新执行" in result.answer


def test_final_source_validation_database_failure_is_not_reported_as_missing_content(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    plans = plan_with_sources()[:2]
    plans[1]["source_scope"] = "all"
    original = ask.invalid_source_chunk_ids

    def fail_final_validation(session, sources, scope):
        if len(sources) > 2:  # 汇总核对包括前序与出处步骤的来源。
            session.execute(select(Document.id))
            raise SQLAlchemyError("fake final validation outage")
        return original(session, sources, scope)

    monkeypatch.setattr(ask, "invalid_source_chunk_ids", fail_final_validation)
    result = workflow.run_workflow(db, "比较并列出全部出处", kb_id, llm=SourceLLM(plans))
    assert [step.status for step in result.steps] == ["error", "error"]
    assert all(step.refusal_reason == "source_validation_failed" for step in result.steps)
    assert all(step.conclusion is None and step.citations == [] for step in result.steps)
    assert result.citations == [] and "fake final validation outage" not in result.answer
    assert "知识库无相关内容" not in result.answer and not db.in_transaction()


def test_source_lookup_only_uses_previously_cited_chunks(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counts = counters(monkeypatch)

    class OneCitationLLM(SourceLLM):
        def complete(self, system, user):
            if "拆解成知识查询" not in system and "source_matches" not in system:
                self.calls.append((system, user))
                context = user.split("\n\n问题：", 1)[0]
                sequence_index = next(index for index, body in re.findall(
                    r"\[(\d+)\] 文档名称：(.*?)(?=\n\n\[\d+\]|$)", context, re.S,
                ) if "n>N" in body)
                return f"数列极限使用 ε–N 定义。[{sequence_index}]"
            return super().complete(system, user)

    plans = [plan_with_sources()[0], plan_with_sources()[2]]
    result = workflow.run_workflow(db, "比较并核对函数出处", kb_id, llm=OneCitationLLM(plans))
    assert result.steps[0].status == "answered" and len(result.steps[0].citations) == 1
    assert result.steps[1].refusal_reason == "source_not_supported"
    assert result.steps[1].citations == [] and len(result.citations) == 1
    assert counts == {"embedding": 1, "retrieval": 1}


def test_new_knowledge_research_does_not_bypass_similarity_gate(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counts = counters(monkeypatch)
    embed = ask._embed_query

    def embed_with_low_relevance(query):
        vector = embed(query)
        return [-value for value in vector] if "所有" in query else vector

    monkeypatch.setattr(ask, "_embed_query", embed_with_low_relevance)
    plans = [plan_with_sources()[0], {
        "goal": "新知识", "query": "函数极限的所有判别方法", "kind": "research",
    }]
    llm = SourceLLM(plans)
    result = workflow.run_workflow(db, "比较并另查所有判别方法", kb_id, llm=llm)
    assert result.steps[0].status == "answered"
    assert result.steps[1].status == "insufficient"
    assert result.steps[1].refusal_reason == "low_relevance"
    assert result.steps[1].evidence_origin == "retrieval" and result.steps[1].citations == []
    assert counts == {"embedding": 2, "retrieval": 2} and len(llm.calls) == 2


@pytest.mark.parametrize("gray", [False, True])
def test_unavailable_parent_does_not_trigger_source_generation_and_later_research_continues(sources_db, monkeypatch, gray):
    db, kb_id, _ = sources_db
    plans = plan_with_sources()[:2] + [{"goal": "其它知识", "query": "确定问题", "kind": "research"}]

    class ParentLLM(SourceLLM):
        def complete(self, system, user):
            if "source_matches" in system:
                pytest.fail("没有前序正式引用，不能调用来源模型")
            if "拆解成知识查询" not in system and "确定问题" not in user:
                return "资料不足，无法回答。"
            return super().complete(system, user)

    borderline = [0.5, 0.8660254037844386] + [0.0] * (settings.embedding_dimension - 2)
    monkeypatch.setattr(ask, "_embed_query", lambda query: borderline if gray and query != "确定问题" else VECTOR)
    result = workflow.run_workflow(db, "任务", kb_id, llm=ParentLLM(plans))
    assert result.steps[1].refusal_reason == "dependency_unavailable"
    assert result.steps[1].status == ("needs_review" if gray else "insufficient")
    assert result.steps[1].citations == [] and result.steps[2].status == "answered"
    if not gray:
        assert result.steps[0].refusal_reason == "generation_insufficient"
        assert result.steps[0].evidence.band.value == "sufficient"


@pytest.mark.parametrize("invalid", [
    {"kind": []}, {"kind": "unknown"}, {"source_scope": []},
    {"kind": "research", "depends_on": [1]},
    {"kind": "source_lookup", "depends_on": []},
    {"kind": "source_lookup", "depends_on": [2]},
    {"kind": "source_lookup", "depends_on": [True]},
    {"kind": "source_lookup", "depends_on": [1, 1]},
])
def test_invalid_dependency_plan_falls_back_to_original_task(invalid):
    plan = [{"goal": "知识", "query": "知识"}, {"goal": "出处", "query": "出处", **invalid}]
    assert workflow.parse_plan(json.dumps(plan), 5) == []
    assert workflow.plan_steps("原始任务", 5, provider=SourceLLM(plan))[0].query == "原始任务"


def test_workflow_and_ask_http_expose_distinct_reasons_and_source_origin(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counts = counters(monkeypatch)
    llm = SourceLLM(plan_with_sources())
    monkeypatch.setattr(generation, "get_llm_provider", lambda: llm)
    app = FastAPI()
    app.include_router(workflow_router.router, prefix="/api/v1")
    app.include_router(ask_router.router, prefix="/api/v1")

    def session_override():
        yield db

    app.dependency_overrides[get_db] = session_override
    with TestClient(app) as client:
        response = client.post("/api/v1/workflow", json={"task": "比较并给出处", "kb_id": kb_id})
        assert response.status_code == 200
        body = response.json()
        assert body["steps"][0]["evidence"]["band"] == "sufficient"
        assert body["steps"][1]["kind"] == "source_lookup"
        assert body["steps"][1]["depends_on"] == [1]
        assert body["steps"][1]["evidence_origin"] == "previous_step"
        assert body["steps"][1]["search_query"] is None
        assert body["steps"][1]["evidence"] is None
        assert "indexed_content_hash" not in response.text and "source_snapshots" not in response.text
        assert counts == {"embedding": 1, "retrieval": 1}

        class Insufficient:
            def complete(self, system, user):
                assert "文档名称：01-函数与极限.md" in user
                return "资料不足，无法回答。"

        monkeypatch.setattr(generation, "get_llm_provider", lambda: Insufficient())
        payload = {"question": "原文未记载的内容", "kb_id": kb_id}
        ordinary = client.post("/api/v1/ask", json=payload).json()
        streamed = client.post("/api/v1/ask/stream", json=payload)
        events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
        assert ordinary["refusal_reason"] == events[-1]["refusal_reason"] == "generation_insufficient"
        assert ordinary["evidence"]["band"] == "sufficient"


def test_source_context_limit_fails_explicitly(sources_db, monkeypatch):
    db, kb_id, _ = sources_db
    counters(monkeypatch)
    parent = ask.answer_question(db, "比较极限", kb_id, llm=SourceLLM([]))
    monkeypatch.setattr(settings, "workflow_source_max_chars", 1)
    result = ask.answer_from_sources(db, "出处", kb_id, parent.source_snapshots, llm=SourceLLM([]))
    assert result.refusal_reason == "source_context_limit" and result.status == AnswerStatus.ERROR
