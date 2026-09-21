"""文档管理路由（M1）：上传 / 重传 / 列表 / 详情 / 原文 / 删除。

路径组织：
- 文档集合隶属于知识库 → /kbs/{kb_id}/documents
- 文档资源独立         → /documents/{doc_id}
两个 router 只声明业务前缀，版本前缀由 main.py 装配（单点修改）。

异步处理说明（D3/D4）：上传/重传只登记元数据并落原文，文档状态置 pending；
切分与向量化由 M2 的入库管线接管（接入后经 BackgroundTasks 驱动）。
"""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session

from .. import crud, ingest, package_storage, storage
from ..document_io import PreparedDocument, UploadValidationError, read_upload
from ..db import get_db
from ..models import Document
from ..schemas import (
    DocStatus,
    DocumentOut,
    UploadResult,
)
from ..vault.coordinator import VaultTransactionError, commit_document
from ..vault.store import VaultError

logger = logging.getLogger(__name__)

kb_documents_router = APIRouter(prefix="/kbs", tags=["Documents"])
documents_router = APIRouter(prefix="/documents", tags=["Documents"])


def _doc_out(doc: Document) -> DocumentOut:
    """ORM → 响应模型：会话存活时显式组装（防懒加载时序问题）。"""
    try:
        display_path = doc.pending_file_path or doc.file_path
        manifest = package_storage.load_package_manifest(display_path) if display_path else None
        image_count = len(manifest["occurrences"]) if manifest else 0
    except (OSError, ValueError):
        image_count = 0
    return DocumentOut(
        id=doc.id,
        kb_id=doc.kb_id,
        title=doc.title,
        status=doc.status,
        char_count=(
            doc.pending_char_count
            if doc.pending_char_count is not None
            else doc.char_count
        ),
        chunk_count=doc.chunk_count,
        image_count=image_count,
        last_error_code=doc.last_error_code,
        last_error_message=doc.last_error_message,
        processed_at=doc.processed_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _schedule_ingest(background_tasks: BackgroundTasks, doc: Document) -> None:
    """把候选文件和版本固定到任务参数，旧任务据此识别自己是否已过期。"""
    candidate_path = doc.pending_file_path or doc.file_path
    background_tasks.add_task(
        _run_ingest_task,
        doc.id,
        expected_version=doc.ingest_version,
        candidate_path=candidate_path,
    )


def _run_ingest_task(
    doc_id: int,
    *,
    expected_version: int,
    candidate_path: str,
) -> None:
    """后台任务入口；独立会话由入库模块创建。"""
    ingest.process_document(
        doc_id,
        expected_version=expected_version,
        candidate_path=candidate_path,
    )


def _clear_pending(doc: Document) -> None:
    doc.pending_file_path = None
    doc.pending_content_hash = None
    doc.pending_char_count = None


def _safe_delete(rel_path: str) -> None:
    """候选登记完成后的文件清理失败只记录日志。"""
    try:
        storage.delete(rel_path)
    except OSError:
        logger.warning("清理候选原文失败: path=%s", rel_path, exc_info=True)


def _validate_upload(file: UploadFile) -> PreparedDocument:
    """上传校验：普通文本兼容旧流程；ZIP 执行路径、CRC、图片与位置校验。"""
    try:
        return read_upload(file.filename, file.file)
    except UploadValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


def _save_candidate(
    prepared: PreparedDocument,
    kb_id: int,
    doc_id: int,
    version: int,
) -> str:
    """按上传类型写入不可变候选版本。"""
    if prepared.is_package:
        return package_storage.save_package(kb_id, doc_id, version, prepared)
    return storage.save_version(
        kb_id,
        doc_id,
        version,
        prepared.content_hash,
        prepared.source_bytes,
    )


@kb_documents_router.post("/{kb_id}/documents", response_model=UploadResult, status_code=201)
def upload_document(
    kb_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传文档：校验 → 登记（flush 取 id）→ 写原文 → 提交 → 异步触发入库管线。

    顺序说明：先 flush 让数据库分配 doc_id（事务未提交），再按
    {kb_id}/{doc_id}/v{version}-{hash}.md 写入不可变候选文件；写文件失败则
    rollback，数据库提交失败则清理候选文件。
    登记提交后经 BackgroundTasks 触发 M2 处理（D3/D4），响应立即返回 pending 状态。
    """
    if crud.get_kb(db, kb_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    prepared = _validate_upload(file)
    title = prepared.title
    if crud.get_document_by_title(db, kb_id, title) is not None:
        raise HTTPException(status_code=409, detail="同库同名文档已存在，请使用重传接口")

    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path="",                                   # 占位，写文件后回填相对路径
        content_hash=prepared.content_hash,
        char_count=len(prepared.text),
        ingest_version=1,
    )
    db.add(doc)
    db.flush()                                          # 分配 doc_id（事务未提交）
    candidate_path: str | None = None
    try:
        candidate_path = _save_candidate(prepared, kb_id, doc.id, doc.ingest_version)
        doc.file_path = candidate_path
        doc.pending_file_path = candidate_path
        doc.pending_content_hash = prepared.content_hash
        doc.pending_char_count = len(prepared.text)
        commit_document(db, doc)
    except VaultTransactionError:
        db.rollback()
        logger.exception(
            "上传事务失败且 Vault 未能恢复，保留候选原文: kb_id=%s title=%s",
            kb_id,
            title,
        )
        raise HTTPException(
            status_code=500,
            detail="数据一致性恢复失败，请停止写入并重新启动检查",
        ) from None
    except (OSError, VaultError):
        db.rollback()
        if candidate_path:
            _safe_delete(candidate_path)
        logger.exception("原文写入失败: kb_id=%s title=%s", kb_id, title)
        raise HTTPException(status_code=500, detail="原文写入失败") from None
    except Exception:
        db.rollback()
        if candidate_path:
            _safe_delete(candidate_path)
        raise
    db.refresh(doc)
    _schedule_ingest(background_tasks, doc)
    return UploadResult(document=_doc_out(doc), content_changed=True)


@documents_router.post("/{doc_id}/reupload", response_model=UploadResult)
def reupload_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """重传覆盖：同文档换内容；sha256 未变则幂等跳过（US-M1-04）。

    内容变更后写入不可变候选文件并回到 pending，经 BackgroundTasks 触发 M2；
    成功时原文与块一起切换，失败时按 DM5 保留匹配的旧原文和旧块。
    """
    prepared = _validate_upload(file)
    doc = crud.get_document(db, doc_id, for_update=True)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    new_hash = prepared.content_hash

    if new_hash == doc.pending_content_hash:
        result = UploadResult(document=_doc_out(doc), content_changed=False)
        if doc.status in {DocStatus.PENDING.value, DocStatus.PROCESSING.value}:
            _schedule_ingest(background_tasks, doc)
        db.rollback()
        return result

    if new_hash == doc.content_hash:
        if doc.pending_file_path:
            abandoned_path = doc.pending_file_path
            doc.ingest_version += 1
            _clear_pending(doc)
            doc.status = (
                DocStatus.READY.value if doc.chunk_count > 0 else DocStatus.FAILED.value
            )
            doc.last_error_code = None
            doc.last_error_message = None
            _commit_document_and_vault(db, doc)
            db.refresh(doc)
            if abandoned_path != doc.file_path:
                _safe_delete(abandoned_path)
            return UploadResult(document=_doc_out(doc), content_changed=True)

        if doc.status == DocStatus.FAILED.value:
            doc.ingest_version += 1
            doc.pending_file_path = doc.file_path
            doc.pending_content_hash = doc.content_hash
            doc.pending_char_count = doc.char_count
            doc.status = DocStatus.PENDING.value
            doc.last_error_code = None
            doc.last_error_message = None
            _commit_document_and_vault(db, doc)
            db.refresh(doc)
            _schedule_ingest(background_tasks, doc)
        return UploadResult(document=_doc_out(doc), content_changed=False)

    next_version = doc.ingest_version + 1
    try:
        candidate_path = _save_candidate(prepared, doc.kb_id, doc.id, next_version)
    except OSError:
        logger.exception("原文覆盖写入失败: doc_id=%s", doc_id)
        raise HTTPException(status_code=500, detail="原文写入失败") from None

    abandoned_path = doc.pending_file_path
    doc.ingest_version = next_version
    doc.pending_file_path = candidate_path
    doc.pending_content_hash = new_hash
    doc.pending_char_count = len(prepared.text)
    doc.status = DocStatus.PENDING.value                # 内容变更 → 待重建（M2 驱动）
    doc.last_error_code = None
    doc.last_error_message = None
    try:
        _commit_document_and_vault(db, doc)
    except VaultTransactionError:
        # 清单恢复失败时保留候选字节，避免进一步破坏可能领先的文件真相。
        raise
    except Exception:
        _safe_delete(candidate_path)
        raise
    db.refresh(doc)
    if abandoned_path and abandoned_path not in {doc.file_path, candidate_path}:
        _safe_delete(abandoned_path)
    _schedule_ingest(background_tasks, doc)
    return UploadResult(document=_doc_out(doc), content_changed=True)


def _commit_document_and_vault(db: Session, doc: Document) -> None:
    """先发布文件真相，再提交派生数据库；失败时按回滚后的 DB 重写清单。"""
    commit_document(db, doc)


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


@documents_router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """删除文档：先删数据库行（真相源），再删原文文件（失败仅记日志）。"""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb_id = doc.kb_id
    crud.delete_document(db, doc_id)
    storage.delete_document_files(kb_id, doc_id)
    return Response(status_code=204)
