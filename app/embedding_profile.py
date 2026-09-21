"""embedding 模型指纹的持久化与兼容性守卫。

向量只有在同一供应端、模型和维度下才可比较。这里采用首次使用信任
（TOFU）：旧数据库第一次升级后记录当前配置；之后只允许空索引自动切换，
有现有块时必须先重建，避免把不同语义空间静默混在一起。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models import AppMetadata, Chunk

PROFILE_KEY = "embedding_profile_v1"
PROFILE_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


class EmbeddingProfileError(RuntimeError):
    """持久化的 embedding 指纹损坏或与当前配置不兼容。"""


class EmbeddingProfileMismatch(EmbeddingProfileError):
    """现有向量与当前 embedding 配置不属于同一语义空间。"""


@dataclass(frozen=True)
class EmbeddingProfile:
    base_url: str
    model: str
    dimension: int
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROFILE_SCHEMA_VERSION
            or not isinstance(self.base_url, str)
            or not self.base_url.strip()
            or not isinstance(self.model, str)
            or not self.model.strip()
            or type(self.dimension) is not int
            or self.dimension < 1
        ):
            raise ValueError("embedding 模型指纹字段无效")

    @classmethod
    def configured(cls, config: Settings = settings) -> "EmbeddingProfile":
        try:
            return cls(
                base_url=config.embedding_base_url.strip().rstrip("/"),
                model=config.embedding_model.strip(),
                dimension=config.embedding_dimension,
            )
        except ValueError as exc:
            raise EmbeddingProfileError("当前 embedding 模型配置无效。") from exc

    @classmethod
    def from_json(cls, value: str) -> "EmbeddingProfile":
        try:
            payload = json.loads(value)
            profile = cls(
                schema_version=payload["schema_version"],
                base_url=payload["base_url"],
                model=payload["model"],
                dimension=payload["dimension"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EmbeddingProfileError("数据库中的 embedding 模型指纹已损坏。") from exc
        return profile

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "base_url": self.base_url,
                "model": self.model,
                "dimension": self.dimension,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        # 日志与 API 错误不回显 base_url，避免用户误把凭据放进 URL 时泄漏。
        return f"{self.model} / {self.dimension} 维 / 指纹 {self.fingerprint[:12]}"


def ensure_embedding_profile(
    db: Session,
    current: EmbeddingProfile | None = None,
) -> EmbeddingProfile:
    """确认现有块与当前模型兼容，并在返回前结束本次数据库事务。

    空库可以安全采用新配置；有块的旧库首次升级时记录当前配置。后者是一次
    明确记录的 TOFU 边界，后续任何变化都会被拒绝。
    """
    configured = current or EmbeddingProfile.configured()
    try:
        row = db.get(AppMetadata, PROFILE_KEY)
        if row is not None:
            stored = EmbeddingProfile.from_json(row.value)
            if stored == configured:
                db.rollback()
                return stored

            chunk_count = _chunk_count(db)
            if chunk_count:
                db.rollback()
                raise EmbeddingProfileMismatch(
                    "当前 embedding 配置与已有向量不一致。"
                    f"已有：{stored.label}；当前：{configured.label}。"
                    "请改回原配置，或重建全部向量后再使用。"
                )
            row.value = configured.to_json()
            db.commit()
            logger.info("空索引采用新的 embedding 模型指纹: %s", configured.label)
            return configured

        existing_chunks = _chunk_count(db)
        db.add(AppMetadata(key=PROFILE_KEY, value=configured.to_json()))
        try:
            db.commit()
        except IntegrityError:
            # 并发首次访问只有一个会话能插入；输家回读胜者记录并重新校验。
            db.rollback()
            return ensure_embedding_profile(db, configured)
        if existing_chunks:
            logger.warning(
                "旧数据库首次记录 embedding 模型指纹（现有块=%d）: %s",
                existing_chunks,
                configured.label,
            )
        else:
            logger.info("记录 embedding 模型指纹: %s", configured.label)
        return configured
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


def stored_embedding_profile(db: Session) -> EmbeddingProfile | None:
    """读取已记录指纹并结束事务，供诊断与评估报告使用。"""
    try:
        row = db.get(AppMetadata, PROFILE_KEY)
        return EmbeddingProfile.from_json(row.value) if row is not None else None
    finally:
        db.rollback()


def _chunk_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Chunk)) or 0)
