"""可恢复入库任务的执行与启动续跑编排。"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import ingest
from app.core.config import Settings, settings
from app.db import SessionLocal
from app.models import Document, IngestTask
from app.vault.coordinator import VaultTransactionError

from . import repository
from .domain import (
    ACTIVE_STATUSES,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    IngestRecovery,
    IngestTaskSpec,
)

logger = logging.getLogger(__name__)
TASK_CRASHED = "TASK_CRASHED"


def run_ingest_task(
    spec: IngestTaskSpec,
    db: Session | None = None,
    *,
    config: Settings = settings,
    storage_root: Path | None = None,
) -> str:
    """领取并执行一项任务，返回最终状态或 ``skipped``。

    领取、阶段更新和终结均带任务版本条件。即使旧 BackgroundTask 晚于重传
    返回，它也无法改写新版本的进度或错误状态。
    """
    own_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        if not repository.claim_task(db, spec):
            logger.info(
                "跳过未领取的入库任务: doc_id=%s version=%s",
                spec.doc_id,
                spec.ingest_version,
            )
            return "skipped"

        def report_stage(stage: str) -> None:
            try:
                repository.update_stage(db, spec, stage)
            except Exception:
                db.rollback()
                logger.warning(
                    "记录入库阶段失败，继续处理文档: doc_id=%s stage=%s",
                    spec.doc_id,
                    stage,
                    exc_info=True,
                )

        try:
            ingest.process_document(
                spec.doc_id,
                db,
                expected_version=spec.ingest_version,
                candidate_path=spec.candidate_path,
                progress=report_stage,
                config=config,
                storage_root=storage_root,
            )
        except VaultTransactionError:
            # Vault 与数据库的最终状态可能需要启动自检判断；保留 running 让
            # 下一次启动重新审计，不能用普通任务提交掩盖一致性故障。
            db.rollback()
            raise

        status, error_code = _document_result(db, spec)
        repository.finish_task(
            db,
            spec,
            status=status,
            error_code=error_code,
        )
        return status
    except (VaultTransactionError, SQLAlchemyError):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "入库任务执行器异常: doc_id=%s version=%s",
            spec.doc_id,
            spec.ingest_version,
        )
        try:
            repository.finish_task(
                db,
                spec,
                status=STATUS_FAILED,
                error_code=TASK_CRASHED,
            )
        except Exception:
            db.rollback()
            logger.exception("入库任务异常状态也无法写入: doc_id=%s", spec.doc_id)
        return STATUS_FAILED
    finally:
        if own_session:
            db.close()


def recover_incomplete_tasks(
    db: Session | None = None,
    *,
    config: Settings = settings,
    storage_root: Path | None = None,
) -> IngestRecovery:
    """重启时补齐终态并顺序续跑 pending/processing 文档。"""
    own_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        active_tasks = list(
            db.scalars(
                select(IngestTask)
                .where(IngestTask.status.in_(ACTIVE_STATUSES))
                .order_by(IngestTask.updated_at, IngestTask.doc_id)
            )
        )
        documents = list(
            db.scalars(
                select(Document)
                .where(Document.status.in_(("pending", "processing")))
                .order_by(Document.created_at, Document.id)
            )
        )
        reconciled = 0

        # 崩溃可能发生在文档发布提交之后、任务终结提交之前。此时文档已经
        # 是事实，直接补齐成功/失败状态，避免重复向量化。
        for task in active_tasks:
            document = db.get(Document, task.doc_id)
            if document is None:
                continue
            spec = IngestTaskSpec(
                task.doc_id,
                task.ingest_version,
                task.candidate_path,
            )
            if document.ingest_version != task.ingest_version:
                repository.finish_task(db, spec, status=STATUS_SUPERSEDED)
                reconciled += 1
                continue
            if (
                document.status in {"ready", "failed"}
                and document.pending_file_path is None
            ):
                status = (
                    STATUS_SUCCEEDED
                    if document.status == "ready" and not document.last_error_code
                    else STATUS_FAILED
                )
                repository.finish_task(
                    db,
                    spec,
                    status=status,
                    error_code=document.last_error_code,
                )
                reconciled += 1

        specs: list[IngestTaskSpec] = []
        for document in documents:
            _task, spec = repository.stage_task(db, document, recovery=True)
            specs.append(spec)
        db.commit()

        succeeded = 0
        failed = 0
        for spec in specs:
            result = run_ingest_task(
                spec,
                db,
                config=config,
                storage_root=storage_root,
            )
            if result == STATUS_SUCCEEDED:
                succeeded += 1
            elif result == STATUS_FAILED:
                failed += 1

        summary = IngestRecovery(
            inspected=len(
                {task.doc_id for task in active_tasks}
                | {document.id for document in documents}
            ),
            recovered=len(specs),
            succeeded=succeeded,
            failed=failed,
            reconciled=reconciled,
        )
        if summary.recovered or summary.reconciled:
            logger.info(
                "入库任务启动恢复完成: recovered=%s succeeded=%s failed=%s reconciled=%s",
                summary.recovered,
                summary.succeeded,
                summary.failed,
                summary.reconciled,
            )
        return summary
    finally:
        if own_session:
            db.close()


def _document_result(
    db: Session,
    spec: IngestTaskSpec,
) -> tuple[str, str | None]:
    db.expire_all()
    document = db.get(Document, spec.doc_id)
    if document is None or document.ingest_version != spec.ingest_version:
        return STATUS_SUPERSEDED, None
    if document.pending_file_path is not None:
        return STATUS_FAILED, document.last_error_code or TASK_CRASHED
    if document.status == "ready" and document.last_error_code is None:
        return STATUS_SUCCEEDED, None
    return STATUS_FAILED, document.last_error_code or TASK_CRASHED
