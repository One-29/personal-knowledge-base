"""Embedding 通道（04 §3 DR2）：OpenAI 兼容协议，供应商可配。

边界规则（04 DR2）：**全项目 embedding 模型唯一**——入库与查询必须用同一模型，
否则向量空间错位、相似度失去意义。维度由配置固定（默认 1536），换模型需一次迁移。

调用方式：httpx 直连 /embeddings（不引入额外 SDK，协议透明、依赖最少）。
"""

from typing import Protocol

import httpx

from .core.config import settings


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
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._timeout = timeout

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(
                f"embedding 服务返回 {exc.response.status_code}: {exc.response.text[:200]}"
            ) from None
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding 请求失败: {exc}") from None

        payload = response.json()
        items = sorted(payload["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]


def get_embedding_provider() -> EmbeddingProvider:
    """按配置构造 provider；未配置 key 时立刻失败（不静默降级）。"""
    if not settings.embedding_api_key:
        raise EmbeddingError("未配置 EMBEDDING_API_KEY（见 .env.example）")
    return OpenAICompatibleEmbedding(
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        timeout=settings.embedding_timeout_seconds,
    )
