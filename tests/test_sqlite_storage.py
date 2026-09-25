"""SQLite 垂直切片：schema、入库、检索、图谱、级联与重启持久化。"""

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import crud, embedding, graph, ingest, retrieval, storage
from app.database import SQLITE_SCHEMA_VERSION, create_database_engine, initialize_database
from app.database.schema import UnsupportedSchemaVersion
from app.db import Base
from app.ingest_tasks import stage_task
from app.models import Chunk, Document, IngestTask, KnowledgeBase


@dataclass(frozen=True)
class SQLiteHarness:
    engine: Engine
    url: str

    def session(self) -> Session:
        return sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )()


@pytest.fixture()
def sqlite_harness() -> Iterator[SQLiteHarness]:
    directory = Path("data") / f"test-sqlite-{uuid4().hex[:8]}"
    database = (directory / "knowbase.db").resolve()
    url = "sqlite+pysqlite:///" + database.as_posix()
    engine = _engine(url)
    initialize_database(engine)
    try:
        yield SQLiteHarness(engine=engine, url=url)
    finally:
        engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)


def _engine(url: str) -> Engine:
    return create_database_engine(
        url,
        pool_size=2,
        max_overflow=1,
        pool_recycle=1800,
        pool_timeout=2,
    )


def _unit_vector(index: int) -> list[float]:
    vector = [0.0, 0.0, 0.0]
    vector[index] = 1.0
    return vector


def _add_chunk(
    db: Session,
    kb: KnowledgeBase,
    title: str,
    content: str,
    vector: list[float],
) -> tuple[Document, Chunk]:
    doc = Document(
        kb_id=kb.id,
        title=title,
        file_path=f"unused/{title}",
        content_hash=f"hash-{title}",
        char_count=len(content),
        status="ready",
        chunk_count=1,
    )
    db.add(doc)
    db.flush()
    chunk = Chunk(
        doc_id=doc.id,
        kb_id=kb.id,
        chunk_index=0,
        content=content,
        char_start=0,
        char_end=len(content),
        embedding=vector,
    )
    db.add(chunk)
    db.commit()
    return doc, chunk


def test_sqlite_schema_enables_wal_foreign_keys_and_version(sqlite_harness):
    with sqlite_harness.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 2000
        assert connection.scalar(text(
            "SELECT value FROM app_metadata WHERE key = 'schema_version'"
        )) == str(SQLITE_SCHEMA_VERSION)
        assert connection.scalar(text("""
            SELECT count(*) FROM sqlite_master
            WHERE name IN ('chunks_fts', 'chunks_fts_ai', 'chunks_fts_ad', 'chunks_fts_au')
        """)) == 4
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN "
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
            ('"极限连"',),
        ).all()
        assert any("VIRTUAL TABLE INDEX" in row.detail for row in plan)


def test_fastapi_lifespan_initializes_new_sqlite_database(monkeypatch):
    from app import main

    directory = Path("data") / f"test-sqlite-lifespan-{uuid4().hex[:8]}"
    database = (directory / "new.db").resolve()
    engine = _engine("sqlite+pysqlite:///" + database.as_posix())
    monkeypatch.setattr(main, "engine", engine)
    try:
        with TestClient(main.app) as client:
            response = client.get("/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "database": "ok"}
        with engine.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'knowledge_bases'
            """)) == 1
    finally:
        engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)


def test_fastapi_lifespan_recovers_persisted_ingest_task(
    sqlite_harness,
    monkeypatch,
):
    from app import main

    text_value = "# 直接启动\nASGI 生命周期会恢复任务。"
    with sqlite_harness.session() as db:
        kb = KnowledgeBase(name="直接启动恢复")
        db.add(kb)
        db.flush()
        document = Document(
            kb_id=kb.id,
            title="lifespan.md",
            file_path="placeholder",
            content_hash="lifespan-hash",
            char_count=len(text_value),
            status="pending",
        )
        db.add(document)
        db.flush()
        path = storage.save(kb.id, document.id, text_value.encode("utf-8"))
        document.file_path = path
        document.pending_file_path = path
        document.pending_content_hash = document.content_hash
        document.pending_char_count = document.char_count
        stage_task(db, document)
        db.commit()
        document_id = document.id

    monkeypatch.setattr(main, "engine", sqlite_harness.engine)
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 200

    with sqlite_harness.session() as db:
        document = db.get(Document, document_id)
        task = db.get(IngestTask, document_id)
        assert document is not None and document.status == "ready"
        assert task is not None and task.status == "succeeded"
        assert task.recovery_count == 1


def test_sqlite_ingest_and_hybrid_retrieval_use_real_storage(
    sqlite_harness,
):
    source = "# 导数与微分\n\n函数在一点可导时，导数描述局部变化率。\n"
    with sqlite_harness.session() as db:
        kb = KnowledgeBase(name="SQLite 高数库")
        db.add(kb)
        db.flush()
        doc = Document(
            kb_id=kb.id,
            title="导数.md",
            file_path="pending",
            content_hash="sqlite-ingest",
            char_count=len(source),
        )
        db.add(doc)
        db.flush()
        doc.file_path = storage.save(kb.id, doc.id, source.encode("utf-8"))
        db.commit()

        ingest.process_document(doc.id, db)
        db.refresh(doc)
        assert doc.status == "ready"
        assert doc.chunk_count >= 1

        query_vector = embedding.get_embedding_provider().embed_texts([source])[0]
        results = retrieval.retrieve(db, "导数描述局部变化率", query_vector, kb.id)
        assert results
        assert "局部变化率" in results[0].content
        assert results[0].vector_rank == 1
        assert results[0].keyword_rank == 1


def test_sqlite_fts_triggers_follow_update_and_cascade_delete(sqlite_harness):
    with sqlite_harness.session() as db:
        kb = KnowledgeBase(name="FTS 触发器")
        db.add(kb)
        db.flush()
        _doc, chunk = _add_chunk(db, kb, "fruit.md", "苹果香蕉梨子知识", _unit_vector(0))

        assert retrieval.search_keyword(db, "苹果香蕉", kb.id) == [chunk.id]
        chunk.content = "量子场论规范对称"
        db.commit()
        assert retrieval.search_keyword(db, "苹果香蕉", kb.id) == []
        assert retrieval.search_keyword(db, "场论规范", kb.id) == [chunk.id]

        assert crud.delete_kb(db, kb.id) is True
        assert db.scalar(select(func.count()).select_from(Chunk)) == 0
        assert retrieval.search_keyword(db, "场论规范", None) == []


def test_sqlite_short_keyword_escapes_like_wildcards(sqlite_harness):
    with sqlite_harness.session() as db:
        kb = KnowledgeBase(name="短词检索")
        db.add(kb)
        db.flush()
        _doc, literal = _add_chunk(db, kb, "literal.md", "符号 %_ 需要按字面匹配", _unit_vector(0))
        _doc, wildcard = _add_chunk(db, kb, "wildcard.md", "没有目标符号", _unit_vector(1))
        _doc, quoted = _add_chunk(db, kb, "quoted.md", '字面 abc"def 查询', _unit_vector(2))

        assert retrieval.search_keyword(db, "%_", kb.id) == [literal.id]
        assert wildcard.id not in retrieval.search_keyword(db, "%_", kb.id)
        assert retrieval.search_keyword(db, 'abc"def', kb.id) == [quoted.id]


def test_sqlite_vector_search_and_graph_are_deterministic(sqlite_harness):
    with sqlite_harness.session() as db:
        kb = KnowledgeBase(name="SQLite 图谱")
        db.add(kb)
        db.flush()
        first_doc, first = _add_chunk(db, kb, "极限.md", "数列极限与收敛", [1.0, 0.0, 0.0])
        second_doc, second = _add_chunk(db, kb, "连续.md", "函数连续与极限", [0.9, 0.1, 0.0])
        _third_doc, third = _add_chunk(db, kb, "积分.md", "定积分与面积", [0.0, 0.0, 1.0])

        hits = retrieval.search_vector(db, [1.0, 0.0, 0.0], kb.id)
        assert [chunk_id for chunk_id, _ in hits[:3]] == [first.id, second.id, third.id]
        assert hits[0][1] == pytest.approx(1.0)

        result = graph.build_graph(db, kb.id, top_k=1, min_similarity=0.9)
        assert {node.doc_id for node in result.nodes} == {
            first_doc.id,
            second_doc.id,
            _third_doc.id,
        }
        assert any(
            {edge.source, edge.target} == {first_doc.id, second_doc.id}
            for edge in result.edges
        )


def test_sqlite_data_survives_engine_restart(sqlite_harness):
    with sqlite_harness.session() as db:
        db.add(KnowledgeBase(name="重启后仍存在"))
        db.commit()

    sqlite_harness.engine.dispose()
    reopened = _engine(sqlite_harness.url)
    try:
        initialize_database(reopened)
        with Session(reopened) as db:
            assert db.scalar(select(KnowledgeBase.name)) == "重启后仍存在"
    finally:
        reopened.dispose()


def test_sqlite_v1_upgrades_task_schema_without_losing_data(sqlite_harness):
    with sqlite_harness.session() as db:
        db.add(KnowledgeBase(name="升级保留数据"))
        db.commit()

    with sqlite_harness.engine.begin() as connection:
        connection.execute(text("DROP TABLE ingest_tasks"))
        connection.execute(text("""
            UPDATE app_metadata
            SET value = '1'
            WHERE key = 'schema_version'
        """))

    initialize_database(sqlite_harness.engine)
    initialize_database(sqlite_harness.engine)

    with sqlite_harness.session() as db:
        assert db.scalar(select(KnowledgeBase.name)) == "升级保留数据"
        assert db.scalar(select(func.count()).select_from(IngestTask)) == 0
    with sqlite_harness.engine.connect() as connection:
        assert connection.scalar(text("""
            SELECT value FROM app_metadata WHERE key = 'schema_version'
        """)) == str(SQLITE_SCHEMA_VERSION)
        assert connection.scalar(text("""
            SELECT count(*) FROM sqlite_master
            WHERE type = 'index' AND name = 'ix_ingest_tasks_status_updated'
        """)) == 1


def test_sqlite_initialization_rebuilds_fts_for_existing_chunks():
    directory = Path("data") / f"test-sqlite-rebuild-{uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    database = (directory / "legacy.db").resolve()
    engine = _engine("sqlite+pysqlite:///" + database.as_posix())
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            kb = KnowledgeBase(name="迁移前数据")
            db.add(kb)
            db.flush()
            _add_chunk(db, kb, "legacy.md", "泰勒公式余项估计", _unit_vector(0))

        initialize_database(engine)
        with Session(engine) as db:
            assert retrieval.search_keyword(db, "公式余项", None)
    finally:
        engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.parametrize(
    ("version", "message"),
    [
        (str(SQLITE_SCHEMA_VERSION + 1), "更高版本"),
        ("0", "需要升级"),
        ("not-a-number", "版本无效"),
    ],
)
def test_sqlite_rejects_unsupported_version_before_business_ddl(version, message):
    directory = Path("data") / f"test-sqlite-version-{uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    database = (directory / "future.db").resolve()
    engine = _engine("sqlite+pysqlite:///" + database.as_posix())
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """))
            connection.execute(
                text("""
                    INSERT INTO app_metadata(key, value)
                    VALUES ('schema_version', :version)
                """),
                {"version": version},
            )

        with pytest.raises(UnsupportedSchemaVersion, match=message):
            initialize_database(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("""
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'knowledge_bases'
            """)) == 0
    finally:
        engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)
