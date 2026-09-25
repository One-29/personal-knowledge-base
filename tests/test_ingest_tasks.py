"""持久入库任务：原子领取、终态、重启续跑与旧数据兼容。"""

from sqlalchemy import select

from app import ingest, storage
from app.ingest_tasks import (
    IngestTaskSpec,
    recover_incomplete_tasks,
    run_ingest_task,
)
from app.ingest_tasks import repository
from app.models import Document, IngestTask


def _upload(client, name: str = "任务测试库") -> tuple[int, dict]:
    kb_id = client.post("/api/v1/kbs", json={"name": name}).json()["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("task.md", "# 入库任务\n持久状态可以恢复。", "text/markdown")},
    )
    assert response.status_code == 201
    return kb_id, response.json()["document"]


def _spec(task: IngestTask) -> IngestTaskSpec:
    return IngestTaskSpec(task.doc_id, task.ingest_version, task.candidate_path)


def test_upload_persists_queued_task_and_exposes_safe_progress(client, db):
    _kb_id, payload = _upload(client)

    assert payload["task"] == {
        "ingest_version": 1,
        "status": "queued",
        "stage": "queued",
        "attempt_count": 0,
        "recovery_count": 0,
        "last_error_code": None,
        "started_at": None,
        "finished_at": None,
        "updated_at": payload["task"]["updated_at"],
    }
    assert "candidate" not in payload["task"]
    task = db.get(IngestTask, payload["id"])
    assert task is not None
    assert task.candidate_path


def test_task_runs_once_and_records_success(client, db, monkeypatch):
    _kb_id, payload = _upload(client)
    task = db.get(IngestTask, payload["id"])
    assert task is not None
    spec = _spec(task)
    stages: list[str] = []
    real_update_stage = repository.update_stage

    def record_stage(session, task_spec, stage):
        stages.append(stage)
        return real_update_stage(session, task_spec, stage)

    monkeypatch.setattr(repository, "update_stage", record_stage)

    assert run_ingest_task(spec, db) == "succeeded"
    assert stages == [
        "validating",
        "reading",
        "chunking",
        "embedding",
        "publishing",
    ]
    db.expire_all()
    document = db.get(Document, payload["id"])
    finished = db.get(IngestTask, payload["id"])
    assert document is not None and document.status == "ready"
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.stage == "complete"
    assert finished.attempt_count == 1
    assert finished.started_at is not None
    assert finished.finished_at is not None

    assert run_ingest_task(spec, db) == "skipped"
    db.expire_all()
    assert db.get(IngestTask, payload["id"]).attempt_count == 1


def test_restart_recovers_running_task_and_counts_recovery(client, db):
    _kb_id, payload = _upload(client)
    document = db.get(Document, payload["id"])
    task = db.get(IngestTask, payload["id"])
    assert document is not None and task is not None
    document.status = "processing"
    task.status = "running"
    task.stage = "embedding"
    task.attempt_count = 1
    db.commit()

    recovered = recover_incomplete_tasks(db)

    assert recovered.inspected == 1
    assert recovered.recovered == 1
    assert recovered.succeeded == 1
    assert recovered.failed == 0
    db.expire_all()
    document = db.get(Document, payload["id"])
    task = db.get(IngestTask, payload["id"])
    assert document is not None and document.status == "ready"
    assert task is not None and task.status == "succeeded"
    assert task.attempt_count == 2
    assert task.recovery_count == 1


def test_restart_reconciles_publish_completed_before_task_finalize(
    client,
    db,
    monkeypatch,
):
    _kb_id, payload = _upload(client)
    document = db.get(Document, payload["id"])
    task = db.get(IngestTask, payload["id"])
    assert document is not None and task is not None

    ingest.process_document(
        document.id,
        db,
        expected_version=document.ingest_version,
        candidate_path=document.pending_file_path,
    )
    db.expire_all()
    task = db.get(IngestTask, payload["id"])
    assert task is not None and task.status == "queued"

    def must_not_repeat(*_args, **_kwargs):
        raise AssertionError("已发布文档不应再次向量化")

    monkeypatch.setattr("app.ingest_tasks.service.ingest.process_document", must_not_repeat)
    recovered = recover_incomplete_tasks(db)

    assert recovered.recovered == 0
    assert recovered.reconciled == 1
    db.expire_all()
    task = db.get(IngestTask, payload["id"])
    assert task is not None and task.status == "succeeded"
    assert task.attempt_count == 0


def test_restart_backfills_legacy_pending_document_without_candidate(client, db):
    kb_id = client.post("/api/v1/kbs", json={"name": "旧版恢复库"}).json()["id"]
    text = "# 旧版任务\n只有活动原文路径。"
    document = Document(
        kb_id=kb_id,
        title="legacy.md",
        file_path="placeholder",
        content_hash="legacy-hash",
        char_count=len(text),
        status="pending",
    )
    db.add(document)
    db.flush()
    document.file_path = storage.save(
        kb_id,
        document.id,
        text.encode("utf-8"),
    )
    db.commit()
    assert db.get(IngestTask, document.id) is None

    recovered = recover_incomplete_tasks(db)

    assert recovered.recovered == 1
    assert recovered.succeeded == 1
    db.expire_all()
    document = db.get(Document, document.id)
    task = db.get(IngestTask, document.id)
    assert document is not None and document.status == "ready"
    assert task is not None
    assert task.candidate_path is None
    assert task.status == "succeeded"
    assert task.recovery_count == 1


def test_restart_continues_after_one_document_fails(client, db):
    kb_id = client.post("/api/v1/kbs", json={"name": "恢复隔离库"}).json()["id"]
    first = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("missing.md", "# 会失败\n候选文件将丢失。", "text/markdown")},
    ).json()["document"]
    second = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("healthy.md", "# 会成功\n恢复不能被前一篇中断。", "text/markdown")},
    ).json()["document"]
    failed_task = db.get(IngestTask, first["id"])
    assert failed_task is not None and failed_task.candidate_path
    storage.delete(failed_task.candidate_path)

    recovered = recover_incomplete_tasks(db)

    assert recovered.recovered == 2
    assert recovered.succeeded == 1
    assert recovered.failed == 1
    db.expire_all()
    assert db.get(IngestTask, first["id"]).status == "failed"
    assert db.get(IngestTask, second["id"]).status == "succeeded"
    assert db.get(Document, first["id"]).last_error_code == "PARSE_FAILED"
    assert db.get(Document, second["id"]).status == "ready"


def test_new_version_replaces_task_identity_and_old_spec_cannot_claim(client, db):
    _kb_id, payload = _upload(client)
    document = db.get(Document, payload["id"])
    old_task = db.get(IngestTask, payload["id"])
    assert document is not None and old_task is not None
    old_spec = _spec(old_task)

    response = client.post(
        f"/api/v1/documents/{document.id}/reupload",
        files={"file": ("task.md", "# 新版本\n旧任务不能覆盖。", "text/markdown")},
    )
    assert response.status_code == 200
    db.expire_all()
    current = db.get(IngestTask, document.id)
    assert current is not None
    assert current.ingest_version == old_spec.ingest_version + 1
    assert current.status == "queued"

    assert run_ingest_task(old_spec, db) == "skipped"
    db.expire_all()
    current = db.get(IngestTask, document.id)
    assert current is not None and current.status == "queued"


def test_document_list_loads_tasks_without_duplicate_rows(client, db):
    kb_id, first = _upload(client, "批量任务库")
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("second.md", "# 第二篇\n等待处理。", "text/markdown")},
    )
    assert response.status_code == 201

    documents = client.get(f"/api/v1/kbs/{kb_id}/documents").json()

    assert {item["id"] for item in documents} == {first["id"], response.json()["document"]["id"]}
    assert all(item["task"]["status"] == "queued" for item in documents)
    assert len(db.scalars(select(IngestTask)).all()) == 2
