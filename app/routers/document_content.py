"""文档内容读取边界：原文、图片清单与不可变原图。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import crud, document_images, package_storage, storage
from ..db import get_db
from ..schemas import DocumentContentOut, DocumentImageOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["Documents"])


def _image_out(
    items: list[document_images.ImageReferenceData],
) -> list[DocumentImageOut]:
    return [DocumentImageOut.model_validate(item) for item in items]


@router.get(
    "/{doc_id}/content",
    response_model=DocumentContentOut,
    response_model_exclude_defaults=True,
)
def get_document_content(doc_id: int, db: Session = Depends(get_db)):
    """返回当前可检索版本的 Markdown 原文和图片出现清单。"""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        content = storage.read(doc.file_path)
    except FileNotFoundError:
        logger.warning("原文文件缺失: doc_id=%s path=%s", doc.id, doc.file_path)
        raise HTTPException(status_code=404, detail="原文文件缺失") from None
    try:
        manifest = package_storage.load_package_manifest(doc.file_path)
        package_storage.verify_package_source(manifest, content)
        images = document_images.images_from_manifest(doc.id, manifest)
    except package_storage.StorageIntegrityError:
        logger.exception("图片包完整性校验失败: doc_id=%s path=%s", doc.id, doc.file_path)
        raise HTTPException(status_code=500, detail="图片包完整性校验失败") from None
    return DocumentContentOut(title=doc.title, content=content, images=_image_out(images))


@router.get(
    "/{doc_id}/versions/{version}/images/{ordinal}",
    response_class=FileResponse,
)
def get_document_image(
    doc_id: int,
    version: int,
    ordinal: int,
    db: Session = Depends(get_db),
):
    """返回指定文档版本的一次原图出现；读取时核对清单和 SHA-256。"""
    if version < 1 or ordinal < 1:
        raise HTTPException(status_code=404, detail="图片不存在")
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        resolved = package_storage.resolve_version_image(
            doc.kb_id, doc.id, version, ordinal
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="图片原件缺失") from None
    except package_storage.StorageIntegrityError:
        logger.exception(
            "图片原件完整性校验失败: doc_id=%s version=%s ordinal=%s",
            doc.id,
            version,
            ordinal,
        )
        raise HTTPException(status_code=500, detail="图片原件完整性校验失败") from None
    if resolved is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    path, asset, _ = resolved
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
        asset["mime_type"]
    ]
    return FileResponse(
        path,
        media_type=asset["mime_type"],
        filename=f"image-{ordinal}{suffix}",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{asset["content_hash"]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
