"""迁移结果的稳定、无凭据报告格式。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class DatabaseDigest:
    counts: dict[str, int]
    table_sha256: dict[str, str]
    combined_sha256: str


@dataclass(frozen=True)
class FileAuditSummary:
    documents: int
    packages: int
    assets: int
    files: int
    bytes: int
    combined_sha256: str


@dataclass(frozen=True)
class EmbeddingProfileSummary:
    model: str
    dimension: int
    fingerprint: str


@dataclass(frozen=True)
class MigrationReport:
    format_version: int
    completed_at: str
    source_backend: str
    target_database: str
    backup_database: str | None
    database: DatabaseDigest
    files: FileAuditSummary
    embedding: EmbeddingProfileSummary

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write(self, path: Path) -> None:
        """在同目录原子写入报告，避免留下半截 JSON。"""
        destination = path.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    self.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
