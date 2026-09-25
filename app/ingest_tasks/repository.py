"""入库任务的事务边界与带版本保护的状态更新。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Document, IngestTask

from .domain import (
    ACTIVE_STATUSES,
    RUNNING_STAGES,
    STAGE_COMPLETE,
    STAGE_QUEUED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    IngestTaskSpec,
)


def task_spec(document: Document) -> IngestTaskSpec:
    """从当前文档生成任务身份；空候选表示兼容旧版活动原文。"""
    return IngestTaskSpec(
        doc_id=document.id,
        ingest_version=document.ingest_version,
        candidate_path=document.pending_file_path,
    )


def stage_task(
    db: Session,
    document: Document,
    *,
    recovery: bool = False,
) -> tuple[IngestTask, IngestTaskSpec]:
    """在调用方事务中登记当前任务，不自行提交。

    上传路由先调用本函数，再与文档/Vault 一起提交，从而避免出现“文档已
    pending、任务却不存在”的崩溃窗口。相同的运行中任务保持不变；启动恢复
    会把它重新排队并累计恢复次数。
    """
    spec = task_spec(document)
    task = db.get(IngestTask, document.id)
    if task is None:
        task = IngestTask(
            doc_id=spec.doc_id,
            ingest_version=spec.ingest_version,
            candidate_path=spec.candidate_path,
            status=STATUS_QUEUED,
            stage=STAGE_QUEUED,
            recovery_count=1 if recovery else 0,
        )
        db.add(task)
        return task, spec

    same_identity = (
        task.ingest_version == spec.ingest_version
        and task.candidate_path == spec.candidate_path
    )
    if not same_identity:
        task.ingest_version = spec.ingest_version
        task.candidate_path = spec.candidate_path
        task.status = STATUS_QUEUED
        task.stage = STAGE_QUEUED
        task.attempt_count = 0
        task.recovery_count = 1 if recovery else 0
        task.last_error_code = None
        task.started_at = None
        task.finished_at = None
        return task, spec

    should_requeue = recovery or task.status not in ACTIVE_STATUSES
    if should_requeue:
        task.status = STATUS_QUEUED
        task.stage = STAGE_QUEUED
        task.last_error_code = None
        task.started_at = None
        task.finished_at = None
    if recovery:
        task.recovery_count += 1
    return task, spec


def claim_task(db: Session, spec: IngestTaskSpec) -> bool:
    """原子领取一次排队任务；重复调度只有一个执行者能成功。"""
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(IngestTask)
        .where(
            IngestTask.doc_id == spec.doc_id,
            IngestTask.ingest_version == spec.ingest_version,
            IngestTask.candidate_path == spec.candidate_path,
            IngestTask.status == STATUS_QUEUED,
        )
        .values(
            status=STATUS_RUNNING,
            stage="validating",
            attempt_count=IngestTask.attempt_count + 1,
            last_error_code=None,
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def update_stage(db: Session, spec: IngestTaskSpec, stage: str) -> bool:
    """只更新仍由该版本持有的运行中任务。"""
    if stage not in RUNNING_STAGES:
        raise ValueError(f"非法入库阶段：{stage}")
    result = db.execute(
        update(IngestTask)
        .where(
            IngestTask.doc_id == spec.doc_id,
            IngestTask.ingest_version == spec.ingest_version,
            IngestTask.candidate_path == spec.candidate_path,
            IngestTask.status == STATUS_RUNNING,
        )
        .values(stage=stage, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def finish_task(
    db: Session,
    spec: IngestTaskSpec,
    *,
    status: str,
    error_code: str | None = None,
) -> bool:
    """终结当前版本任务；新版本已经替换任务行时保持无操作。"""
    if status not in {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_SUPERSEDED}:
        raise ValueError(f"非法入库任务终态：{status}")
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(IngestTask)
        .where(
            IngestTask.doc_id == spec.doc_id,
            IngestTask.ingest_version == spec.ingest_version,
            IngestTask.candidate_path == spec.candidate_path,
            IngestTask.status.in_(ACTIVE_STATUSES),
        )
        .values(
            status=status,
            stage=STAGE_COMPLETE,
            last_error_code=error_code,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def supersede_task(db: Session, doc_id: int) -> None:
    """在调用方事务中结束被取消的旧版本，不自行提交。"""
    task = db.get(IngestTask, doc_id)
    if task is None or task.status not in ACTIVE_STATUSES:
        return
    now = datetime.now(timezone.utc)
    task.status = STATUS_SUPERSEDED
    task.stage = STAGE_COMPLETE
    task.last_error_code = None
    task.finished_at = now
    task.updated_at = now


def current_task(db: Session, document: Document) -> IngestTask | None:
    task = db.get(IngestTask, document.id)
    if task is None or task.ingest_version != document.ingest_version:
        return None
    return task


def tasks_by_document_ids(
    db: Session,
    document_ids: list[int],
) -> dict[int, IngestTask]:
    """批量读取任务，供文档列表避免逐行查询。"""
    if not document_ids:
        return {}
    tasks = db.scalars(
        select(IngestTask).where(IngestTask.doc_id.in_(document_ids))
    ).all()
    return {task.doc_id: task for task in tasks}
