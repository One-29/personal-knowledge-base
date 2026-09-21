"""入库管线编排（M2）：原文 → 切分 → 向量化 → 事务内替换 chunks → 状态流转。

职责边界（02 §3）：
- 属于：切分、向量化、chunks 写入与清理、文档状态机维护
- 不属于：上传接口（M1）、检索问答（M3）
触发方：M1 的上传/重传端点（D3 登记即返回、D4 进程内后台任务）。

失败语义（DM5，03 §4）：**导入绝不破坏可用性**——
- 有旧块（重传失败）→ 回滚 status=ready，旧内容继续可检索，记录 last_error；
- 无旧块（首次导入失败）→ status=failed。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import chunking, crud, embedding, embedding_profile, package_storage, storage
from .core.config import settings
from .db import SessionLocal
from .embedding import EmbeddingError
from .models import Chunk, Document

logger = logging.getLogger(__name__)

# 文档状态值（与 03 §3 CHECK 约束同源；M2 内部使用，API 层见 schemas.DocStatus）
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# 错误码（US-M1-06 分类；上传期错误由 M1 现场拦截，此处为处理期错误）
ERROR_PARSE_FAILED = "PARSE_FAILED"
ERROR_EMBED_FAILED = "EMBED_FAILED"
ERROR_EMBED_PROFILE_MISMATCH = "EMBED_PROFILE_MISMATCH"
ERROR_PROCESS_FAILED = "PROCESS_FAILED"


def process_document(
    doc_id: int,
    db: Session | None = None,
    *,
    expected_version: int | None = None,
    candidate_path: str | None = None,
) -> None:
    """处理一篇文档：切分 → 向量化 → 替换 chunks → 更新状态。

    :param db: 可选外部会话（测试注入用）；缺省时自建独立会话
        （后台任务路径：请求级会话在响应后已关闭，不能复用）。
    :param expected_version: 上传登记时固定的版本；版本变化表示任务已经过期。
    :param candidate_path: 上传登记时固定的不可变候选文件路径。

    捕获处理异常并尽力更新文档状态与 last_error，避免任务停留在 processing。
    若数据库持续不可用导致状态恢复也失败，记录完整异常以供排查。
    """
    own_session = db is None
    if db is None:
        db = SessionLocal()
    version = expected_version
    source_path = candidate_path
    task_is_bound = expected_version is not None or candidate_path is not None
    try:
        doc = crud.get_document(db, doc_id)
        if doc is None:
            logger.warning("处理任务找不到文档: doc_id=%s", doc_id)
            return

        version = doc.ingest_version if version is None else version
        source_path = source_path or doc.pending_file_path or doc.file_path
        if doc.ingest_version != version or (
            task_is_bound and doc.pending_file_path != source_path
        ):
            logger.info(
                "跳过过期文档任务: doc_id=%s task_version=%s current_version=%s",
                doc_id,
                version,
                doc.ingest_version,
            )
            _delete_if_unreferenced(db, source_path)
            return

        source_hash = (
            doc.pending_content_hash
            if doc.pending_file_path == source_path
            else doc.content_hash
        ) or doc.content_hash
        source_char_count = (
            doc.pending_char_count
            if doc.pending_file_path == source_path
            and doc.pending_char_count is not None
            else doc.char_count
        )

        try:
            embedding_profile.ensure_embedding_profile(db)
        except embedding_profile.EmbeddingProfileError as exc:
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_EMBED_PROFILE_MISMATCH,
                str(exc),
                require_pending_candidate=task_is_bound,
            )
            return

        doc.status = STATUS_PROCESSING
        db.commit()

        try:
            text = storage.read(source_path)
            package_manifest = package_storage.load_package_manifest(source_path)
            package_storage.verify_package_source(package_manifest, text)
        except FileNotFoundError:
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_PARSE_FAILED,
                "原文文件缺失",
                require_pending_candidate=task_is_bound,
            )
            return
        except package_storage.StorageIntegrityError as exc:
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_PARSE_FAILED,
                str(exc),
                require_pending_candidate=task_is_bound,
            )
            return

        chunks = chunking.split_markdown(
            text,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
            protected_spans=tuple(
                (int(item["char_start"]), int(item["char_end"]))
                for item in (package_manifest or {}).get("occurrences", [])
            ),
        )
        if not chunks:
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_PARSE_FAILED,
                "切分后无有效内容",
                require_pending_candidate=task_is_bound,
            )
            return

        try:
            vectors = embedding.get_embedding_provider().embed_texts([c.text for c in chunks])
            _validate_vectors(vectors, expected_count=len(chunks))
        except EmbeddingError as exc:
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_EMBED_FAILED,
                str(exc),
                require_pending_candidate=task_is_bound,
            )
            return

        doc = crud.get_document(db, doc_id, for_update=True)
        if doc is None:
            logger.warning("处理完成前文档已删除: doc_id=%s", doc_id)
            db.rollback()
            _safe_delete(source_path)
            return
        if doc.ingest_version != version or (
            task_is_bound and doc.pending_file_path != source_path
        ):
            logger.info(
                "放弃过期文档任务结果: doc_id=%s task_version=%s current_version=%s",
                doc_id,
                version,
                doc.ingest_version,
            )
            db.rollback()
            _delete_if_unreferenced(db, source_path)
            return

        old_source_path = doc.file_path
        # 事务内全量替换：删旧块 + 插新块（重传重建，US-M1-04）
        db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
        db.add_all(
            Chunk(
                doc_id=doc.id,
                kb_id=doc.kb_id,
                chunk_index=chunk.index,
                content=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors)
        )
        if doc.pending_file_path == source_path:
            doc.file_path = source_path
            doc.content_hash = source_hash
            doc.char_count = source_char_count
            doc.pending_file_path = None
            doc.pending_content_hash = None
            doc.pending_char_count = None
        doc.chunk_count = len(chunks)
        doc.status = STATUS_READY
        doc.last_error_code = None
        doc.last_error_message = None
        doc.processed_at = datetime.now(timezone.utc)
        db.commit()
        # 带图片的历史版本保留到文档删除：回答中保存的引用快照仍可打开原图。
        # 普通文本沿用原清理策略，避免无图片版本不断占用磁盘。
        if (
            old_source_path
            and old_source_path != source_path
            and not package_storage.is_package_source(old_source_path)
        ):
            _safe_delete(old_source_path)
        logger.info("文档处理完成: doc_id=%s chunks=%s", doc.id, len(chunks))
    except Exception as exc:
        logger.exception("处理文档异常: doc_id=%s", doc_id)
        try:
            # _mark_failure 先回滚被中止的事务并恢复已删除的旧块，再重新查询文档。
            _mark_failure(
                db,
                doc_id,
                version,
                source_path,
                ERROR_PROCESS_FAILED,
                f"{type(exc).__name__}: {exc}",
                require_pending_candidate=task_is_bound,
            )
        except Exception:
            logger.exception("处理失败后恢复文档状态也失败: doc_id=%s", doc_id)
            try:
                db.rollback()
            except Exception:
                logger.exception("状态恢复失败后的事务回滚也失败: doc_id=%s", doc_id)
    finally:
        if own_session:
            db.close()


def _mark_failure(
    db: Session,
    doc_id: int,
    expected_version: int | None,
    candidate_path: str | None,
    code: str,
    message: str,
    *,
    require_pending_candidate: bool,
) -> None:
    """只给当前版本记录失败；过期任务不能覆盖新任务的状态。"""
    db.rollback()
    doc = crud.get_document(db, doc_id, for_update=True)
    if doc is None:
        logger.warning("处理失败后无法恢复状态，文档已不存在: doc_id=%s", doc_id)
        db.rollback()
        if candidate_path:
            _safe_delete(candidate_path)
        return
    if expected_version is not None and doc.ingest_version != expected_version:
        logger.info(
            "忽略过期文档任务错误: doc_id=%s task_version=%s current_version=%s",
            doc_id,
            expected_version,
            doc.ingest_version,
        )
        db.rollback()
        if candidate_path:
            _delete_if_unreferenced(db, candidate_path)
        return
    if (
        require_pending_candidate
        and candidate_path is not None
        and doc.pending_file_path != candidate_path
    ):
        logger.info("忽略已被替换的候选文档错误: doc_id=%s", doc_id)
        db.rollback()
        _delete_if_unreferenced(db, candidate_path)
        return

    has_old_chunks = (
        db.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc.id)) or 0
    ) > 0
    failed_candidate = None
    if candidate_path and doc.pending_file_path == candidate_path:
        if candidate_path != doc.file_path:
            failed_candidate = candidate_path
        doc.pending_file_path = None
        doc.pending_content_hash = None
        doc.pending_char_count = None
    doc.last_error_code = code
    doc.last_error_message = message
    doc.processed_at = datetime.now(timezone.utc)
    doc.status = STATUS_READY if has_old_chunks else STATUS_FAILED
    db.commit()
    if failed_candidate:
        _safe_delete(failed_candidate)
    logger.warning("文档处理失败: doc_id=%s code=%s kept_old=%s", doc.id, code, has_old_chunks)


def _delete_if_unreferenced(db: Session, rel_path: str) -> None:
    """删除不再被活动原文或候选原文引用的版本文件。"""
    referenced = db.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            (Document.file_path == rel_path)
            | (Document.pending_file_path == rel_path)
        )
    )
    db.rollback()
    if not referenced:
        _safe_delete(rel_path)


def _safe_delete(rel_path: str) -> None:
    """提交后的旧文件清理失败只记录日志，不反转已经成功的数据库切换。"""
    try:
        storage.delete(rel_path)
    except OSError:
        logger.warning("清理旧原文失败: path=%s", rel_path, exc_info=True)


def _validate_vectors(vectors: list[list[float]], expected_count: int) -> None:
    """拒绝不完整或维度错误的 provider 响应，避免 zip 静默少写知识块。"""
    if len(vectors) != expected_count:
        raise EmbeddingError(
            f"embedding 返回数量不匹配：期望 {expected_count}，实际 {len(vectors)}"
        )
    invalid = [
        index
        for index, vector in enumerate(vectors)
        if len(vector) != settings.embedding_dimension
    ]
    if invalid:
        raise EmbeddingError(
            f"embedding 维度不匹配：期望 {settings.embedding_dimension}，"
            f"异常位置 {invalid[:5]}"
        )
