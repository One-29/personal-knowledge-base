"""SQLite 来源 ID 生命周期与旧库升级，全部使用隔离内存数据库。"""

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.database import create_database_engine, initialize_database
from app.db import Base
from app.models import Chunk, Document, IngestTask, KnowledgeBase


@pytest.fixture(params=[False, True], ids=["new", "schema-v2"])
def identity_engine(request):
    engine = create_database_engine("sqlite+pysqlite://", pool_size=1, max_overflow=0,
                                    pool_recycle=1800, pool_timeout=2)
    if request.param:
        create_v2(engine)
    else:
        initialize_database(engine)
    yield engine
    engine.dispose()


def create_v2(engine):
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=engine.dialect))
            connection.exec_driver_sql(ddl.replace("AUTOINCREMENT", ""))
        connection.execute(text("INSERT INTO app_metadata VALUES ('schema_version', '2')"))
        from app.database.schema import _create_fts_schema
        _create_fts_schema(connection)


def seed(engine):
    with Session(engine, expire_on_commit=False) as db:
        kb = KnowledgeBase(name="Identity audit")
        db.add(kb)
        db.flush()
        doc = Document(kb_id=kb.id, title="cache.md", file_path="unused/cache.md",
                       content_hash="a" * 64, char_count=15, chunk_count=1, status="ready")
        db.add(doc)
        db.flush()
        chunk = Chunk(kb_id=kb.id, doc_id=doc.id, chunk_index=0, content="cache reference",
                      char_start=0, char_end=15, embedding=[1., 0., 0.])
        db.add(chunk)
        db.add(IngestTask(doc_id=doc.id, ingest_version=1, status="succeeded", stage="complete"))
        db.commit()
        return kb.id, doc.id, chunk.id


def test_replaced_chunk_never_resolves_to_new_content(identity_engine):
    _, doc_id, old_id = seed(identity_engine)
    initialize_database(identity_engine)
    with Session(identity_engine) as db:
        db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        new = Chunk(kb_id=1, doc_id=doc_id, chunk_index=0, content="new reference",
                    char_start=0, char_end=13, embedding=[0., 1., 0.])
        db.add(new)
        db.commit()
        assert new.id > old_id
        assert db.get(Chunk, old_id) is None


def test_deleted_document_and_library_ids_are_not_reused(identity_engine):
    kb_id, doc_id, _ = seed(identity_engine)
    initialize_database(identity_engine)
    with Session(identity_engine) as db:
        db.execute(delete(Document).where(Document.id == doc_id))
        doc = Document(kb_id=kb_id, title="new.md", file_path="unused/new.md", content_hash="b" * 64)
        db.add(doc)
        db.flush()
        assert doc.id > doc_id
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        kb = KnowledgeBase(name="New library")
        db.add(kb)
        db.commit()
        assert kb.id > kb_id
        assert db.scalar(select(Chunk.id)) is None
        assert db.scalar(select(IngestTask.doc_id)) is None


def test_v2_upgrade_preserves_rows_fts_custom_index_and_is_repeatable():
    engine = create_database_engine("sqlite+pysqlite://", pool_size=1, max_overflow=0,
                                    pool_recycle=1800, pool_timeout=2)
    try:
        create_v2(engine)
        ids = seed(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE INDEX audit_title_idx ON documents(title)")
            before = {name: connection.exec_driver_sql(f'SELECT * FROM "{name}"').all()
                      for name in ("knowledge_bases", "documents", "chunks", "ingest_tasks")}
        initialize_database(engine)
        initialize_database(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM app_metadata WHERE key='schema_version'")) == "3"
            for name, rows in before.items():
                assert connection.exec_driver_sql(f'SELECT * FROM "{name}"').all() == rows
            for name in ("knowledge_bases", "documents", "chunks"):
                ddl = connection.scalar(text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": name})
                assert "AUTOINCREMENT" in ddl
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.scalar(text("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'cache'")) == ids[2]
            assert connection.scalar(text("SELECT name FROM sqlite_master WHERE name='audit_title_idx'"))
    finally:
        engine.dispose()


def test_failed_identity_upgrade_rolls_back_data_schema_and_fts():
    engine = create_database_engine("sqlite+pysqlite://", pool_size=1, max_overflow=0,
                                    pool_recycle=1800, pool_timeout=2)
    try:
        create_v2(engine)
        _, _, chunk_id = seed(engine)
        def fail_rebuild(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("ALTER TABLE") and "chunks" in statement:
                raise RuntimeError("injected migration interruption")
        event.listen(engine, "before_cursor_execute", fail_rebuild)
        try:
            with pytest.raises(RuntimeError, match="injected migration"):
                initialize_database(engine)
        finally:
            event.remove(engine, "before_cursor_execute", fail_rebuild)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM app_metadata WHERE key='schema_version'")) == "2"
            assert connection.scalar(text("SELECT count(*) FROM chunks")) == 1
            assert connection.scalar(text("SELECT count(*) FROM ingest_tasks")) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.scalar(text("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'cache'")) == chunk_id
            assert connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name LIKE '%__identity_v3'")) == 0
        initialize_database(engine)
    finally:
        engine.dispose()
