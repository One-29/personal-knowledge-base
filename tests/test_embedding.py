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


def _provider(
    client: httpx.Client,
    **kwargs,
) -> OpenAICompatibleEmbedding:
    kwargs.setdefault("max_retries", 0)
    return OpenAICompatibleEmbedding(
        api_key="test-key", base_url="https://example.test/v1",
        model="text-embedding-3-small", client=client, **kwargs,
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


def test_large_input_is_batched_and_order_is_preserved():
    """大文档拆成固定批次，每批 index 独立排序后仍保持全局输入顺序。"""
    batches = []

    def respond(request):
        inputs = json.loads(request.content)["input"]
        batches.append(inputs)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(text.removeprefix("段"))]}
            for index, text in reversed(list(enumerate(inputs)))
        ]})

    texts = [f"段{index}" for index in range(5)]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vectors = _provider(client, batch_size=2).embed_texts(texts)

    assert batches == [["段0", "段1"], ["段2", "段3"], ["段4"]]
    assert vectors == [[0.0], [1.0], [2.0], [3.0], [4.0]]


def test_transient_failure_retries_only_current_batch():
    """中间批次限流时按退避重试该批次，已成功批次不重复计费。"""
    requests = []
    delays = []

    def respond(request):
        inputs = json.loads(request.content)["input"]
        requests.append(inputs)
        if inputs == ["c", "d"] and requests.count(inputs) == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(ord(text))]}
            for index, text in enumerate(inputs)
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        vectors = _provider(
            client,
            batch_size=2,
            max_retries=2,
            retry_base_seconds=0.25,
            sleeper=delays.append,
        ).embed_texts(["a", "b", "c", "d"])

    assert requests == [["a", "b"], ["c", "d"], ["c", "d"]]
    assert delays == [0.25]
    assert vectors == [[97.0], [98.0], [99.0], [100.0]]


@pytest.mark.parametrize(
    "kind,response",
    [
        ("embedding", httpx.Response(200, text="not-json")),
        ("embedding", httpx.Response(200, json={})),
        ("llm", httpx.Response(200, text="not-json")),
        ("llm", httpx.Response(200, json={})),
        ("llm", httpx.Response(200, json={"choices": [{"message": {}}]})),
    ],
)
def test_provider_translates_malformed_success_response(kind, response):
    """HTTP 200 也必须满足协议结构，异常响应统一转为领域错误。"""
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        if kind == "embedding":
            with pytest.raises(EmbeddingError):
                _provider(client).embed_texts(["x"])
        else:
            provider = OpenAICompatibleLLM(
                "test-key", "https://example.test/v1", "test-model", client=client,
            )
            with pytest.raises(LLMError):
                provider.complete("system", "question")


@pytest.mark.parametrize(
    "body",
    [
        b'{"data":[{"index":0,"embedding":[1.0,1e999]}]}',
        b'{"data":[{"index":0,"embedding":[1.0]}]}',
    ],
)
def test_embedding_rejects_invalid_or_wrong_dimension_vectors(body):
    """查询向量进入数据库前即拒绝非有限数值和维度不匹配。"""
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/json"},
            )
        )
    ) as client:
        with pytest.raises(EmbeddingError):
            _provider(client, dimension=2).embed_texts(["x"])


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


def test_llm_stream_parses_openai_sse_and_requests_streaming():
    """角色帧、注释和结束帧不混入正文，中文增量按原顺序返回。"""
    requests = []
    body = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        ': keep-alive\n\n'
        'data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"与第二段 [1]"}}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    def respond(request):
        assert request.headers["Accept"] == "text/event-stream"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleLLM(
            "test-key", "https://example.test/v1", "test-model", client=client,
        )
        assert list(provider.stream("system", "question")) == ["第一段", "与第二段 [1]"]

    assert requests == [{
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "question"},
        ],
        "temperature": 0,
        "stream": True,
    }]


def test_llm_stream_falls_back_to_single_json_completion():
    """部分 OpenAI 兼容服务忽略 stream=true 时仍能返回最终回答。"""
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "完整回答 [1]"}}]},
        ))
    ) as client:
        provider = OpenAICompatibleLLM(
            "test-key", "https://example.test/v1", "test-model", client=client,
        )
        assert list(provider.stream("system", "question")) == ["完整回答 [1]"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            content=b"data: not-json\n\n",
            headers={"content-type": "text/event-stream"},
        ),
        httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":7}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        ),
        httpx.Response(401, text="unauthorized"),
    ],
)
def test_llm_stream_translates_protocol_and_http_failures(response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        provider = OpenAICompatibleLLM(
            "test-key", "https://example.test/v1", "test-model", client=client,
        )
        with pytest.raises(LLMError):
            list(provider.stream("system", "question"))


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
