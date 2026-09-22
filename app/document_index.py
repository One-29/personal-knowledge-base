"""文档切块与向量化的共享纯计算边界。"""

from __future__ import annotations

from collections.abc import Iterable

from app import chunking, embedding
from app.core.config import Settings, settings
from app.diagnostics import timed_stage
from app.embedding import EmbeddingError


def split_source(
    text: str,
    *,
    config: Settings = settings,
    protected_spans: Iterable[tuple[int, int]] = (),
) -> list[chunking.Chunk]:
    """按统一参数切分原文，上传入库与外部编辑同步共用。"""
    with timed_stage("index.chunking", characters=len(text)):
        return chunking.split_markdown(
            text,
            max_chars=config.chunk_max_chars,
            overlap_chars=config.chunk_overlap_chars,
            protected_spans=tuple(protected_spans),
        )


def embed_chunks(
    chunks: list[chunking.Chunk],
    *,
    config: Settings = settings,
) -> list[list[float]]:
    """向量化全部块，并拒绝数量或维度不完整的 provider 响应。"""
    with timed_stage(
        "index.embedding",
        chunks=len(chunks),
        dimension=config.embedding_dimension,
    ):
        vectors = embedding.get_embedding_provider(config).embed_texts(
            [chunk.text for chunk in chunks]
        )
        validate_vectors(
            vectors,
            expected_count=len(chunks),
            expected_dimension=config.embedding_dimension,
        )
        return vectors


def validate_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
    expected_dimension: int,
) -> None:
    if len(vectors) != expected_count:
        raise EmbeddingError(
            f"embedding 返回数量不匹配：期望 {expected_count}，实际 {len(vectors)}"
        )
    invalid = [
        index
        for index, vector in enumerate(vectors)
        if len(vector) != expected_dimension
    ]
    if invalid:
        raise EmbeddingError(
            f"embedding 维度不匹配：期望 {expected_dimension}，"
            f"异常位置 {invalid[:5]}"
        )
