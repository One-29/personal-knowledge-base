"""通过实际文档接口重建索引：同内容、失败回退、图片包和任务冲突。"""

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import embedding, ingest_tasks, storage
from app.core.config import settings
from app.database import create_database_engine, initialize_database
from app.db import get_db
from app.models import Chunk, Document, KnowledgeBase
from app.routers import documents
from app.vault.coordinator import commit_document


@pytest.fixture()
def reindex_api(monkeypatch):
    engine = create_database_engine("sqlite+pysqlite://", pool_size=2, max_overflow=1, pool_recycle=1800, pool_timeout=2)
    initialize_database(engine)
    with Session(engine, expire_on_commit=False) as db:
        kb = KnowledgeBase(name="重建测试库")
        db.add(kb)
        db.commit()
        app = FastAPI()
        app.include_router(documents.kb_documents_router, prefix="/api/v1")
        app.include_router(documents.documents_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: db
        scheduled = []
        monkeypatch.setattr(documents, "_run_ingest_task", scheduled.append)
        with TestClient(app) as client:
            yield db, client, kb.id, scheduled
    engine.dispose()


def seed_old_index(api, text):
    db, client, kb, scheduled = api
    response = client.post(f"/api/v1/kbs/{kb}/documents", files={"file": ("条件.md", text.encode(), "text/markdown")})
    assert response.status_code == 201, response.text
    doc = db.get(Document, response.json()["document"]["id"])
    # 模拟升级前已经发布的字符窗口索引。
    ranges = [(0, min(800, len(text)))]
    if len(text) > 800:
        ranges.append((720, len(text)))
    for i, (a, b) in enumerate(ranges):
        db.add(Chunk(doc_id=doc.id, kb_id=kb, chunk_index=i, content=text[a:b],
                     char_start=a, char_end=b, embedding=[.1] * settings.embedding_dimension))
    doc.chunk_count = len(ranges)
    doc.status = "ready"
    doc.pending_file_path = doc.pending_content_hash = doc.pending_char_count = None
    commit_document(db, doc)
    scheduled.clear()
    return doc


def chunk_state(db, doc):
    return list(db.execute(select(Chunk.id, Chunk.content, Chunk.char_start, Chunk.char_end).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_index)).all())


def test_same_content_reindexes_with_new_boundaries_and_preserves_original(reindex_api):
    db, client, _, scheduled = reindex_api
    text = "背景。" * 239 + "如果不开启缓存，性能会下降。" + "后续说明。" * 50
    doc = seed_old_index(reindex_api, text)
    original = storage.resolve_relative(doc.file_path).read_bytes()
    old = chunk_state(db, doc)
    version = doc.ingest_version
    response = client.post(f"/api/v1/documents/{doc.id}/reindex")
    assert response.status_code == 202, response.text
    assert response.json()["content_changed"] is False
    assert response.json()["document"]["task"]["status"] == "queued"
    assert chunk_state(db, doc) == old and doc.ingest_version == version + 1
    assert client.post(f"/api/v1/documents/{doc.id}/reindex").status_code == 409
    assert ingest_tasks.run_ingest_task(scheduled[-1], db) == "succeeded"
    db.refresh(doc)
    assert doc.status == "ready" and doc.last_error_code is None
    assert storage.resolve_relative(doc.file_path).read_bytes() == original
    assert chunk_state(db, doc) != old
    assert all("如果不开启缓存，性能会下降。" in c.content for c in chunk_state(db, doc) if "开启缓存，性能会下降" in c.content)


@pytest.mark.parametrize("failure", ["embedding", "unsplittable"])
def test_failed_rebuild_preserves_old_index_and_source(reindex_api, monkeypatch, failure):
    db, client, _, scheduled = reindex_api
    text = "没有安全断点" * 400 if failure == "unsplittable" else "如果不开启缓存，性能会下降。"
    doc = seed_old_index(reindex_api, text)
    old = chunk_state(db, doc)

    class FailingProvider:
        def embed_texts(self, texts):
            if failure == "unsplittable":
                pytest.fail("超长不可分单元必须在调用向量服务前失败")
            raise embedding.EmbeddingError("test provider unavailable")

    monkeypatch.setattr(embedding, "get_embedding_provider", lambda *args: FailingProvider())
    assert client.post(f"/api/v1/documents/{doc.id}/reindex").status_code == 202
    assert ingest_tasks.run_ingest_task(scheduled[-1], db) == "failed"
    db.refresh(doc)
    assert doc.status == "ready" and doc.pending_file_path is None
    assert chunk_state(db, doc) == old
    assert storage.read(doc.file_path) == text
    assert doc.last_error_code == ("PARSE_FAILED" if failure == "unsplittable" else "EMBED_FAILED")


def test_missing_source_does_not_change_index_or_enqueue(reindex_api):
    db, client, _, scheduled = reindex_api
    doc = seed_old_index(reindex_api, "完整原文。")
    old = chunk_state(db, doc)
    # 只修改隔离测试库中的路径，不删除任何原文。
    doc.file_path = "missing/source.md"
    db.commit()
    response = client.post(f"/api/v1/documents/{doc.id}/reindex")
    assert response.status_code == 409 and "原文文件缺失" in response.json()["detail"]
    assert scheduled == [] and chunk_state(db, doc) == old


def test_reindex_keeps_multiple_image_files_byte_identical(reindex_api):
    db, client, kb, scheduled = reindex_api
    markdown = "# 图片\r\n\r\n第一幅。![甲](images/a.png)\r\n第二幅。![乙](images/b.png)\r\n"
    package = io.BytesIO()
    originals = {}
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("notes.md", markdown.encode())
        for name, color in [("a", "red"), ("b", "blue")]:
            png = io.BytesIO()
            Image.new("RGB", (2, 2), color).save(png, format="PNG")
            originals[name] = png.getvalue()
            archive.writestr(f"images/{name}.png", png.getvalue())
    response = client.post(f"/api/v1/kbs/{kb}/documents", files={"file": ("notes.zip", package.getvalue(), "application/zip")})
    assert response.status_code == 201, response.text
    assert ingest_tasks.run_ingest_task(scheduled[-1], db) == "succeeded"
    doc = db.get(Document, response.json()["document"]["id"])
    root = storage.resolve_relative(doc.file_path).parent
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert client.post(f"/api/v1/documents/{doc.id}/reindex").status_code == 202
    assert ingest_tasks.run_ingest_task(scheduled[-1], db) == "succeeded"
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after and storage.read(doc.file_path) == markdown
    assert all(data in after.values() for data in originals.values())
