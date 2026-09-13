"""入库管线测试（M2 编排层）。

覆盖：切分落库成功、向量维度、重传全量替换、失败保旧（DM5）、缺文件失败。
统一使用假 embedding provider——不打真实 API（conftest 的 _fake_embedding）。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select, text

from app import ingest, storage
from app.core.config import settings
from app.embedding import EmbeddingError
from app.models import Chunk, Document

MD = """# TCP 三次握手

客户端发 SYN，服务端回 SYN+ACK，客户端再回 ACK。

## 为什么是三次

两次无法确认客户端接收能力。
"""


@pytest.mark.parametrize(
    "vectors,expected_message",
    [
        ([[0.0] * 3], "返回数量不匹配"),
        ([[0.0] * 2, [0.0] * 3], "维度不匹配"),
    ],
)
def test_embedding_batch_contract_rejects_silent_chunk_loss(
    vectors, expected_message, monkeypatch
):
    """provider 少返回向量或维度错误时立即失败，不能让 zip 静默少写块。"""
    monkeypatch.setattr(settings, "embedding_dimension", 3)
    with pytest.raises(EmbeddingError, match=expected_message):
        ingest._validate_vectors(vectors, expected_count=2)


def _create_doc(db, kb_id: int, title: str = "tcp.md") -> Document:
    """造一篇已登记的文档（跳过 HTTP 层，直击管线）。"""
    rel = storage.save(kb_id, 9999, MD.encode("utf-8"))
    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path=rel,
        content_hash="hash-" + title,
        char_count=len(MD),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_process_document_creates_chunks(db, client):
    """成功路径：块落库、状态 ready、chunk_count 与偏移正确。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    doc = _create_doc(db, kb_id)

    ingest.process_document(doc.id, db)

    db.refresh(doc)
    assert doc.status == "ready"
    assert doc.chunk_count == 2
    chunks = db.scalars(
        select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_index)
    ).all()
    assert len(chunks) == 2
    assert chunks[0].content.startswith("# TCP 三次握手")
    assert chunks[0].char_start == 0
    assert chunks[0].kb_id == kb_id                     # 冗余归属（DM3）
    assert len(chunks[0].embedding) == settings.embedding_dimension  # 与 DR2 配置一致


def test_reprocess_replaces_chunks(db, client):
    """重传重建：全量替换，块数变化且不残留旧块（US-M1-04）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    doc = _create_doc(db, kb_id)
    ingest.process_document(doc.id, db)
    first_ids = set(db.scalars(select(Chunk.id).where(Chunk.doc_id == doc.id)).all())

    storage.save(kb_id, doc.id, "# 只有一个标题\n内容".encode("utf-8"))
    doc.file_path = f"{kb_id}/{doc.id}.md"
    db.commit()

    ingest.process_document(doc.id, db)

    db.refresh(doc)
    new_chunks = db.scalars(select(Chunk).where(Chunk.doc_id == doc.id)).all()
    assert doc.chunk_count == len(new_chunks) == 1
    assert not first_ids & {c.id for c in new_chunks}   # 旧块已删


def test_missing_file_marks_failed(db, client):
    """原文缺失且无旧块 → status=failed + PARSE_FAILED（DM5）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    doc = _create_doc(db, kb_id, title="missing.md")
    storage.delete(doc.file_path)

    ingest.process_document(doc.id, db)

    db.refresh(doc)
    assert doc.status == "failed"
    assert doc.last_error_code == ingest.ERROR_PARSE_FAILED
    assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc.id)) == 0


def test_failure_keeps_old_chunks_and_ready(db, client):
    """重传失败但旧块仍在 → 回滚 ready 保旧 + 记录错误（DM5 核心语义）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    doc = _create_doc(db, kb_id)
    ingest.process_document(doc.id, db)
    assert doc.status == "ready"

    # 模拟「重传后原文被删除」→ 处理失败，但旧块还在
    storage.delete(doc.file_path)
    ingest.process_document(doc.id, db)

    db.refresh(doc)
    assert doc.status == "ready"                        # 保旧，不标 failed
    assert doc.last_error_code == ingest.ERROR_PARSE_FAILED
    assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc.id)) > 0


@pytest.mark.parametrize("has_old_chunks", [False, True])
@pytest.mark.parametrize("failure_kind", ["provider", "sql"])
def test_unexpected_failure_restores_document_status_and_old_chunks(
    db, client, monkeypatch, has_old_chunks, failure_kind
):
    """意外 provider 异常或真正 SQL 事务失败均不得遗留 processing 或丢旧块。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "异常恢复测试"}).json()["id"]
    doc = _create_doc(db, kb_id)
    if has_old_chunks:
        ingest.process_document(doc.id, db)
    doc_id = doc.id
    snapshot_query = (
        select(Chunk.id, Chunk.content, Chunk.char_start, Chunk.char_end)
        .where(Chunk.doc_id == doc_id)
        .order_by(Chunk.chunk_index)
    )
    old_chunks = db.execute(snapshot_query).all()

    if failure_kind == "provider":
        provider = Mock()
        provider.embed_texts.side_effect = RuntimeError("unexpected provider result")
        monkeypatch.setattr(ingest.embedding, "get_embedding_provider", lambda: provider)
        expected_message = "unexpected provider result"
    else:
        def fail_after_old_chunks_deleted(instances):
            # 管线已在同一事务删除旧块；实际 PostgreSQL 错误将事务置为 aborted。
            db.execute(text("SELECT 1 / 0"))

        monkeypatch.setattr(db, "add_all", fail_after_old_chunks_deleted)
        expected_message = "division by zero"

    ingest.process_document(doc_id, db)

    db.refresh(doc)
    assert doc.status == ("ready" if has_old_chunks else "failed")
    assert doc.last_error_code == ingest.ERROR_PROCESS_FAILED
    assert expected_message in doc.last_error_message
    assert doc.processed_at is not None
    assert doc.chunk_count == len(old_chunks)
    assert db.execute(snapshot_query).all() == old_chunks
    assert db.scalar(text("SELECT 42")) == 42


def test_failed_status_recovery_logs_both_errors_without_claiming_success(monkeypatch, caplog):
    """数据库持续不可用时记录原始处理异常和恢复异常，不伪称状态已落库。"""
    doc = SimpleNamespace(status="pending", file_path="missing.md")
    db = Mock()
    monkeypatch.setattr(
        ingest.crud, "get_document", Mock(side_effect=[doc, RuntimeError("database unavailable")])
    )
    monkeypatch.setattr(ingest.storage, "read", Mock(side_effect=RuntimeError("read failed")))

    ingest.process_document(17, db)

    assert "read failed" in caplog.text
    assert "database unavailable" in caplog.text
    assert "恢复文档状态也失败" in caplog.text
    assert "文档处理完成" not in caplog.text
    assert "文档处理失败: doc_id=" not in caplog.text
    assert db.rollback.call_count == 2
