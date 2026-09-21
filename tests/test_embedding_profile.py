"""embedding 模型指纹：采用、切换、拒绝混用与业务降级。"""

from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import ask, embedding, embedding_profile, ingest
from app.database import create_database_engine, initialize_database
from app.models import AppMetadata, Chunk, Document, KnowledgeBase


@pytest.fixture()
def profile_db():
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=1,
    )
    initialize_database(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db
    engine.dispose()


def _profile(suffix: str, dimension: int = 3) -> embedding_profile.EmbeddingProfile:
    return embedding_profile.EmbeddingProfile(
        base_url=f"https://embedding-{suffix}.example/v1",
        model=f"model-{suffix}",
        dimension=dimension,
    )


def _add_ready_document(db: Session) -> tuple[KnowledgeBase, Document, Chunk]:
    kb = KnowledgeBase(name="指纹测试库")
    db.add(kb)
    db.flush()
    doc = Document(
        kb_id=kb.id,
        title="profile.md",
        file_path="unused/profile.md",
        content_hash="profile-hash",
        char_count=4,
        chunk_count=1,
        status="ready",
    )
    db.add(doc)
    db.flush()
    chunk = Chunk(
        doc_id=doc.id,
        kb_id=kb.id,
        chunk_index=0,
        content="已有向量",
        char_start=0,
        char_end=4,
        embedding=[1.0, 0.0, 0.0],
    )
    db.add(chunk)
    db.commit()
    return kb, doc, chunk


def test_empty_database_adopts_and_can_switch_profile(profile_db):
    first = _profile("first")
    second = _profile("second", dimension=4)

    assert embedding_profile.ensure_embedding_profile(profile_db, first) == first
    assert not profile_db.in_transaction()
    assert embedding_profile.ensure_embedding_profile(profile_db, second) == second
    assert embedding_profile.stored_embedding_profile(profile_db) == second
    assert len(second.fingerprint) == 64
    assert not profile_db.in_transaction()


def test_existing_vectors_reject_profile_change(profile_db):
    first = _profile("first")
    second = _profile("second")
    _add_ready_document(profile_db)
    embedding_profile.ensure_embedding_profile(profile_db, first)

    with pytest.raises(
        embedding_profile.EmbeddingProfileMismatch,
        match="重建全部向量",
    ):
        embedding_profile.ensure_embedding_profile(profile_db, second)

    assert embedding_profile.stored_embedding_profile(profile_db) == first
    assert not profile_db.in_transaction()


def test_corrupt_profile_is_rejected_without_open_transaction(profile_db):
    profile_db.add(AppMetadata(key=embedding_profile.PROFILE_KEY, value="not-json"))
    profile_db.commit()

    with pytest.raises(embedding_profile.EmbeddingProfileError, match="已损坏"):
        embedding_profile.ensure_embedding_profile(profile_db, _profile("current"))

    assert not profile_db.in_transaction()


def test_invalid_current_profile_configuration_is_rejected():
    class InvalidSettings:
        embedding_base_url = "  "
        embedding_model = ""
        embedding_dimension = 0

    with pytest.raises(embedding_profile.EmbeddingProfileError, match="配置无效"):
        embedding_profile.EmbeddingProfile.configured(InvalidSettings())


def test_ask_refuses_mismatched_profile_before_embedding_call(
    profile_db,
    monkeypatch,
):
    kb, _doc, _chunk = _add_ready_document(profile_db)
    stored = _profile("stored")
    embedding_profile.ensure_embedding_profile(profile_db, stored)
    monkeypatch.setattr(
        embedding_profile.EmbeddingProfile,
        "configured",
        classmethod(lambda cls, config=None: _profile("current")),
    )
    provider = Mock(side_effect=AssertionError("模型指纹不匹配时不应请求 embedding"))
    monkeypatch.setattr(embedding, "get_embedding_provider", provider)

    result = ask.answer_question(profile_db, "会不会混用旧向量？", kb.id)

    assert result.refused is True
    assert result.refusal_reason == ask.REFUSAL_EMBEDDING_MISMATCH
    assert result.search_query == "会不会混用旧向量？"
    provider.assert_not_called()


def test_ingest_mismatch_keeps_old_chunks_and_marks_actionable_error(
    profile_db,
    monkeypatch,
):
    _kb, doc, old_chunk = _add_ready_document(profile_db)
    embedding_profile.ensure_embedding_profile(profile_db, _profile("stored"))
    monkeypatch.setattr(
        embedding_profile.EmbeddingProfile,
        "configured",
        classmethod(lambda cls, config=None: _profile("current")),
    )

    ingest.process_document(doc.id, profile_db)
    profile_db.refresh(doc)

    assert doc.status == ingest.STATUS_READY
    assert doc.last_error_code == ingest.ERROR_EMBED_PROFILE_MISMATCH
    assert "重建全部向量" in (doc.last_error_message or "")
    assert profile_db.get(Chunk, old_chunk.id) is not None
