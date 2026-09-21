"""ORM 元数据与活动原文到 Vault 清单的同步边界。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import package_storage
from app.models import Document, KnowledgeBase

from .models import (
    DocumentRecord,
    KnowledgeBaseRecord,
    SourceRecord,
    VaultSnapshot,
)
from .store import VaultError, VaultStore


def record_knowledge_base(
    kb: KnowledgeBase,
    *,
    store: VaultStore | None = None,
) -> None:
    catalog = store or VaultStore()
    record = _knowledge_base_record(kb)
    catalog.update(lambda snapshot: snapshot.with_knowledge_base(record))


def forget_knowledge_base(
    kb_id: int,
    *,
    store: VaultStore | None = None,
) -> None:
    catalog = store or VaultStore()
    catalog.update(lambda snapshot: snapshot.without_knowledge_base(kb_id))


def record_document(
    doc: Document,
    *,
    store: VaultStore | None = None,
) -> None:
    catalog = store or VaultStore()
    record = _document_record(doc, catalog.root)
    catalog.update(lambda snapshot: snapshot.with_document(record))


def forget_document(
    doc_id: int,
    *,
    store: VaultStore | None = None,
) -> None:
    catalog = store or VaultStore()
    catalog.update(lambda snapshot: snapshot.without_document(doc_id))


def snapshot_database(
    db: Session,
    *,
    store: VaultStore | None = None,
) -> VaultSnapshot:
    """完整导出当前业务元数据；chunks 是可重建派生物，不进入清单。"""
    catalog = store or VaultStore()
    snapshot = database_snapshot(db, store=catalog)
    catalog.write(snapshot)
    return snapshot


def database_snapshot(
    db: Session,
    *,
    store: VaultStore | None = None,
) -> VaultSnapshot:
    """读取并校验当前业务元数据，但不改写磁盘清单。"""
    catalog = store or VaultStore()
    knowledge_bases = list(
        db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.id)).all()
    )
    documents = list(db.scalars(select(Document).order_by(Document.id)).all())
    return VaultSnapshot(
        knowledge_bases=tuple(_knowledge_base_record(kb) for kb in knowledge_bases),
        documents=tuple(_document_record(doc, catalog.root) for doc in documents),
    )


def ensure_snapshot(
    db: Session,
    *,
    store: VaultStore | None = None,
) -> VaultSnapshot:
    catalog = store or VaultStore()
    existing = catalog.load()
    return existing if existing is not None else snapshot_database(db, store=catalog)


def verify_database_matches_snapshot(
    db: Session,
    snapshot: VaultSnapshot,
    *,
    store: VaultStore | None = None,
) -> VaultSnapshot:
    """核对数据库的用户元数据；只自动接受内容未变的 mtime 刷新。"""
    catalog = store or VaultStore()
    current = database_snapshot(db, store=catalog)
    if current == snapshot:
        return current
    if _without_mtime(current) == _without_mtime(snapshot):
        catalog.write(current)
        return current
    raise VaultError(
        "SQLite 与 Vault 清单不一致。为避免覆盖原文，启动已停止；"
        "请保留 storage 目录并移走 knowbase.db 后重新启动重建"
    )


def verify_snapshot_sources(
    snapshot: VaultSnapshot,
    *,
    store: VaultStore | None = None,
) -> None:
    catalog = store or VaultStore()
    for document in snapshot.documents:
        _verify_source(document.active, catalog.root)
        if document.pending is not None:
            _verify_source(document.pending, catalog.root)


def _knowledge_base_record(kb: KnowledgeBase) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        created_at=_timestamp(kb.created_at),
        updated_at=_timestamp(kb.updated_at),
    )


def _document_record(doc: Document, root: Path) -> DocumentRecord:
    if not doc.file_path:
        raise VaultError(f"文档 {doc.id} 缺少活动原文路径")
    active = _capture_source(
        doc.file_path,
        doc.content_hash,
        doc.char_count,
        root,
    )
    pending = None
    if doc.pending_file_path:
        if doc.pending_content_hash is None or doc.pending_char_count is None:
            raise VaultError(f"文档 {doc.id} 的候选原文元数据不完整")
        pending = _capture_source(
            doc.pending_file_path,
            doc.pending_content_hash,
            doc.pending_char_count,
            root,
        )
    return DocumentRecord(
        id=doc.id,
        kb_id=doc.kb_id,
        title=doc.title,
        ingest_version=doc.ingest_version,
        active=active,
        pending=pending,
        created_at=_timestamp(doc.created_at),
        updated_at=_timestamp(doc.updated_at),
    )


def _capture_source(
    rel_path: str,
    content_hash: str,
    char_count: int,
    root: Path,
) -> SourceRecord:
    path = _safe_source_path(root, rel_path)
    before = path.stat()
    text, actual_hash = _read_and_verify(rel_path, content_hash, root)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise VaultError(f"记录清单时原文发生变化：{rel_path}")
    if len(text) != char_count:
        raise VaultError(
            f"原文字符数不一致：{rel_path}，数据库={char_count}，实际={len(text)}"
        )
    if actual_hash != content_hash:
        raise VaultError(f"原文摘要不一致：{rel_path}")
    return SourceRecord(
        path=rel_path,
        content_hash=content_hash,
        char_count=char_count,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def _verify_source(record: SourceRecord, root: Path) -> None:
    actual = _capture_source(
        record.path,
        record.content_hash,
        record.char_count,
        root,
    )
    if actual.size != record.size:
        raise VaultError(f"原文大小与 Vault 清单不一致：{record.path}")


def _read_and_verify(
    rel_path: str,
    expected_hash: str,
    root: Path,
) -> tuple[str, str]:
    try:
        manifest = package_storage.load_package_manifest(
            rel_path,
            storage_root=root,
        )
        if manifest is not None:
            _manifest, text = package_storage.verify_stored_package(
                rel_path,
                expected_package_hash=expected_hash,
                storage_root=root,
            )
            return text, expected_hash
        data = _safe_source_path(root, rel_path).read_bytes()
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise VaultError(f"无法验证原文 {rel_path}：{exc}") from exc


def _safe_source_path(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise VaultError(f"原文不存在或越出存储目录：{rel_path}")
    return candidate


def _timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _without_mtime(snapshot: VaultSnapshot) -> VaultSnapshot:
    return VaultSnapshot(
        knowledge_bases=snapshot.knowledge_bases,
        documents=tuple(
            document.model_copy(
                update={
                    "active": document.active.model_copy(update={"mtime_ns": 0}),
                    "pending": (
                        document.pending.model_copy(update={"mtime_ns": 0})
                        if document.pending is not None
                        else None
                    ),
                }
            )
            for document in snapshot.documents
        ),
    )
