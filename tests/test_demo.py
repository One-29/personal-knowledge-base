"""高等数学演示语料与安全加载器测试。"""

from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import crud, graph
from app.core.config import settings
from app.database import create_database_engine, initialize_database
from app.models import Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate
from demo import load_calculus


def test_calculus_corpus_has_enough_substantial_documents():
    paths = load_calculus.note_paths()

    assert len(paths) >= 7
    assert len({path.name for path in paths}) == len(paths)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# ")
        assert text.count("\n## ") >= 3
        assert len(text) >= 600


def test_graph_presets_expose_the_calibrated_demo_thresholds():
    thresholds = load_calculus.GRAPH_THRESHOLDS
    html = (
        Path(__file__).resolve().parents[1] / "frontend" / "index.html"
    ).read_text(encoding="utf-8")

    assert thresholds == tuple(sorted(set(thresholds)))
    assert graph.DEFAULT_MIN_SIMILARITY == thresholds[1]
    for threshold in thresholds:
        assert f'value="{threshold:.2f}"' in html


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://postgres@localhost/knowbase_test",
        "postgresql+psycopg://postgres@localhost/knowbase_eval",
        "postgresql+psycopg://postgres@localhost/course_test",
        "postgresql+psycopg://postgres@localhost/course_eval",
    ],
)
def test_demo_loader_rejects_test_and_eval_databases(url):
    with pytest.raises(RuntimeError, match="测试或评估环境"):
        load_calculus.validate_demo_target(url)


def test_demo_loader_accepts_daily_database():
    load_calculus.validate_demo_target(
        "postgresql+psycopg://postgres@localhost/knowbase"
    )


def test_demo_loader_accepts_daily_sqlite_file(tmp_path):
    load_calculus.validate_demo_target(
        "sqlite+pysqlite:///" + (tmp_path / "daily-knowbase.db").as_posix()
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:///data/eval/knowbase.db",
        "sqlite+pysqlite:///data/knowbase-test.db",
    ],
)
def test_demo_loader_rejects_memory_test_and_eval_sqlite(url):
    with pytest.raises(RuntimeError, match="拒绝加载演示知识库"):
        load_calculus.validate_demo_target(url)


def test_demo_loader_rejects_session_connected_to_different_database():
    db = Mock()
    db.scalar.return_value = "knowbase_test"

    with pytest.raises(RuntimeError, match="当前连接实际指向 knowbase_test"):
        load_calculus.validate_demo_database_session(
            db,
            "postgresql+psycopg://postgres@localhost/knowbase",
        )


def test_demo_loader_accepts_session_matching_configured_database():
    db = Mock()
    db.scalar.return_value = "knowbase"

    load_calculus.validate_demo_database_session(
        db,
        "postgresql+psycopg://postgres@localhost/knowbase",
    )


def test_demo_loader_checks_real_sqlite_file(tmp_path):
    database = tmp_path / "daily-knowbase.db"
    url = "sqlite+pysqlite:///" + database.as_posix()
    engine = create_database_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_recycle=1800,
        pool_timeout=1,
    )
    try:
        initialize_database(engine)
        with Session(engine) as db:
            load_calculus.validate_demo_database_session(db, url)
            with pytest.raises(RuntimeError, match="当前连接实际指向"):
                load_calculus.validate_demo_database_session(
                    db,
                    "sqlite+pysqlite:///" + (tmp_path / "other.db").as_posix(),
                )
    finally:
        engine.dispose()


def test_demo_loader_is_idempotent_and_all_documents_are_ready(db):
    first_id, first_documents = load_calculus.replace_demo_kb(db)
    second_id, second_documents = load_calculus.replace_demo_kb(db)

    assert second_id != first_id
    assert len(first_documents) == len(load_calculus.note_paths())
    assert len(second_documents) == len(load_calculus.note_paths())
    assert db.scalar(
        select(func.count())
        .select_from(KnowledgeBase)
        .where(KnowledgeBase.name == load_calculus.DEMO_KB_NAME)
    ) == 1
    assert db.scalar(
        select(func.count()).select_from(KnowledgeBase).where(
            KnowledgeBase.name == load_calculus.STAGING_KB_NAME
        )
    ) == 0
    documents = db.scalars(
        select(Document).where(Document.kb_id == second_id).order_by(Document.title)
    ).all()
    assert len(documents) == len(load_calculus.note_paths())
    assert all(doc.status == "ready" and doc.chunk_count >= 1 for doc in documents)
    assert all((settings.storage_dir / doc.file_path).is_file() for doc in documents)
    assert not (settings.storage_dir / str(first_id)).exists()


def test_demo_loader_failure_keeps_old_library_and_removes_staging(db, monkeypatch):
    old = crud.create_kb(
        db,
        KnowledgeBaseCreate(name=load_calculus.DEMO_KB_NAME, description="旧演示库"),
    )

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("模拟向量化失败")

    monkeypatch.setattr(load_calculus, "_add_and_process_document", fail_import)

    with pytest.raises(RuntimeError, match="模拟向量化失败"):
        load_calculus.replace_demo_kb(db)

    assert crud.get_kb_by_name(db, load_calculus.DEMO_KB_NAME).id == old.id
    assert crud.get_kb_by_name(db, load_calculus.STAGING_KB_NAME) is None
