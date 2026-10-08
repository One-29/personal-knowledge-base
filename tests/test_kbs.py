"""知识库 CRUD 端点测试（US-M1-01、M1 契约 §3）。"""

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError


def _create_kb(client, name: str = "计算机网络", description: str | None = "计网笔记"):
    return client.post("/api/v1/kbs", json={"name": name, "description": description})


def test_create_kb_201(client):
    """创建成功：返回 id/doc_count/created_at（KBOut 契约）。"""
    r = _create_kb(client)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] > 0
    assert body["name"] == "计算机网络"
    assert body["description"] == "计网笔记"
    assert body["doc_count"] == 0
    assert "created_at" in body


def test_create_kb_duplicate_409(client):
    """同库名重复创建 → 409（name UNIQUE）。"""
    assert _create_kb(client).status_code == 201
    assert _create_kb(client).status_code == 409


def test_create_kb_invalid_422(client):
    """空白名称和数据库不兼容的 NUL 字符都在 API 边界拒绝。"""
    assert _create_kb(client, name="").status_code == 422
    assert _create_kb(client, name="   \n").status_code == 422
    assert _create_kb(client, name="坏\x00名称").status_code == 422


def test_create_kb_trims_human_input(client):
    response = _create_kb(client, name="  高等数学  ", description="  极限与积分  ")

    assert response.status_code == 201
    assert response.json()["name"] == "高等数学"
    assert response.json()["description"] == "极限与积分"


def test_create_kb_concurrent_duplicate_returns_409(client, db, monkeypatch):
    """两个请求同时通过预检时，数据库唯一约束仍应转换为业务冲突。"""
    from app.routers import kbs

    lookups = iter([None, object()])
    monkeypatch.setattr(kbs.crud, "get_kb_by_name", lambda *_args: next(lookups))

    def fail_create(*_args):
        raise IntegrityError("INSERT knowledge_bases", {}, Exception("duplicate"))

    monkeypatch.setattr(kbs.crud, "create_kb", fail_create)
    rollbacks = 0
    real_rollback = db.rollback

    def record_rollback():
        nonlocal rollbacks
        rollbacks += 1
        real_rollback()

    monkeypatch.setattr(db, "rollback", record_rollback)

    response = _create_kb(client, name="并发同名")

    assert response.status_code == 409
    assert response.json()["detail"] == "知识库名称已存在"
    assert rollbacks == 1


def test_list_kbs_returns_created(client):
    """列表按创建时间倒序，含刚创建的库。"""
    _create_kb(client, name="操作系统")
    _create_kb(client, name="计算机网络")
    r = client.get("/api/v1/kbs")
    assert r.status_code == 200
    names = [kb["name"] for kb in r.json()]
    assert names == ["计算机网络", "操作系统"]


def test_list_kbs_loads_document_counts_in_two_queries(client, db):
    """多个知识库的文档计数批量预加载，查询数不随知识库数量增长。"""
    from app.models import Document

    for index in range(4):
        kb_id = _create_kb(client, name=f"kb-{index}").json()["id"]
        for doc_index in range(index):
            db.add(Document(
                kb_id=kb_id, title=f"doc-{doc_index}.md", file_path="unused.md",
                content_hash=f"hash-{index}-{doc_index}", char_count=1,
            ))
    db.commit()
    db.expunge_all()
    selects = []
    bind = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        response = client.get("/api/v1/kbs")
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert response.status_code == 200
    assert {kb["name"]: kb["doc_count"] for kb in response.json()} == {
        f"kb-{index}": index for index in range(4)
    }
    assert len(selects) == 2


def test_get_kb_404(client):
    """不存在的库 → 404。"""
    assert client.get("/api/v1/kbs/999999").status_code == 404


def test_delete_kb_204_then_404(client):
    """删除成功 204，随后查询 404。"""
    kb_id = _create_kb(client).json()["id"]
    assert client.delete(f"/api/v1/kbs/{kb_id}").status_code == 204
    assert client.get(f"/api/v1/kbs/{kb_id}").status_code == 404


def test_delete_kb_404(client):
    """删除不存在的库 → 404。"""
    assert client.delete("/api/v1/kbs/999999").status_code == 404


def test_delete_kb_with_documents_cascades(client):
    """删除含文档的库：数据库 ON DELETE CASCADE 清理，不触发 ORM 置空外键（回归）。"""
    kb_id = _create_kb(client).json()["id"]
    upload = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("tcp.md", "# TCP\n三次握手", "text/markdown")},
    )
    doc_id = upload.json()["document"]["id"]

    assert client.delete(f"/api/v1/kbs/{kb_id}").status_code == 204
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/v1/kbs/{kb_id}").status_code == 404
