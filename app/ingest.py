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

from . import chunking, crud, embedding, storage
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
ERROR_PROCESS_FAILED = "PROCESS_FAILED"


def process_document(doc_id: int, db: Session | None = None) -> None:
    """处理一篇文档：切分 → 向量化 → 替换 chunks → 更新状态。

    :param db: 可选外部会话（测试注入用）；缺省时自建独立会话
        （后台任务路径：请求级会话在响应后已关闭，不能复用）。

    捕获处理异常并尽力更新文档状态与 last_error，避免任务停留在 processing。
    若数据库持续不可用导致状态恢复也失败，记录完整异常以供排查。
    """
    own_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        doc = crud.get_document(db, doc_id)
        if doc is None:
            logger.warning("处理任务找不到文档: doc_id=%s", doc_id)
            return

        doc.status = STATUS_PROCESSING
        db.commit()

        try:
            text = storage.read(doc.file_path)
        except FileNotFoundError:
            _mark_failure(db, doc, ERROR_PARSE_FAILED, "原文文件缺失")
            return

        chunks = chunking.split_markdown(
            text,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )
        if not chunks:
            _mark_failure(db, doc, ERROR_PARSE_FAILED, "切分后无有效内容")
            return

        try:
            vectors = embedding.get_embedding_provider().embed_texts([c.text for c in chunks])
            _validate_vectors(vectors, expected_count=len(chunks))
        except EmbeddingError as exc:
            _mark_failure(db, doc, ERROR_EMBED_FAILED, str(exc))
            return

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
        doc.chunk_count = len(chunks)
        doc.status = STATUS_READY
        doc.last_error_code = None
        doc.last_error_message = None
        doc.processed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("文档处理完成: doc_id=%s chunks=%s", doc.id, len(chunks))
    except Exception as exc:
        logger.exception("处理文档异常: doc_id=%s", doc_id)
        try:
            # 写块失败会让事务不可用；先回滚（同时恢复被删除的旧块），
            # 再重新查询文档，避免沿用已过期或处于失败事务的 ORM 状态。
            db.rollback()
            failed_doc = crud.get_document(db, doc_id)
            if failed_doc is None:
                logger.warning("处理失败后无法恢复状态，文档已不存在: doc_id=%s", doc_id)
            else:
                _mark_failure(
                    db, failed_doc, ERROR_PROCESS_FAILED, f"{type(exc).__name__}: {exc}"
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


def _mark_failure(db: Session, doc: Document, code: str, message: str) -> None:
    """失败落库（DM5）：有旧块则保旧回滚 ready，无旧块才标 failed。"""
    has_old_chunks = (
        db.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc.id)) or 0
    ) > 0
    doc.last_error_code = code
    doc.last_error_message = message
    doc.processed_at = datetime.now(timezone.utc)
    doc.status = STATUS_READY if has_old_chunks else STATUS_FAILED
    db.commit()
    logger.warning("文档处理失败: doc_id=%s code=%s kept_old=%s", doc.id, code, has_old_chunks)


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
