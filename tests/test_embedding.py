"""HTTP provider 与共享连接池单元测试（MockTransport，不访问真实网络）。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import http_client
from app.embedding import (
    EmbeddingError,
    OpenAICompatibleEmbedding,
    get_embedding_provider,
)
from app.generation import LLMError, OpenAICompatibleLLM
from app.main import app


def _provider(client: httpx.Client) -> OpenAICompatibleEmbedding:
    return OpenAICompatibleEmbedding(
        api_key="test-key", base_url="https://example.test/v1",
        model="text-embedding-3-small", client=client,
    )


def test_empty_input_returns_empty_without_request():
    """空输入不发请求（避免无意义网络调用）。"""
    def fail(request):
        raise AssertionError("不应发起请求")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        assert _provider(client).embed_texts([]) == []


def test_parses_and_orders_embeddings():
    """按 index 排序还原输入顺序，返回维度正确。"""
    def respond(request):
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["input"] == ["第一段", "第二段"]
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [2.0, 2.0]},
            {"index": 0, "embedding": [1.0, 1.0]},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert _provider(client).embed_texts(["第一段", "第二段"]) == [
            [1.0, 1.0], [2.0, 2.0],
        ]


@pytest.mark.parametrize("kind", ["embedding", "llm"])
@pytest.mark.parametrize("failure", ["status", "timeout"])
def test_provider_translates_http_failures(kind, failure):
    """复用 client 后，服务端错误和网络超时仍保留原领域异常契约。"""
    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("模拟超时", request=request)
        return httpx.Response(401, text="unauthorized")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        if kind == "embedding":
            with pytest.raises(EmbeddingError):
                _provider(client).embed_texts(["x"])
        else:
            provider = OpenAICompatibleLLM(
                "test-key", "https://example.test/v1", "test-model", client=client,
            )
            with pytest.raises(LLMError):
                provider.complete("system", "question")


def test_missing_api_key_fails_loud(monkeypatch):
    """未配置 key → 立刻报错，不静默降级。"""
    from app.core import config

    monkeypatch.setattr(config.settings, "embedding_api_key", None)
    with pytest.raises(EmbeddingError):
        get_embedding_provider()


def test_clients_shared_across_providers_and_recreated_after_lifespan(monkeypatch):
    """跨 provider 和线程复用一个池；重复 ASGI 生命周期均关闭、重建且可继续请求。"""
    http_client.close_http_client()
    requests = []
    created = []
    client_type = httpx.Client

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "回答 [1]"}}]})

    def create_client(**kwargs):
        client = client_type(transport=httpx.MockTransport(respond), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(http_client.httpx, "Client", create_client)
    embedding = OpenAICompatibleEmbedding("embed-key", "https://example.test/v1", "embed")
    llm = OpenAICompatibleLLM("llm-key", "https://example.test/v1", "chat", timeout=19)

    async def exercise():
        for cycle in range(2):
            async with app.router.lifespan_context(app):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    shared = list(executor.map(lambda _: http_client.get_http_client(), range(16)))
                assert all(client is created[cycle] for client in shared)
                assert len(created) == cycle + 1
                assert embedding.embed_texts(["原文"]) == [[1.0]]
                assert llm.complete("system", "question") == "回答 [1]"
            assert created[cycle].is_closed

    asyncio.run(exercise())
    assert len(requests) == 4
    assert requests[0].headers["Authorization"] == "Bearer embed-key"
    assert requests[1].headers["Authorization"] == "Bearer llm-key"
    assert requests[0].extensions["timeout"]["read"] == 30
    assert requests[1].extensions["timeout"]["read"] == 19
