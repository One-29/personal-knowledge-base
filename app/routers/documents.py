"""文档管理路由（M1）：上传 / 重传 / 列表 / 详情 / 原文 / 删除。

路径组织：
- 文档集合隶属于知识库 → /kbs/{kb_id}/documents
- 文档资源独立         → /documents/{doc_id}
两个 router 只声明业务前缀，版本前缀由 main.py 装配（单点修改）。

异步处理说明（D3/D4）：上传/重传只登记元数据并落原文，文档状态置 pending；
切分与向量化由 M2 的入库管线接管（接入后经 BackgroundTasks 驱动）。
"""

import hashlib
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session

from .. import crud, storage
from ..core.config import settings
from ..db import get_db
from ..models import Document
from ..schemas import DocStatus, DocumentContentOut, DocumentOut, UploadResult

logger = logging.getLogger(__name__)

# 一期仅支持 Markdown / 纯文本（决策 D1）
ALLOWED_EXTENSIONS = {".md", ".txt"}

kb_documents_router = APIRouter(prefix="/kbs", tags=["Documents"])
documents_router = APIRouter(prefix="/documents", tags=["Documents"])


def _doc_out(doc: Document) -> DocumentOut:
    """ORM → 响应模型：会话存活时显式组装（防懒加载时序问题）。"""
    return DocumentOut(
        id=doc.id,
        kb_id=doc.kb_id,
        title=doc.title,
        status=doc.status,
        char_count=doc.char_count,
        chunk_count=doc.chunk_count,
        last_error_code=doc.last_error_code,
        last_error_message=doc.last_error_message,
        processed_at=doc.processed_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def _validate_upload(file: UploadFile) -> tuple[str, bytes, str]:
    """上传校验：格式 / 大小 / 空内容 / 编码，在写库前拦截（US-M1-06）。

    返回 (title, content_bytes, text)；不合法直接抛 HTTPException。
    """
    title = Path(file.filename or "").name          # 只取文件名，防路径注入
    if Path(title).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=422, detail="仅支持 .md / .txt 文件")
    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过大小上限")
    if not content.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="文件编码需为 UTF-8") from None
    return title, content, text


@kb_documents_router.post("/{kb_id}/documents", response_model=UploadResult, status_code=201)
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传文档：校验 → 登记（flush 取 id）→ 写原文 → 提交（US-M1-02、D6）。

    顺序说明：先 flush 让数据库分配 doc_id（事务未提交），再按
    {kb_id}/{doc_id}.md 写文件；写文件失败则 rollback，无需清理已提交数据。
    """
    if crud.get_kb(db, kb_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    title, content, text = await _validate_upload(file)
    if crud.get_document_by_title(db, kb_id, title) is not None:
        raise HTTPException(status_code=409, detail="同库同名文档已存在，请使用重传接口")

    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path="",                                   # 占位，写文件后回填相对路径
        content_hash=hashlib.sha256(content).hexdigest(),
        char_count=len(text),
    )
    db.add(doc)
    db.flush()                                          # 分配 doc_id（事务未提交）
    try:
        doc.file_path = storage.save(kb_id, doc.id, content)
    except OSError:
        db.rollback()
        logger.exception("原文写入失败: kb_id=%s title=%s", kb_id, title)
        raise HTTPException(status_code=500, detail="原文写入失败") from None
    db.commit()
    db.refresh(doc)
    return UploadResult(document=_doc_out(doc), content_changed=True)


@documents_router.post("/{doc_id}/reupload", response_model=UploadResult)
async def reupload_document(
    doc_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """重传覆盖：同文档换内容；sha256 未变则幂等跳过（US-M1-04）。

    内容变更后状态回到 pending，等待 M2 重建切块与向量；
    重建失败时按 DM5 保留旧内容并记录 last_error（由 M2 写入）。
    """
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    _, content, text = await _validate_upload(file)
    new_hash = hashlib.sha256(content).hexdigest()
    if new_hash == doc.content_hash:
        return UploadResult(document=_doc_out(doc), content_changed=False)
    try:
        storage.save(doc.kb_id, doc.id, content)        # 覆盖写原文
    except OSError:
        logger.exception("原文覆盖写入失败: doc_id=%s", doc_id)
        raise HTTPException(status_code=500, detail="原文写入失败") from None
    doc.content_hash = new_hash
    doc.char_count = len(text)
    doc.status = DocStatus.PENDING.value                # 内容变更 → 待重建（M2 驱动）
    doc.last_error_code = None
    doc.last_error_message = None
    db.commit()
    db.refresh(doc)
    return UploadResult(document=_doc_out(doc), content_changed=True)


@kb_documents_router.get("/{kb_id}/documents", response_model=list[DocumentOut])
def list_documents(
    kb_id: int,
    status: DocStatus | None = Query(default=None, description="按处理状态过滤"),
    title: str | None = Query(default=None, min_length=1, description="标题模糊匹配"),
    db: Session = Depends(get_db),
):
    """某知识库下的文档列表（可按状态/标题过滤）。"""
    if crud.get_kb(db, kb_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    docs = crud.list_documents(
        db,
        kb_id,
        status=status.value if status is not None else None,
        title=title,
    )
    return [_doc_out(doc) for doc in docs]


@documents_router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, db: Session = Depends(get_db)):
    """单篇文档详情（含最新处理状态与错误信息）。"""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return _doc_out(doc)


@documents_router.get("/{doc_id}/content", response_model=DocumentContentOut)
def get_document_content(doc_id: int, db: Session = Depends(get_db)):
    """原文内容（前端查看与溯源高亮用）。"""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        content = storage.read(doc.file_path)
    except FileNotFoundError:
        logger.warning("原文文件缺失: doc_id=%s path=%s", doc.id, doc.file_path)
        raise HTTPException(status_code=404, detail="原文文件缺失") from None
    return DocumentContentOut(title=doc.title, content=content)


@documents_router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """删除文档：先删数据库行（真相源），再删原文文件（失败仅记日志）。"""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    rel_path = doc.file_path
    crud.delete_document(db, doc_id)
    if not storage.delete(rel_path):
        logger.warning("原文文件删除失败或已不存在: %s", rel_path)
    return Response(status_code=204)
