"""隔离环境、候选导入、多库原子切换与失败清理回归。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import evaluation
from eval import baseline as eval_baseline
from eval import environment as eval_environment
from tests.evaluation_helpers import _set_eval_environment, _write_small_eval_set

def test_eval_environment_rejects_daily_database(tmp_path):
    with pytest.raises(
        eval_environment.UnsafeEvalEnvironmentError,
        match="knowbase_eval",
    ):
        eval_environment.validate_eval_environment(
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
    tmp_path,
    storage_dir,
):
    with pytest.raises(eval_environment.UnsafeEvalEnvironmentError, match="必须精确使用隔离目录"):
        eval_environment.validate_eval_environment(
            "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
            storage_dir,
            project_root=tmp_path,
            working_directory=tmp_path,
        )


def test_eval_environment_accepts_isolated_database_and_storage(tmp_path):
    eval_environment.validate_eval_environment(
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
        "data/eval-storage",
        project_root=tmp_path,
        working_directory=tmp_path,
    )


def test_build_eval_kbs_validates_environment_before_touching_database(
    tmp_path,
    monkeypatch,
):
    _, dataset = _write_small_eval_set(tmp_path)
    db = Mock()
    monkeypatch.setattr(eval_baseline, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        eval_baseline.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase",
    )
    monkeypatch.setattr(
        eval_baseline.settings,
        "storage_dir",
        tmp_path / "data" / "eval-storage",
    )

    with pytest.raises(eval_environment.UnsafeEvalEnvironmentError):
        eval_baseline.build_eval_kbs(db, dataset)

    assert db.mock_calls == []


def test_build_eval_kbs_rejects_session_connected_to_another_database(
    tmp_path,
    monkeypatch,
):
    _, dataset = _write_small_eval_set(tmp_path)
    db = Mock()
    db.scalar.return_value = "knowbase"
    get_kb = Mock()
    _set_eval_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(eval_baseline.crud, "get_kb_by_name", get_kb)

    with pytest.raises(
        eval_environment.UnsafeEvalEnvironmentError,
        match="实际指向 knowbase",
    ):
        eval_baseline.build_eval_kbs(db, dataset)

    db.scalar.assert_called_once()
    get_kb.assert_not_called()


def test_import_library_requires_every_document_to_be_ready(tmp_path, monkeypatch):
    _, dataset = _write_small_eval_set(tmp_path, ("alpha",))
    library = dataset.libraries["alpha"]
    db = Mock()
    added_documents: list[object] = []

    def add(document):
        added_documents.append(document)

    def flush():
        added_documents[-1].id = 77

    def mark_failed(document):
        document.status = eval_baseline.ingest.STATUS_FAILED
        document.chunk_count = 0
        document.last_error_code = "EMBED_FAILED"
        document.last_error_message = "模拟向量化失败"

    db.add.side_effect = add
    db.flush.side_effect = flush
    db.refresh.side_effect = mark_failed
    monkeypatch.setattr(eval_baseline.storage, "save", Mock(return_value="70/77.md"))
    monkeypatch.setattr(eval_baseline.ingest, "process_document", Mock())

    with pytest.raises(RuntimeError, match="模拟向量化失败"):
        eval_baseline._import_library(db, library, 70)

    assert len(added_documents) == 1
    assert len(added_documents[0].content_hash) == 64
    db.commit.assert_called_once()


def test_build_eval_kbs_swaps_all_libraries_in_one_final_transaction(
    tmp_path,
    monkeypatch,
):
    _, dataset = _write_small_eval_set(tmp_path)
    _set_eval_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(evaluation, "assert_baseline_scale", Mock())

    old = {
        "alpha": SimpleNamespace(id=41, name="评估·alpha"),
        "beta": SimpleNamespace(id=51, name="评估·beta"),
    }
    staging = {
        "alpha": SimpleNamespace(
            id=42,
            name=eval_baseline._staging_name(dataset.libraries["alpha"]),
            description="导入中",
        ),
        "beta": SimpleNamespace(
            id=52,
            name=eval_baseline._staging_name(dataset.libraries["beta"]),
            description="导入中",
        ),
    }
    by_name = {old[key].name: old[key] for key in old}
    stage_by_name = {staging[key].name: staging[key] for key in staging}
    objects_by_id = {
        item.id: item for item in [*old.values(), *staging.values()]
    }
    events: list[str] = []
    db = Mock()
    db.scalar.return_value = "knowbase_eval"
    db.get.side_effect = lambda _model, object_id: objects_by_id.get(object_id)
    db.delete.side_effect = lambda item: events.append(f"delete:{item.id}")
    db.flush.side_effect = lambda: events.append("swap-flush")
    db.commit.side_effect = lambda: events.append("swap-commit")

    def get_kb_by_name(_db, name):
        return by_name.get(name)

    def create_kb(_db, data):
        return stage_by_name[data.name]

    def import_library(_db, library, staging_id):
        events.append(f"ready:{library.key}:{staging_id}")

    monkeypatch.setattr(eval_baseline.crud, "get_kb_by_name", get_kb_by_name)
    monkeypatch.setattr(eval_baseline.crud, "create_kb", create_kb)
    monkeypatch.setattr(eval_baseline, "_import_library", import_library)
    monkeypatch.setattr(
        eval_baseline.storage,
        "delete_kb_dir",
        lambda kb_id: events.append(f"storage:{kb_id}"),
    )

    result = eval_baseline.build_eval_kbs(db, dataset)

    assert result == {"alpha": 42, "beta": 52}
    assert events == [
        "ready:alpha:42",
        "ready:beta:52",
        "delete:41",
        "delete:51",
        "swap-flush",
        "swap-commit",
        "storage:41",
        "storage:51",
    ]
    assert staging["alpha"].name == "评估·alpha"
    assert staging["beta"].name == "评估·beta"
    assert staging["alpha"].description == "固定评估基线 v2 · alpha"
    assert staging["beta"].description == "固定评估基线 v2 · beta"
    db.rollback.assert_not_called()


def test_second_library_failure_preserves_all_old_libraries_and_cleans_candidates(
    tmp_path,
    monkeypatch,
):
    _, dataset = _write_small_eval_set(tmp_path)
    _set_eval_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(evaluation, "assert_baseline_scale", Mock())

    old = {
        "alpha": SimpleNamespace(id=61, name="评估·alpha"),
        "beta": SimpleNamespace(id=71, name="评估·beta"),
    }
    staging = {
        "alpha": SimpleNamespace(
            id=62,
            name=eval_baseline._staging_name(dataset.libraries["alpha"]),
            description="导入中",
        ),
        "beta": SimpleNamespace(
            id=72,
            name=eval_baseline._staging_name(dataset.libraries["beta"]),
            description="导入中",
        ),
    }
    by_name = {item.name: item for item in old.values()}
    stage_by_name = {item.name: item for item in staging.values()}
    stage_by_id = {item.id: item for item in staging.values()}
    events: list[str] = []
    db = Mock()
    db.scalar.return_value = "knowbase_eval"
    db.get.side_effect = lambda _model, object_id: stage_by_id.get(object_id)

    monkeypatch.setattr(
        eval_baseline.crud,
        "get_kb_by_name",
        lambda _db, name: by_name.get(name),
    )
    monkeypatch.setattr(
        eval_baseline.crud,
        "create_kb",
        lambda _db, data: stage_by_name[data.name],
    )

    def import_library(_db, library, staging_id):
        events.append(f"import:{library.key}:{staging_id}")
        if library.key == "beta":
            raise RuntimeError("第二个库向量化失败")

    def delete_candidate(_db, kb_id):
        events.append(f"cleanup-db:{kb_id}")
        return True

    monkeypatch.setattr(eval_baseline, "_import_library", import_library)
    monkeypatch.setattr(eval_baseline.crud, "delete_kb", delete_candidate)
    monkeypatch.setattr(
        eval_baseline.storage,
        "delete_kb_dir",
        lambda kb_id: events.append(f"cleanup-storage:{kb_id}"),
    )

    with pytest.raises(RuntimeError, match="第二个库向量化失败"):
        eval_baseline.build_eval_kbs(db, dataset)

    assert events == [
        "import:alpha:62",
        "import:beta:72",
        "cleanup-db:62",
        "cleanup-storage:62",
        "cleanup-db:72",
        "cleanup-storage:72",
    ]
    assert old["alpha"].name == "评估·alpha"
    assert old["beta"].name == "评估·beta"
    db.rollback.assert_called_once()
    db.delete.assert_not_called()
    db.commit.assert_not_called()
