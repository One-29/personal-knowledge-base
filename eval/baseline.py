"""多知识库候选基线的导入、校验、原子切换与失败清理。"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app import crud, evaluation, ingest, storage
from app.core.config import settings
from app.models import Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate

from .environment import validate_eval_database_session, validate_eval_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAGING_SUFFIX = "（导入中）"
logger = logging.getLogger(__name__)


def _staging_name(library: evaluation.EvalLibrary) -> str:
    name = f"{library.name}{STAGING_SUFFIX}"
    if len(name) > 100:
        raise evaluation.EvalDatasetError(
            f"知识库 {library.key} 的导入中名称超过 100 个字符。"
        )
    return name


def _delete_eval_kb(db, kb: KnowledgeBase | None) -> None:
    if kb is None:
        return
    kb_id = kb.id
    if not crud.delete_kb(db, kb_id):
        raise RuntimeError(f"删除评估知识库失败：kb_id={kb_id}")
    storage.delete_kb_dir(kb_id)


def _best_effort_cleanup(db, kb_id: int) -> None:
    """失败回滚后尽量同时清除候选元数据与文件，不覆盖原始异常。"""
    try:
        candidate = db.get(KnowledgeBase, kb_id)
        if candidate is not None:
            if not crud.delete_kb(db, kb_id):
                logger.warning("候选评估知识库已不存在：kb_id=%s", kb_id)
    except Exception:
        db.rollback()
        logger.exception("清理候选评估知识库失败：kb_id=%s", kb_id)
    try:
        storage.delete_kb_dir(kb_id)
    except Exception:
        logger.exception("清理候选评估原文失败：kb_id=%s", kb_id)


def _create_staging_kb(db, library: evaluation.EvalLibrary) -> KnowledgeBase:
    stale = crud.get_kb_by_name(db, _staging_name(library))
    if stale is not None:
        _delete_eval_kb(db, stale)
    return crud.create_kb(
        db,
        KnowledgeBaseCreate(
            name=_staging_name(library),
            description=f"{library.name}评估语料正在导入；全部知识库就绪后统一切换",
        ),
    )


def _import_library(
    db,
    library: evaluation.EvalLibrary,
    staging_id: int,
) -> None:
    for path in library.note_paths():
        document_bytes = path.read_bytes()
        document_text = document_bytes.decode("utf-8")
        document = Document(
            kb_id=staging_id,
            title=path.name,
            file_path="",
            content_hash=hashlib.sha256(document_bytes).hexdigest(),
            char_count=len(document_text),
        )
        db.add(document)
        db.flush()
        document.file_path = storage.save(staging_id, document.id, document_bytes)
        db.commit()
        ingest.process_document(document.id, db)
        db.refresh(document)
        if document.status != ingest.STATUS_READY or document.chunk_count < 1:
            detail = document.last_error_message or document.last_error_code or "未知错误"
            raise RuntimeError(f"评估文档 {library.key}/{path.name} 处理失败：{detail}")


def build_eval_kbs(
    db,
    dataset: evaluation.EvalDataset,
) -> dict[str, int]:
    """先完整构建所有候选库，再用一个事务替换上一版正式基线。"""
    validate_eval_environment(
        settings.database_url,
        settings.storage_dir,
        project_root=PROJECT_ROOT,
    )
    validate_eval_database_session(
        db,
        settings.database_url,
        project_root=PROJECT_ROOT,
    )
    evaluation.validate_eval_sources(dataset)
    evaluation.assert_baseline_scale(dataset)

    old_ids: dict[str, int | None] = {}
    staging_ids: dict[str, int] = {}
    try:
        for key, library in dataset.libraries.items():
            old = crud.get_kb_by_name(db, library.name)
            old_ids[key] = old.id if old is not None else None
            staging = _create_staging_kb(db, library)
            staging_ids[key] = staging.id
            _import_library(db, library, staging.id)

        # 删除旧库与候选改名在同一事务；任何唯一约束或提交故障都会恢复旧版。
        for old_id in old_ids.values():
            if old_id is None:
                continue
            old = db.get(KnowledgeBase, old_id)
            if old is not None:
                db.delete(old)
        db.flush()
        for key, library in dataset.libraries.items():
            staging = db.get(KnowledgeBase, staging_ids[key])
            if staging is None:
                raise RuntimeError(f"候选评估知识库在切换前消失：{key}")
            staging.name = library.name
            staging.description = f"固定评估基线 v{dataset.version} · {key}"
        db.commit()
    except Exception:
        db.rollback()
        for staging_id in staging_ids.values():
            _best_effort_cleanup(db, staging_id)
        raise

    # 正式库已经提交；旧目录清理失败只留下孤儿文件，不能反向删除新基线。
    for old_id in old_ids.values():
        if old_id is None:
            continue
        try:
            storage.delete_kb_dir(old_id)
        except Exception:
            logger.exception("旧评估原文目录清理失败：kb_id=%s", old_id)
    return staging_ids
