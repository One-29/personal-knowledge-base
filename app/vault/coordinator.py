"""日常 SQLite 事务与文件系统 Vault 清单的一致性协调。"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import (
    DatabaseLocationError,
    dialect_name,
    parse_database_location,
    session_database_location,
)
from app.models import Document, KnowledgeBase

from .store import VaultError, VaultStore
from .sync import (
    ensure_snapshot,
    forget_document,
    forget_knowledge_base,
    record_document,
    record_knowledge_base,
    snapshot_database,
)

_SESSION_STORE_KEY = "knowbase_vault_store"
_UNMANAGED = object()
_mutation_lock = RLock()


class VaultTransactionError(VaultError):
    """数据库提交失败后，Vault 清单也未能恢复到数据库现状。"""


def bind_managed_store(db: Session, store: VaultStore) -> None:
    """为自定义运行时 Session 显式绑定同一用户目录的 Vault。"""
    db.info[_SESSION_STORE_KEY] = store


def managed_store(db: Session) -> VaultStore | None:
    """仅为配置所指向的日常文件 SQLite 启用 Vault。"""
    cached = db.info.get(_SESSION_STORE_KEY)
    if cached is _UNMANAGED:
        return None
    if isinstance(cached, VaultStore):
        return cached

    if dialect_name(db) != "sqlite" or settings.database_url is None:
        db.info[_SESSION_STORE_KEY] = _UNMANAGED
        return None
    try:
        configured = parse_database_location(settings.database_url)
        if configured.backend != "sqlite":
            db.info[_SESSION_STORE_KEY] = _UNMANAGED
            return None
        actual = session_database_location(db, settings.database_url)
    except DatabaseLocationError:
        db.info[_SESSION_STORE_KEY] = _UNMANAGED
        return None
    if configured != actual:
        db.info[_SESSION_STORE_KEY] = _UNMANAGED
        return None

    store = VaultStore()
    db.info[_SESSION_STORE_KEY] = store
    return store


def commit_knowledge_base(db: Session, kb: KnowledgeBase) -> None:
    _commit_change(
        db,
        lambda store: record_knowledge_base(kb, store=store),
    )


def commit_document(db: Session, document: Document) -> None:
    _commit_change(
        db,
        lambda store: record_document(document, store=store),
    )


def commit_without_knowledge_base(db: Session, kb_id: int) -> None:
    _commit_change(
        db,
        lambda store: forget_knowledge_base(kb_id, store=store),
    )


def commit_without_document(db: Session, doc_id: int) -> None:
    _commit_change(
        db,
        lambda store: forget_document(doc_id, store=store),
    )


def commit_full_snapshot(db: Session) -> None:
    """提交涉及多行的原子切换，并把完整业务元数据写入 Vault。"""
    _commit_change(
        db,
        lambda store: snapshot_database(db, store=store),
        ensure_first=False,
    )


def _commit_change(
    db: Session,
    change: Callable[[VaultStore], object],
    *,
    ensure_first: bool = True,
) -> None:
    store = managed_store(db)
    if store is None:
        db.commit()
        return

    with _mutation_lock:
        try:
            # flush 先固定 server default、更新时间与删除结果；清单由此记录的
            # 正是待提交状态，而不是 ORM 尚未落到事务里的近似值。
            db.flush()
            if ensure_first:
                ensure_snapshot(db, store=store)
            change(store)
            db.commit()
        except Exception as original:
            db.rollback()
            try:
                # 原子清单写入本身不会留下半文件。若失败发生在 DB commit，
                # 则按 rollback 后的真实数据库重写，消除“清单领先”状态。
                snapshot_database(db, store=store)
            except Exception as recovery:
                raise VaultTransactionError(
                    "数据库事务失败，且 Vault 清单无法恢复；"
                    "请停止写入并重新运行启动自检"
                ) from ExceptionGroup(
                    "Vault transaction and recovery both failed",
                    [original, recovery],
                )
            raise
