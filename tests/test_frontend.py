"""前端静态资源冒烟测试（M5）。

前端是纯静态单页（FastAPI 静态挂载），不依赖数据库——用独立 TestClient 验证，
避免把"页面能否打开"绑在数据库可用性上。
"""

from fastapi.testclient import TestClient

from app.main import app


def test_root_redirects_to_ui():
    """根路径跳转到前端单页。"""
    with TestClient(app) as client:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/ui/"


def test_ui_serves_index_page():
    """前端首页可访问，且包含关键挂载点与脚本引用。"""
    with TestClient(app) as client:
        resp = client.get("/ui/")
        assert resp.status_code == 200
        assert "KnowBase" in resp.text
        assert 'id="ask-form"' in resp.text       # 问答入口
        assert 'id="trace-form"' in resp.text     # 工作流入口
        assert 'id="margin"' in resp.text         # 页边注：核对原文
        assert 'id="docs-file"' in resp.text      # 文档上传
        assert "app.js" in resp.text


def test_ui_serves_assets():
    """静态资源（脚本与样式）可访问。"""
    with TestClient(app) as client:
        assert client.get("/ui/app.js").status_code == 200
        assert client.get("/ui/style.css").status_code == 200


def test_health_endpoint():
    """存活探针（CI/容器用）。"""
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
