"""文档端点测试（US-M1-02~06、M1 契约 §3）。

覆盖：上传登记、幂等重传、内容变更重传、列表过滤、原文读取、删除，
以及写库前的四类校验（422/413/400）与查重（409）。
"""

MD = ("tcp.md", "# TCP\n三次握手与四次挥手", "text/markdown")


def _create_kb(client, name: str = "计算机网络") -> int:
    return client.post("/api/v1/kbs", json={"name": name}).json()["id"]


def _upload(client, kb_id: int, file=MD):
    return client.post(f"/api/v1/kbs/{kb_id}/documents", files={"file": file})


def test_upload_201_pending(client):
    """上传登记成功：201 + content_changed=True + status=pending（D3 登记即返回）。"""
    kb_id = _create_kb(client)
    r = _upload(client, kb_id)
    assert r.status_code == 201
    body = r.json()
    assert body["content_changed"] is True
    doc = body["document"]
    assert doc["title"] == "tcp.md"
    assert doc["status"] == "pending"
    assert doc["char_count"] == len("# TCP\n三次握手与四次挥手")
    assert doc["kb_id"] == kb_id


def test_upload_404_kb_not_found(client):
    """知识库不存在 → 404。"""
    assert _upload(client, 999999).status_code == 404


def test_upload_409_duplicate_title(client):
    """同库同名 → 409（UNIQUE(kb_id,title)，指引走重传）。"""
    kb_id = _create_kb(client)
    assert _upload(client, kb_id).status_code == 201
    assert _upload(client, kb_id).status_code == 409


def test_upload_422_unsupported_format(client):
    """非 .md/.txt → 422。"""
    kb_id = _create_kb(client)
    assert _upload(client, kb_id, ("a.pdf", b"%PDF-1.4", "application/pdf")).status_code == 422


def test_upload_400_empty_content(client):
    """空内容（仅空白）→ 400。"""
    kb_id = _create_kb(client)
    assert _upload(client, kb_id, ("empty.md", b"   \n  ", "text/markdown")).status_code == 400


def test_upload_413_too_large(client, monkeypatch):
    """超过上限 → 413（临时把上限调小以构造场景）。"""
    from app.core import config

    monkeypatch.setattr(config.settings, "max_upload_bytes", 10)
    kb_id = _create_kb(client)
    assert _upload(client, kb_id, ("big.md", b"x" * 50, "text/markdown")).status_code == 413


def test_list_documents_filter_by_status(client):
    """列表支持状态过滤：pending 命中、ready 不命中。"""
    kb_id = _create_kb(client)
    _upload(client, kb_id)
    assert len(client.get(f"/api/v1/kbs/{kb_id}/documents?status=pending").json()) == 1
    assert client.get(f"/api/v1/kbs/{kb_id}/documents?status=ready").json() == []
    assert client.get(f"/api/v1/kbs/{kb_id}/documents?status=bogus").status_code == 422


def test_get_content_matches_upload(client):
    """原文读取与上传内容一致（US-M1-03）。"""
    kb_id = _create_kb(client)
    doc_id = _upload(client, kb_id).json()["document"]["id"]
    r = client.get(f"/api/v1/documents/{doc_id}/content")
    assert r.status_code == 200
    assert r.json() == {"title": "tcp.md", "content": "# TCP\n三次握手与四次挥手"}


def test_reupload_same_content_idempotent(client):
    """重传相同内容 → content_changed=False，状态不变（幂等，US-M1-04）。"""
    kb_id = _create_kb(client)
    doc_id = _upload(client, kb_id).json()["document"]["id"]
    r = client.post(f"/api/v1/documents/{doc_id}/reupload", files={"file": MD})
    assert r.status_code == 200
    assert r.json()["content_changed"] is False


def test_reupload_changed_content_resets_status(client):
    """重传新内容 → content_changed=True，状态回 pending 等待重建。"""
    kb_id = _create_kb(client)
    doc_id = _upload(client, kb_id).json()["document"]["id"]
    new_file = ("tcp.md", "# TCP\n拥塞控制", "text/markdown")
    r = client.post(f"/api/v1/documents/{doc_id}/reupload", files={"file": new_file})
    assert r.status_code == 200
    body = r.json()
    assert body["content_changed"] is True
    assert body["document"]["status"] == "pending"
    assert body["document"]["char_count"] == len("# TCP\n拥塞控制")


def test_delete_document_204_then_404(client):
    """删除文档 204，随后详情与原文均 404。"""
    kb_id = _create_kb(client)
    doc_id = _upload(client, kb_id).json()["document"]["id"]
    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert client.get(f"/api/v1/documents/{doc_id}/content").status_code == 404
