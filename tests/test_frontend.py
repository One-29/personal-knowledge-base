"""前端静态资源与关键挂载点冒烟测试（M5）。

前端是纯静态单页（FastAPI 静态挂载），不依赖数据库——用独立 TestClient 验证，
避免把"页面能否打开"绑在数据库可用性上。
"""

import re

from fastapi.testclient import TestClient

from app.main import FRONTEND_DIR, app


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
        assert 'id="activity-ask"' in resp.text   # 问答独立运行状态
        assert 'id="activity-trace"' in resp.text # 工作流独立运行状态
        assert 'id="docs-status-filter"' in resp.text
        assert 'id="margin-open"' in resp.text
        assert "app.js" in resp.text


def test_ui_serves_assets():
    """静态资源（脚本与样式）可访问。"""
    with TestClient(app) as client:
        assert client.get("/ui/app.js").status_code == 200
        assert client.get("/ui/style.css").status_code == 200
        assert client.get("/ui/favicon.svg").status_code == 200


def test_image_package_ui_preserves_occurrence_order_and_uses_original_urls():
    """含图文档由后端字符位置驱动 DOM；不经 canvas 或客户端重编码。"""
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    script = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    style = (FRONTEND_DIR / "style.css").read_text(encoding="utf-8")
    assert 'accept=".md,.txt,.zip"' in html
    assert "function renderSourceWithImages" in script
    assert "Array.from(String(content" in script  # Unicode code point 与后端 Python 偏移一致
    assert 'element.src = sourceUrl' in script
    assert ".todataurl(" not in script.lower()
    assert ".drawimage(" not in script.lower()
    assert ".positioned-source .source-image img" in style


def test_javascript_mount_ids_exist_once_in_html():
    """脚本依赖的 DOM 挂载点必须存在且唯一，避免页面启动时空引用中断。"""
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    script = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8")
    html_ids = re.findall(r'\bid="([^"]+)"', html)
    script_ids = set(re.findall(r'getElementById\("([^"]+)"\)', script))

    assert len(html_ids) == len(set(html_ids)), "index.html 包含重复 id"
    assert script_ids <= set(html_ids), f"缺少前端挂载点：{sorted(script_ids - set(html_ids))}"


def test_health_endpoint():
    """存活探针（CI/容器用）。"""
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_readiness_endpoint_reports_database_state(monkeypatch):
    """就绪探针区分数据库可用与不可用，且不影响存活探针。"""
    from app import main

    with TestClient(app) as client:
        monkeypatch.setattr(main, "database_is_ready", lambda: True)
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}

        monkeypatch.setattr(main, "database_is_ready", lambda: False)
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "unavailable",
            "database": "unavailable",
        }
        assert client.get("/health").json() == {"status": "ok"}
