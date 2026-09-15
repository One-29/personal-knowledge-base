"""Embedding 通道（04 §3 DR2）：OpenAI 兼容协议，供应商可配。

边界规则（04 DR2）：**全项目 embedding 模型唯一**——入库与查询必须用同一模型，
否则向量空间错位、相似度失去意义。维度由配置固定（默认 1024），换模型需一次迁移。

调用方式：httpx 直连 /embeddings（不引入额外 SDK，协议透明、依赖最少）。
"""

import logging
import math
import time
from collections.abc import Callable
from numbers import Real
from typing import Protocol

import httpx

from .core.config import settings
from .http_client import get_http_client

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class EmbeddingError(RuntimeError):
    """embedding 调用失败（未配置 key / 网络错误 / 服务端错误）。"""


class EmbeddingProvider(Protocol):
    """向量化能力接口：实现可替换（云 API / 本地模型，见 04 DR2 演进条件）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长的向量列表（顺序与输入一致）。"""
        ...


class OpenAICompatibleEmbedding:
    """OpenAI 兼容 /embeddings 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 30.0,
        *,
        client: httpx.Client | None = None,
        batch_size: int = 32,
        max_retries: int = 2,
        retry_base_seconds: float = 0.5,
        dimension: int | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        if max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds 不能小于 0")
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._timeout = timeout
        self._client = client
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._dimension = dimension
        self._sleep = sleeper or time.sleep

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """请求一个批次；只重试限流、服务端错误与传输层故障。"""
        client = self._client if self._client is not None else get_http_client()
        for attempt in range(self._max_retries + 1):
            try:
                response = client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "input": texts},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.TransportError) and attempt < self._max_retries:
                    self._wait_before_retry(attempt, type(exc).__name__)
                    continue
                raise EmbeddingError(f"embedding 请求失败: {exc}") from None

            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < self._max_retries
            ):
                self._wait_before_retry(attempt, f"HTTP {response.status_code}")
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmbeddingError(
                    f"embedding 服务返回 {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ) from None
            return self._parse_response(response, expected_count=len(texts))

        raise AssertionError("embedding 重试循环未返回结果")

    def _wait_before_retry(self, attempt: int, reason: str) -> None:
        delay = self._retry_base_seconds * (2**attempt)
        logger.warning(
            "embedding 请求临时失败，%.2f 秒后重试（%s/%s）: %s",
            delay,
            attempt + 1,
            self._max_retries,
            reason,
        )
        self._sleep(delay)

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        expected_count: int,
    ) -> list[list[float]]:
        """把供应商响应转换为按输入顺序排列、可安全写入 pgvector 的向量。"""
        try:
            payload = response.json()
        except ValueError:
            raise EmbeddingError("embedding 服务返回的内容不是有效 JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingError("embedding 服务响应缺少 data 数组")

        items = payload["data"]
        if len(items) != expected_count:
            raise EmbeddingError(
                f"embedding 返回数量不匹配：期望 {expected_count}，实际 {len(items)}"
            )

        ordered: list[list[float] | None] = [None] * expected_count
        for item in items:
            if not isinstance(item, dict):
                raise EmbeddingError("embedding 服务响应包含无效条目")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < expected_count:
                raise EmbeddingError("embedding 服务响应包含越界 index")
            if ordered[index] is not None:
                raise EmbeddingError("embedding 服务响应包含重复 index")

            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list) or not raw_vector:
                raise EmbeddingError("embedding 服务响应包含无效向量")
            if self._dimension is not None and len(raw_vector) != self._dimension:
                raise EmbeddingError(
                    f"embedding 维度不匹配：期望 {self._dimension}，实际 {len(raw_vector)}"
                )

            vector: list[float] = []
            for value in raw_vector:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise EmbeddingError("embedding 服务响应包含非数值向量元素")
                number = float(value)
                if not math.isfinite(number):
                    raise EmbeddingError("embedding 服务响应包含非有限向量元素")
                vector.append(number)
            ordered[index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingError("embedding 服务响应缺少部分 index")
        return [vector for vector in ordered if vector is not None]


def get_embedding_provider() -> EmbeddingProvider:
    """按配置构造 provider；未配置 key 时立刻失败（不静默降级）。"""
    if not settings.embedding_api_key:
        raise EmbeddingError("未配置 EMBEDDING_API_KEY（见 .env.example）")
    return OpenAICompatibleEmbedding(
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        timeout=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
        max_retries=settings.embedding_max_retries,
        retry_base_seconds=settings.embedding_retry_base_seconds,
        dimension=settings.embedding_dimension,
    )
