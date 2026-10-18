"""真实 SQLite 上验证条件补全、来源复用、上下文预算与流式最终校验。"""

import hashlib
import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import ask, generation, retrieval
from app.core.config import settings
from app.database import create_database_engine, initialize_database
from app.evidence.context import ContextIncomplete, expand_evidence
from app.models import Chunk, Document, KnowledgeBase


@pytest.fixture()
def context_db():
    engine = create_database_engine("sqlite+pysqlite://", pool_size=2, max_overflow=1, pool_recycle=1800, pool_timeout=2)
    initialize_database(engine)
    with Session(engine, expire_on_commit=False) as db:
        kb = KnowledgeBase(name="语义完整性测试")
        db.add(kb)
        db.commit()
        yield db, kb.id
    engine.dispose()


def add_source(db, kb_id, text, ranges):
    doc = Document(kb_id=kb_id, title="条件.md", file_path="unused/conditions.md",
                   content_hash=hashlib.sha256(text.encode()).hexdigest(), status="ready",
                   char_count=len(text), chunk_count=len(ranges))
    db.add(doc)
    db.flush()
    chunks = []
    for i, (start, end) in enumerate(ranges):
        chunk = Chunk(doc_id=doc.id, kb_id=kb_id, chunk_index=i, content=text[start:end],
                      char_start=start, char_end=end, embedding=[1.0] + [0.0] * (settings.embedding_dimension - 1))
        db.add(chunk)
        db.flush()
        chunks.append(retrieval.RetrievedChunk(chunk.id, doc.id, chunk.content, start, end, .1, .9, 1, None))
    db.commit()
    return doc, chunks


def run_answer(monkeypatch, db, kb, chunks, reply="必须保留条件。[1]", callback=None):
    monkeypatch.setattr(ask, "_embed_query", lambda _: [1.0])
    monkeypatch.setattr(retrieval, "retrieve", lambda *a, **kw: chunks)

    class Model:
        def complete(self, system, user):
            assert not db.in_transaction()
            if callback:
                callback(user)
            return reply

    return ask.answer_question(db, "开启缓存的条件是什么？", kb, llm=Model())


def test_old_mid_sentence_chunk_recovers_negation_and_keeps_both_sources(context_db, monkeypatch):
    db, kb = context_db
    text = "背景。" * 239 + "如果不开启缓存，性能会下降。"
    _, chunks = add_source(db, kb, text, [(0, 720), (720, len(text))])
    prompts = []
    answer = run_answer(monkeypatch, db, kb, [chunks[1]], callback=prompts.append)
    assert not answer.refused and len(answer.citations) == 2
    assert "如果不" in prompts[0] and "开启缓存，性能会下降" in prompts[0]
    assert prompts[0].index("如果不") < prompts[0].index("开启缓存，性能会下降")
    assert answer.evidence.candidate_count == 1 and answer.evidence.max_vector_similarity == .9
    assert {s.citation.chunk_id for s in answer.source_snapshots} == {c.chunk_id for c in chunks}


def test_pronoun_context_can_be_used_by_source_lookup(context_db, monkeypatch):
    db, kb = context_db
    first = "缓存用于保存重复查询的结果。"
    second = "它能够减少重复查询，并缩短响应时间。"
    text = first + second
    _, chunks = add_source(db, kb, text, [(0, len(first)), (len(first), len(text))])
    answer = run_answer(monkeypatch, db, kb, [chunks[1]])

    class SourceModel:
        def complete(self, system, user):
            return json.dumps({"source_matches": [{"source_index": 1, "subject": "缓存", "quote": second}]})

    sources = ask.answer_from_sources(db, "缓存减少重复查询的出处", kb, answer.source_snapshots, llm=SourceModel())
    assert not sources.refused and len(sources.citations) == 2


@pytest.mark.parametrize("limit", ["total", "neighbor"])
def test_required_context_is_never_truncated_to_fit_budget(context_db, monkeypatch, limit):
    db, kb = context_db
    first = "系统默认开启缓存。"
    text = first + "它可以提高查询效率。"
    _, chunks = add_source(db, kb, text, [(0, len(first)), (len(first), len(text))])
    monkeypatch.setattr(settings, "answer_context_max_chars" if limit == "total" else "answer_context_neighbor_chars", 5)
    result = run_answer(monkeypatch, db, kb, [chunks[1]], callback=lambda _: pytest.fail("不完整证据不能调用模型"))
    assert result.status == "needs_review" and result.refusal_reason == "context_incomplete"
    assert result.citations == [] and result.possible_sources and not db.in_transaction()


def test_context_does_not_cross_a_heading(context_db, monkeypatch):
    db, kb = context_db
    first = "# 第一节\n缓存已开启。\n"
    second = "# 第二节\n它会影响性能。"
    text = first + second
    split = len(first) + len("# 第二节\n")
    _, chunks = add_source(db, kb, text, [(0, len(first)), (len(first), split), (split, len(text))])
    result = run_answer(monkeypatch, db, kb, [chunks[2]])
    assert result.refusal_reason == "context_incomplete"


def test_missing_or_conflicting_source_coverage_is_rejected(context_db):
    db, kb = context_db
    _, chunks = add_source(db, kb, "如果不开启缓存，性能下降。", [(3, 13)])
    with pytest.raises(ContextIncomplete):
        expand_evidence(db, chunks, max_chars=12000, neighbor_chars=1600)


def test_source_change_during_generation_prevents_normal_answer_publication(context_db, monkeypatch):
    db, kb = context_db
    text = "如果不开启缓存，性能会下降。"
    doc, chunks = add_source(db, kb, text, [(0, len(text))])

    def change_source(_):
        db.execute(update(Document).where(Document.id == doc.id).values(content_hash="changed"))
        db.commit()

    result = run_answer(monkeypatch, db, kb, chunks, callback=change_source)
    assert result.refusal_reason == "source_changed" and result.citations == []


def test_gray_retrieval_does_not_expand_or_generate(context_db, monkeypatch):
    from dataclasses import replace
    db, kb = context_db
    text = "它缺少主语。"
    _, chunks = add_source(db, kb, text, [(0, len(text))])
    result = run_answer(monkeypatch, db, kb, [replace(chunks[0], vector_similarity=.5)], callback=lambda _: pytest.fail("灰区不生成"))
    assert result.refusal_reason == "borderline_relevance"


@pytest.mark.parametrize("streaming", [False, True])
def test_resolved_followup_reaches_generation_without_losing_original_request(context_db, monkeypatch, streaming):
    from app.answer_stream import AnswerCompleted, stream_prepared_answer
    db, kb = context_db
    text = "关闭缓存会增加重复查询的计算次数。"
    _, chunks = add_source(db, kb, text, [(0, len(text))])
    monkeypatch.setattr(ask, "_embed_query", lambda _: [1.0])
    monkeypatch.setattr(retrieval, "retrieve", lambda *a, **kw: chunks)
    monkeypatch.setattr(generation, "rewrite_query", lambda *a, **kw: "关闭缓存会影响重复查询吗？")
    prompts = []
    class Model:
        def complete(self, system, user):
            prompts.append(user)
            return "关闭缓存会增加计算次数。[1]"
    prepared = ask.prepare_answer(db, "它会有影响吗？请用两点说明。", kb,
                                  history=[("关闭缓存是什么意思？", "不复用缓存结果。")], llm=Model())
    if streaming:
        assert isinstance(list(stream_prepared_answer(prepared, llm=Model()))[-1], AnswerCompleted)
    else:
        ask.complete_prepared_answer(prepared, llm=Model())
    assert "关闭缓存会影响重复查询吗？" in prompts[0]
    assert "它会有影响吗？请用两点说明。" in prompts[0]


def test_implicit_condition_is_included_even_without_a_recognized_sentence_prefix(context_db):
    from app.chunking import split_markdown
    db, kb = context_db
    first = "# Cache experiment\n\n" + "The system receives repeated queries. " * 16 + "\n\n"
    condition = "For the experiment, the cache is disabled and all requests bypass any previously stored lookup results, so each repeated query must be recomputed from the source data.\n\n"
    conclusion = "In this situation, repeated queries are slower than with caching enabled."
    text = first + condition + conclusion
    split = split_markdown(text)
    assert len(split) == 2 and "cache is disabled" not in split[-1].text
    _, chunks = add_source(db, kb, text, [(c.char_start, c.char_end) for c in split])
    expanded, dependencies = expand_evidence(db, [chunks[-1]], max_chars=12000, neighbor_chars=1600)
    assert any("cache is disabled" in c.content for c in expanded)
    assert chunks[0].chunk_id in dependencies[chunks[-1].chunk_id]


def test_neutral_prefix_keeps_previous_sentence_window_without_recursive_full_document(context_db):
    db, kb = context_db
    sentences = [f"实验场景{i}的详细说明。" for i in range(8)]
    sentences[-2] = "实验中关闭了缓存。"
    sentences[-1] = "重复查询的耗时增加。"
    text = "".join(sentences)
    ends = [0]
    for sentence in sentences:
        ends.append(ends[-1] + len(sentence))
    _, chunks = add_source(db, kb, text, list(zip(ends, ends[1:])))
    expanded, dependencies = expand_evidence(db, [chunks[-1]], max_chars=12000, neighbor_chars=1600)
    assert chunks[-2].chunk_id in {c.chunk_id for c in expanded}
    # 普通窗口供生成理解，不强迫出处步骤把无显式依赖的相邻内容当成正式引用。
    assert dependencies[chunks[-1].chunk_id] == ()
    assert chunks[0].chunk_id not in {c.chunk_id for c in expanded}
