"""启动时编排已登记原文的外部修改同步。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import document_index
from app.core.config import Settings, settings
from app.embedding import EmbeddingError
from app.models import Document, KnowledgeBase

from .external_index import document_fingerprint, publish_index_update
from .external_source import ExternalSourceSyncError, observe_source
from .models import DocumentRecord, KnowledgeBaseRecord, VaultSnapshot
from .store import VaultStore

__all__ = [
    "ExternalSourceSync",
    "ExternalSourceSyncError",
    "reconcile_external_sources",
]


@dataclass(frozen=True)
class ExternalSourceSync:
    checked: int = 0
    refreshed: int = 0
    reindexed: int = 0
    resumed: int = 0


def reconcile_external_sources(
    db: Session,
    snapshot: VaultSnapshot,
    *,
    store: VaultStore | None = None,
    config: Settings = settings,
) -> tuple[VaultSnapshot, ExternalSourceSync]:
    """校验已登记来源，并把普通文本外部修改推进 Vault 与单篇索引。

    Vault 是重建真相，因此内容变化时先发布新的清单记录，再提交对应文档的
    SQLite chunks。两步之间退出会留下“数据库落后于 Vault”的可识别状态；
    下次启动重新向量化并继续，而不会用旧数据库反向覆盖新原文。
    """
    catalog = store or VaultStore()
    _validate_knowledge_bases(db, snapshot)
    _validate_document_ids(db, snapshot)

    current = snapshot
    refreshed = 0
    reindexed = 0
    resumed = 0

    for original in snapshot.documents:
        record = _document_by_id(current, original.id)
        document = db.get(Document, record.id)
        assert document is not None
        relation = _database_relation(document, record)
        expected_database = document_fingerprint(document)
        database_ingest_version = document.ingest_version
        database_pending_path = document.pending_file_path
        database_updated_at = _utc_timestamp(document.updated_at)
        # 文件读取和 embedding 都可能耗时；所需字段已经快照到内存，
        # 在远程调用前结束只读事务并归还连接。
        db.rollback()

        active = observe_source(
            record.active,
            root=catalog.root,
            config=config,
            force_text=relation == "behind",
        )
        pending = (
            observe_source(
                record.pending,
                root=catalog.root,
                config=config,
                force_text=False,
            )
            if record.pending is not None
            else None
        )
        if pending is not None and pending.content_changed:
            raise ExternalSourceSyncError(
                f"文档 {record.id} 的候选原文被外部修改；"
                "请恢复该文件，或在 KnowBase 中重新上传"
            )

        if active.content_changed and (
            record.pending is not None or database_pending_path is not None
        ):
            raise ExternalSourceSyncError(
                f"文档 {record.id} 仍有待处理候选版本，不能同时接收外部编辑；"
                "请恢复活动原文并先完成或取消重传"
            )

        needs_index = relation == "behind" or active.content_changed
        if not needs_index:
            refreshed_record = record.model_copy(
                update={
                    "active": active.record,
                    "pending": pending.record if pending is not None else None,
                    # status/last_error 的普通事务也可能触发数据库 updated_at；
                    # 内容身份一致时接受数据库的最新观察值。
                    "updated_at": database_updated_at,
                }
            )
            if refreshed_record != record:
                current = current.with_document(refreshed_record)
                catalog.write(current)
                refreshed += 1
            continue

        if active.text is None:
            active = observe_source(
                active.record,
                root=catalog.root,
                config=config,
                force_text=True,
            )
        assert active.text is not None

        protected_spans = tuple(
            (int(item["char_start"]), int(item["char_end"]))
            for item in (active.package_manifest or {}).get("occurrences", [])
        )
        chunks = document_index.split_source(
            active.text,
            config=config,
            protected_spans=protected_spans,
        )
        if not chunks:
            raise ExternalSourceSyncError(
                f"文档 {record.id} 的外部原文切分后没有有效内容"
            )
        try:
            vectors = document_index.embed_chunks(chunks, config=config)
        except EmbeddingError as exc:
            raise ExternalSourceSyncError(
                f"文档 {record.id} 的外部修改暂时无法向量化：{exc}"
            ) from exc

        # 模型调用可能持续数秒；提交前重新读一遍，避免把编辑到一半的版本
        # 与刚才生成的向量组合在一起。
        confirmed = observe_source(
            active.record,
            root=catalog.root,
            config=config,
            force_text=True,
        )
        if confirmed.content_changed or confirmed.text != active.text:
            raise ExternalSourceSyncError(
                f"文档 {record.id} 在同步期间再次发生变化；"
                "请保存完成后重新启动 KnowBase"
            )

        if active.content_changed:
            target_record = record.model_copy(
                update={
                    "ingest_version": max(
                        record.ingest_version,
                        database_ingest_version,
                    )
                    + 1,
                    "active": confirmed.record,
                    "pending": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        else:
            # 上次启动可能已经把新原文写进 Vault、但尚未提交 SQLite。
            # 此时严格使用 Vault 中的版本和时间戳完成同一逻辑提交。
            target_record = record.model_copy(
                update={
                    "active": confirmed.record,
                    "pending": None,
                }
            )

        target_snapshot = current.with_document(target_record)
        publish_index_update(
            db,
            store=catalog,
            snapshot=target_snapshot,
            expected_database=expected_database,
            target=target_record,
            text=confirmed.text or "",
            chunks=chunks,
            vectors=vectors,
            embedding_dimension=config.embedding_dimension,
        )
        current = target_snapshot
        if relation == "behind":
            resumed += 1
        if active.content_changed:
            reindexed += 1

    return current, ExternalSourceSync(
        checked=len(snapshot.documents),
        refreshed=refreshed,
        reindexed=reindexed,
        resumed=resumed,
    )


def _validate_knowledge_bases(db: Session, snapshot: VaultSnapshot) -> None:
    rows = list(db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.id)))
    expected = list(snapshot.knowledge_bases)
    if [row.id for row in rows] != [item.id for item in expected]:
        raise ExternalSourceSyncError("SQLite 与 Vault 的知识库集合不一致")
    for row, item in zip(rows, expected, strict=True):
        current = KnowledgeBaseRecord(
            id=row.id,
            name=row.name,
            description=row.description,
            created_at=_utc_timestamp(row.created_at),
            updated_at=_utc_timestamp(row.updated_at),
        )
        if current != item:
            raise ExternalSourceSyncError(
                f"知识库 {item.id} 的 SQLite 元数据与 Vault 不一致"
            )
    db.rollback()


def _validate_document_ids(db: Session, snapshot: VaultSnapshot) -> None:
    ids = list(db.scalars(select(Document.id).order_by(Document.id)))
    if ids != [item.id for item in snapshot.documents]:
        raise ExternalSourceSyncError("SQLite 与 Vault 的文档集合不一致")
    db.rollback()


def _database_relation(document: Document, record: DocumentRecord) -> str:
    if (
        document.kb_id != record.kb_id
        or document.title != record.title
        or document.file_path != record.active.path
        or _utc_timestamp(document.created_at) != record.created_at
    ):
        raise ExternalSourceSyncError(
            f"文档 {record.id} 的身份元数据在 SQLite 与 Vault 之间不一致"
        )

    pending = record.pending
    pending_matches = (
        document.pending_file_path == (pending.path if pending is not None else None)
        and document.pending_content_hash
        == (pending.content_hash if pending is not None else None)
        and document.pending_char_count
        == (pending.char_count if pending is not None else None)
    )
    active_matches = (
        document.content_hash == record.active.content_hash
        and document.char_count == record.active.char_count
        and document.ingest_version == record.ingest_version
    )
    if active_matches and pending_matches:
        return "aligned"

    if (
        pending is None
        and document.pending_file_path is None
        and document.pending_content_hash is None
        and document.pending_char_count is None
        and document.ingest_version < record.ingest_version
    ):
        return "behind"

    raise ExternalSourceSyncError(
        f"文档 {record.id} 的 SQLite 内容状态无法由 Vault 安全解释"
    )


def _document_by_id(snapshot: VaultSnapshot, doc_id: int) -> DocumentRecord:
    return next(item for item in snapshot.documents if item.id == doc_id)


def _utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        raise ExternalSourceSyncError("SQLite 时间戳缺失")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
