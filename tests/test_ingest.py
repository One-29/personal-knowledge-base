"""入库管线测试（M2 编排层）。

覆盖：切分落库成功、向量维度、重传全量替换、失败保旧（DM5）、缺文件失败。
统一使用假 embedding provider——不打真实 API（conftest 的 _fake_embedding）。
"""

from sqlalchemy import func, select

from app import ingest, storage
from app.models import Chunk, Document

MD = """# TCP 三次握手

客户端发 SYN，服务端回 SYN+ACK，客户端再回 ACK。

## 为什么是三次

两次无法确认客户端接收能力。
"""


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
    assert len(chunks[0].embedding) == 1536             # 维度与 DR2 一致


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
