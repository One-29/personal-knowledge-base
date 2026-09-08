"""文档管理路由（M1）：列表 / 详情 / 原文 / 删除。

路径组织：
- 文档集合隶属于知识库 → /kbs/{kb_id}/documents
- 文档资源独立         → /documents/{doc_id}
两个 router 只声明业务前缀，版本前缀由 main.py 装配（单点修改）。

上传与重传端点（multipart + 异步状态机）在 M2 接入时补充。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import crud, storage
from ..db import get_db
from ..models import Document
from ..schemas import DocStatus, DocumentContentOut, DocumentOut

logger = logging.getLogger(__name__)

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
