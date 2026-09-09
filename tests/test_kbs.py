"""知识库 CRUD 端点测试（US-M1-01、M1 契约 §3）。"""


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
    """空名称 → 422（Pydantic 校验 min_length=1）。"""
    assert _create_kb(client, name="").status_code == 422


def test_list_kbs_returns_created(client):
    """列表按创建时间倒序，含刚创建的库。"""
    _create_kb(client, name="操作系统")
    _create_kb(client, name="计算机网络")
    r = client.get("/api/v1/kbs")
    assert r.status_code == 200
    names = [kb["name"] for kb in r.json()]
    assert names == ["计算机网络", "操作系统"]


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
