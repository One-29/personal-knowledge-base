"""滚动日志、请求关联与隐私安全诊断摘要回归。"""

from __future__ import annotations

import logging
from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.core.config import settings
from app.diagnostics.context import current_request_id
from app.diagnostics.local_logging import (
    configure_local_logging,
    flush_local_logging,
    shutdown_local_logging,
)
from app.diagnostics.stages import timed_stage
from app.models import KnowledgeBase


def test_diagnostics_report_correlates_request_without_knowledge_content(
    client,
    db,
    monkeypatch,
):
    private_name = "绝不能进入诊断报告的知识库名称"
    secret_key = "secret-test-key-42"
    secret_url = "https://user:password@private-provider.example/v1"
    db.add(KnowledgeBase(name=private_name, description="私人说明"))
    db.commit()
    monkeypatch.setattr(settings, "embedding_api_key", secret_key)
    monkeypatch.setattr(settings, "llm_api_key", secret_key)
    monkeypatch.setattr(settings, "embedding_base_url", secret_url)
    monkeypatch.setattr(settings, "llm_base_url", secret_url)

    response = client.get("/api/v1/diagnostics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    request_id = response.headers["x-request-id"]
    body = response.json()
    report = body["report"]
    assert re.fullmatch(r"[0-9a-f]{16}", request_id)
    assert body["request_id"] == request_id
    assert "knowledge_bases: 1" in report
    assert "ingest_tasks: queued:0,running:0,succeeded:0,failed:0,superseded:0" in report
    assert "database_backend: postgresql" in report
    assert private_name not in report
    assert "私人说明" not in report
    assert secret_key not in report
    assert secret_url not in report
    assert "private-provider.example" not in report
    assert "不含 API Key" in body["privacy_notice"]
    assert current_request_id() == "-"


def test_request_ids_are_unique_and_completion_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.diagnostics.request"):
        first = client.get("/health")
        second = client.get("/ready")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert "path=/health status=200 duration_ms=" in caplog.text
    assert "path=/ready status=200 duration_ms=" in caplog.text


def test_unhandled_error_returns_request_id_without_exception_detail(
    monkeypatch,
):
    from app import main

    def _fail_readiness() -> bool:
        raise RuntimeError("private database path and SQL must stay in local logs")

    monkeypatch.setattr(main, "database_is_ready", _fail_readiness)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        response = client.get("/ready")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "服务内部错误，请复制诊断信息后重试",
        "request_id": response.headers["x-request-id"],
    }
    assert "private database path" not in response.text


def test_local_log_rotates_and_redacts_credentials(tmp_path: Path):
    shutdown_local_logging()
    secret = "super-secret-value"
    try:
        path = configure_local_logging(
            tmp_path,
            secrets=(secret,),
            max_bytes=320,
            backup_count=2,
        )
        assert path is not None
        logger = logging.getLogger("app.diagnostics.redaction_test")
        for index in range(12):
            logger.info("rotation probe=%d payload=%s", index, "x" * 90)
        logger.error(
            "Authorization: Bearer %s basic=%s token=plain-token url=%s",
            secret,
            "Basic dXNlcjpwYXNzd29yZA==",
            "https://alice:password@example.test/v1",
        )
        flush_local_logging()

        files = sorted(path.parent.glob("knowbase.log*"))
        rendered = "\n".join(file.read_text(encoding="utf-8") for file in files)
        assert len(files) > 1
        assert secret not in rendered
        assert "dXNlcjpwYXNzd29yZA==" not in rendered
        assert "plain-token" not in rendered
        assert "alice:password" not in rendered
        assert "[REDACTED]" in rendered
        assert "request_id=-" in rendered
    finally:
        shutdown_local_logging()


def test_local_logging_preserves_host_logger_level(tmp_path: Path):
    shutdown_local_logging()
    host_logger = logging.getLogger("uvicorn.error")
    original_level = host_logger.level
    host_logger.setLevel(logging.ERROR)
    try:
        assert configure_local_logging(tmp_path) is not None
        assert host_logger.level == logging.ERROR
        shutdown_local_logging()
        assert host_logger.level == logging.ERROR
    finally:
        shutdown_local_logging()
        host_logger.setLevel(original_level)


def test_request_and_stage_logs_share_id_without_question_text(
    client,
    db,
    tmp_path: Path,
):
    shutdown_local_logging()
    private_question = "这是一段绝不能进入日志的私人问题正文"
    kb = KnowledgeBase(name="日志关联测试库")
    db.add(kb)
    db.commit()
    try:
        path = configure_local_logging(tmp_path)
        assert path is not None

        response = client.post(
            "/api/v1/ask",
            json={"question": private_question, "kb_id": kb.id},
        )
        assert response.status_code == 200
        request_id = response.headers["x-request-id"]
        flush_local_logging()
        rendered = path.read_text(encoding="utf-8")

        assert f"request_id={request_id}" in rendered
        assert "stage=ask.query_embedding" in rendered
        assert "stage=ask.retrieval" in rendered
        assert private_question not in rendered
    finally:
        shutdown_local_logging()


def test_timed_stage_records_only_dimensions_and_outcome(caplog):
    with caplog.at_level(logging.INFO, logger="app.diagnostics.stage"):
        with timed_stage("unit.probe", items=3, scope="all"):
            pass

    assert "stage=unit.probe outcome=ok duration_ms=" in caplog.text
    assert "items=3 scope=all" in caplog.text
