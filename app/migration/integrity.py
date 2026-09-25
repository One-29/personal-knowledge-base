"""跨方言行值规范化、显式字段契约与确定性摘要。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.db import Base
from app.models import AppMetadata, Chunk, Document, IngestTask, KnowledgeBase

from .errors import IntegrityError
from .report import DatabaseDigest


@dataclass(frozen=True)
class TableSpec:
    name: str
    table: Any
    columns: tuple[str, ...]
    order_by: tuple[str, ...]


TABLE_SPECS = (
    TableSpec(
        "app_metadata",
        AppMetadata.__table__,
        ("key", "value"),
        ("key",),
    ),
    TableSpec(
        "knowledge_bases",
        KnowledgeBase.__table__,
        ("id", "name", "description", "created_at", "updated_at"),
        ("id",),
    ),
    TableSpec(
        "documents",
        Document.__table__,
        (
            "id",
            "kb_id",
            "title",
            "file_path",
            "content_hash",
            "ingest_version",
            "pending_file_path",
            "pending_content_hash",
            "pending_char_count",
            "status",
            "last_error_code",
            "last_error_message",
            "char_count",
            "chunk_count",
            "processed_at",
            "created_at",
            "updated_at",
        ),
        ("id",),
    ),
    TableSpec(
        "ingest_tasks",
        IngestTask.__table__,
        (
            "doc_id",
            "ingest_version",
            "candidate_path",
            "status",
            "stage",
            "attempt_count",
            "recovery_count",
            "last_error_code",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        ),
        ("doc_id",),
    ),
    TableSpec(
        "chunks",
        Chunk.__table__,
        (
            "id",
            "doc_id",
            "kb_id",
            "chunk_index",
            "content",
            "char_start",
            "char_end",
            "embedding",
            "created_at",
        ),
        ("id",),
    ),
)

SCHEMA_METADATA_KEY = "schema_version"


def validate_model_contract() -> None:
    """新增 ORM 列时强制同步迁移清单，避免静默漏字段。"""
    registered_tables = {spec.table.name for spec in TABLE_SPECS}
    model_tables = set(Base.metadata.tables)
    if registered_tables != model_tables:
        raise IntegrityError(
            "迁移表清单与 ORM 模型不一致："
            f"未登记={sorted(model_tables - registered_tables)}，"
            f"已删除={sorted(registered_tables - model_tables)}"
        )
    for spec in TABLE_SPECS:
        actual = tuple(column.name for column in spec.table.columns)
        if set(actual) != set(spec.columns):
            missing = sorted(set(actual) - set(spec.columns))
            stale = sorted(set(spec.columns) - set(actual))
            raise IntegrityError(
                f"迁移字段清单与 {spec.name} 模型不一致："
                f"未登记={missing}，已删除={stale}"
            )


def select_rows(connection: Connection, spec: TableSpec):
    columns = [spec.table.c[name] for name in spec.columns]
    statement = select(*columns).order_by(
        *(spec.table.c[name] for name in spec.order_by)
    )
    return connection.execution_options(stream_results=True).execute(statement)


def portable_row(
    spec: TableSpec,
    row: Mapping[str, Any],
    *,
    embedding_dimension: int,
) -> dict[str, Any] | None:
    """把 PostgreSQL 值转成 SQLite 可无损读取的 Python 值。"""
    if spec.name == "app_metadata" and row["key"] == SCHEMA_METADATA_KEY:
        return None

    result: dict[str, Any] = {}
    for name in spec.columns:
        value = row[name]
        if isinstance(value, datetime):
            result[name] = _portable_datetime(value)
        elif spec.name == "chunks" and name == "embedding":
            result[name] = _portable_embedding(value, embedding_dimension)
        else:
            result[name] = value
    return result


class DatabaseDigestBuilder:
    def __init__(self) -> None:
        self._hashes = {
            spec.name: hashlib.sha256() for spec in TABLE_SPECS
        }
        self._counts = {spec.name: 0 for spec in TABLE_SPECS}

    def add(self, table_name: str, row: Mapping[str, Any]) -> None:
        payload = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        ).encode("utf-8")
        self._hashes[table_name].update(payload)
        self._hashes[table_name].update(b"\n")
        self._counts[table_name] += 1

    def finish(self) -> DatabaseDigest:
        table_sha256 = {
            name: digest.hexdigest() for name, digest in self._hashes.items()
        }
        combined = hashlib.sha256()
        for spec in TABLE_SPECS:
            combined.update(spec.name.encode("ascii"))
            combined.update(b"\0")
            combined.update(str(self._counts[spec.name]).encode("ascii"))
            combined.update(b"\0")
            combined.update(table_sha256[spec.name].encode("ascii"))
            combined.update(b"\n")
        return DatabaseDigest(
            counts=dict(self._counts),
            table_sha256=table_sha256,
            combined_sha256=combined.hexdigest(),
        )


def digest_database(
    connection: Connection,
    *,
    embedding_dimension: int,
) -> DatabaseDigest:
    validate_model_contract()
    builder = DatabaseDigestBuilder()
    for spec in TABLE_SPECS:
        for row in select_rows(connection, spec).mappings():
            portable = portable_row(
                spec,
                row,
                embedding_dimension=embedding_dimension,
            )
            if portable is not None:
                builder.add(spec.name, portable)
    return builder.finish()


def _portable_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _portable_embedding(value: Any, expected_dimension: int) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise IntegrityError("块向量不是数值数组")
    embedding = [float(item) for item in value]
    if len(embedding) != expected_dimension:
        raise IntegrityError(
            f"块向量维度不一致：期望 {expected_dimension}，实际 {len(embedding)}"
        )
    if not all(math.isfinite(item) for item in embedding):
        raise IntegrityError("块向量包含 NaN 或无穷值")
    return embedding


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    raise TypeError(f"无法生成迁移摘要的值：{type(value).__name__}")
