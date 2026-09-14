"""评估指标的纯单元回归：未命中计零分，且来源文档必须匹配。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import embedding, evaluation
from app.retrieval import RetrievedChunk
from eval import run_eval


def _candidate(chunk_id: int, doc_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        content=content,
        char_start=0,
        char_end=len(content),
        rrf_score=0.1,
        vector_similarity=0.9,
        vector_rank=chunk_id,
        keyword_rank=None,
    )


def test_is_hit_requires_expected_document_and_keyword():
    assert evaluation.is_hit("tcp.md", "TCP.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "os.md", ["三次握手"], "解释三次握手")
    assert not evaluation.is_hit("tcp.md", "tcp.md", ["四次挥手"], "解释三次握手")


def test_mrr_includes_zero_for_misses_and_rejects_wrong_source(monkeypatch):
    items = [
        evaluation.EvalItem("问题一", "tcp.md", ["目标词"], True),
        evaluation.EvalItem("问题二", "tcp.md", ["不存在"], True),
    ]
    responses = [
        [
            _candidate(1, 2, "目标词但来源错误"),
            _candidate(2, 1, "正确来源的目标词"),
        ],
        [],
    ]

    class Provider:
        def embed_texts(self, texts):
            return [[1.0]]

    monkeypatch.setattr(embedding, "get_embedding_provider", lambda: Provider())
    monkeypatch.setattr(evaluation.retrieval, "retrieve", lambda *args, **kwargs: responses.pop(0))
    db = Mock()

    metrics = evaluation.evaluate_retrieval(
        db, items, kb_id=1, doc_titles={1: "tcp.md", 2: "os.md"}
    )

    assert metrics.total == 2
    assert metrics.hits == 1
    assert metrics.reciprocal_ranks == [0.5]
    assert metrics.recall_at_k == pytest.approx(0.5)
    assert metrics.mrr == pytest.approx(0.25)
    assert db.rollback.call_count == 2


def test_eval_environment_rejects_daily_database(tmp_path):
    with pytest.raises(run_eval.UnsafeEvalEnvironmentError, match="knowbase_eval"):
        run_eval.validate_eval_environment(
            "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase",
            "data/eval-storage",
            project_root=tmp_path,
            working_directory=tmp_path,
        )


@pytest.mark.parametrize(
    "storage_dir",
    [
        Path("data/storage"),
        Path("data/storage/eval"),
        Path("data"),
        Path("data/another-eval-directory"),
    ],
)
def test_eval_environment_rejects_every_storage_except_dedicated_directory(
    tmp_path, storage_dir
):
    with pytest.raises(run_eval.UnsafeEvalEnvironmentError, match="必须精确使用隔离目录"):
        run_eval.validate_eval_environment(
            "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
            storage_dir,
            project_root=tmp_path,
            working_directory=tmp_path,
        )


def test_eval_environment_accepts_isolated_database_and_storage(tmp_path):
    run_eval.validate_eval_environment(
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
        "data/eval-storage",
        project_root=tmp_path,
        working_directory=tmp_path,
    )


def test_build_eval_kb_validates_before_touching_database(tmp_path, monkeypatch):
    db = Mock()
    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_eval.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase",
    )
    monkeypatch.setattr(
        run_eval.settings, "storage_dir", tmp_path / "data" / "eval-storage"
    )

    with pytest.raises(run_eval.UnsafeEvalEnvironmentError):
        run_eval.build_eval_kb(
            db,
            notes_dir=tmp_path,
        )

    assert db.mock_calls == []


def test_build_eval_kb_rejects_session_connected_to_another_database(
    tmp_path, monkeypatch
):
    db = Mock()
    db.scalar.return_value = "knowbase"
    get_kb = Mock()
    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_eval.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
    )
    monkeypatch.setattr(
        run_eval.settings, "storage_dir", tmp_path / "data" / "eval-storage"
    )
    monkeypatch.setattr(run_eval.crud, "get_kb_by_name", get_kb)

    with pytest.raises(run_eval.UnsafeEvalEnvironmentError, match="实际指向 knowbase"):
        run_eval.build_eval_kb(db, notes_dir=tmp_path)

    db.scalar.assert_called_once()
    get_kb.assert_not_called()


def test_build_eval_kb_swaps_only_after_ready_and_then_removes_old_storage(
    tmp_path, monkeypatch
):
    old_kb = SimpleNamespace(id=41)
    staging_kb = SimpleNamespace(
        id=42,
        name=run_eval.STAGING_KB_NAME,
        description="导入中",
    )
    db = Mock()
    db.scalar.return_value = "knowbase_eval"
    events: list[str] = []
    eval_storage = tmp_path / "data" / "eval-storage"
    old_storage = eval_storage / str(old_kb.id)
    old_storage.mkdir(parents=True)
    (old_storage / "1.md").write_text("旧评估原文", encoding="utf-8")
    note = tmp_path / "note.md"
    note.write_text("# 可处理的评估文档\n\n固定语料。", encoding="utf-8")

    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_eval.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
    )
    monkeypatch.setattr(run_eval.settings, "storage_dir", eval_storage)
    monkeypatch.setattr(
        run_eval.crud,
        "get_kb_by_name",
        lambda _db, name: old_kb if name == run_eval.KB_NAME else None,
    )
    monkeypatch.setattr(run_eval.crud, "create_kb", lambda *_: staging_kb)
    monkeypatch.setattr(db, "commit", Mock(side_effect=lambda: events.append("commit")))
    monkeypatch.setattr(db, "get", Mock(return_value=staging_kb))
    monkeypatch.setattr(
        run_eval.storage,
        "save",
        lambda *_args: "42/100.md",
    )

    def mark_ready(doc):
        doc.status = run_eval.ingest.STATUS_READY
        doc.chunk_count = 1

    monkeypatch.setattr(db, "refresh", Mock(side_effect=mark_ready))
    monkeypatch.setattr(run_eval.ingest, "process_document", Mock())
    original_delete_kb_dir = run_eval.storage.delete_kb_dir

    def delete_kb_dir(kb_id):
        events.append(f"storage:{kb_id}")
        original_delete_kb_dir(kb_id)

    monkeypatch.setattr(run_eval.storage, "delete_kb_dir", delete_kb_dir)

    kb_id = run_eval.build_eval_kb(
        db,
        notes_dir=tmp_path,
    )

    assert kb_id == staging_kb.id
    assert events == ["commit", "commit", "storage:41"]
    assert staging_kb.name == run_eval.KB_NAME
    assert staging_kb.description == "检索质量评估专用"
    db.delete.assert_called_once_with(old_kb)
    assert not old_storage.exists()


def test_build_eval_kb_failure_preserves_old_library_and_cleans_staging(
    tmp_path, monkeypatch
):
    old_kb = SimpleNamespace(id=51)
    staging_kb = SimpleNamespace(
        id=52,
        name=run_eval.STAGING_KB_NAME,
        description="导入中",
    )
    db = Mock()
    db.scalar.return_value = "knowbase_eval"
    eval_storage = tmp_path / "data" / "eval-storage"
    old_storage = eval_storage / str(old_kb.id)
    staging_storage = eval_storage / str(staging_kb.id)
    old_storage.mkdir(parents=True)
    staging_storage.mkdir(parents=True)
    (old_storage / "old.md").write_text("仍可使用", encoding="utf-8")
    (staging_storage / "new.md").write_text("处理失败", encoding="utf-8")
    (tmp_path / "note.md").write_text("# 会失败的评估文档", encoding="utf-8")

    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        run_eval.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
    )
    monkeypatch.setattr(run_eval.settings, "storage_dir", eval_storage)
    monkeypatch.setattr(
        run_eval.crud,
        "get_kb_by_name",
        lambda _db, name: old_kb if name == run_eval.KB_NAME else None,
    )
    monkeypatch.setattr(run_eval.crud, "create_kb", lambda *_: staging_kb)
    delete_kb = Mock(return_value=True)
    monkeypatch.setattr(run_eval.crud, "delete_kb", delete_kb)
    monkeypatch.setattr(db, "get", Mock(return_value=staging_kb))
    monkeypatch.setattr(run_eval.storage, "save", lambda *_args: "52/100.md")
    monkeypatch.setattr(run_eval.ingest, "process_document", Mock())

    def mark_failed(doc):
        doc.status = run_eval.ingest.STATUS_FAILED
        doc.chunk_count = 0
        doc.last_error_code = "EMBED_FAILED"
        doc.last_error_message = "模拟向量化失败"

    monkeypatch.setattr(db, "refresh", Mock(side_effect=mark_failed))

    with pytest.raises(RuntimeError, match="模拟向量化失败"):
        run_eval.build_eval_kb(db, notes_dir=tmp_path)

    assert old_storage.is_dir()
    assert not staging_storage.exists()
    delete_kb.assert_called_once_with(db, staging_kb.id)
    db.delete.assert_not_called()
