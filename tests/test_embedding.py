"""embedding provider 单元测试（不打真实网络：monkeypatch httpx.post）。"""

import httpx
import pytest

from app.embedding import (
    EmbeddingError,
    OpenAICompatibleEmbedding,
    get_embedding_provider,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test/embeddings")
            raise httpx.HTTPStatusError(
                "error", request=request, response=httpx.Response(self.status_code, request=request)
            )

    def json(self) -> dict:
        return self._payload


def _provider() -> OpenAICompatibleEmbedding:
    return OpenAICompatibleEmbedding(
        api_key="test-key", base_url="https://example.test/v1", model="text-embedding-3-small"
    )


def test_empty_input_returns_empty_without_request(monkeypatch):
    """空输入不发请求（避免无意义网络调用）。"""
    called = False

    def _fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应发起请求")

    monkeypatch.setattr(httpx, "post", _fail)
    assert _provider().embed_texts([]) == []
    assert called is False


def test_parses_and_orders_embeddings(monkeypatch):
    """按 index 排序还原输入顺序，返回维度正确。"""
    payload = {
        "data": [
            {"index": 1, "embedding": [2.0, 2.0]},
            {"index": 0, "embedding": [1.0, 1.0]},
        ]
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(payload=payload))
    vectors = _provider().embed_texts(["第一段", "第二段"])
    assert vectors == [[1.0, 1.0], [2.0, 2.0]]


def test_http_error_raises_embedding_error(monkeypatch):
    """服务端 4xx/5xx → EmbeddingError（不吞错）。"""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(status_code=401))
    with pytest.raises(EmbeddingError):
        _provider().embed_texts(["x"])


def test_missing_api_key_fails_loud(monkeypatch):
    """未配置 key → 立刻报错，不静默降级（misconfiguration fails loud）。"""
    from app.core import config

    monkeypatch.setattr(config.settings, "embedding_api_key", None)
    with pytest.raises(EmbeddingError):
        get_embedding_provider()
