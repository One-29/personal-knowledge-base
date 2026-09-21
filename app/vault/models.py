"""文件系统真相清单的严格、可版本化数据契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

VAULT_FORMAT_VERSION = 1


class VaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRecord(VaultModel):
    path: StrictStr = Field(min_length=1, max_length=500)
    content_hash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    char_count: StrictInt = Field(ge=0)
    size: StrictInt = Field(ge=0)
    mtime_ns: StrictInt = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or ":" in value
            or "\x00" in value
            or value != path.as_posix()
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("source path must be canonical and relative")
        return value


class KnowledgeBaseRecord(VaultModel):
    id: StrictInt = Field(gt=0)
    name: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_timestamp_type(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("timestamp must be an ISO string or datetime")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class DocumentRecord(VaultModel):
    id: StrictInt = Field(gt=0)
    kb_id: StrictInt = Field(gt=0)
    title: StrictStr = Field(min_length=1, max_length=255)
    ingest_version: StrictInt = Field(ge=1)
    active: SourceRecord
    pending: SourceRecord | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_timestamp_type(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("timestamp must be an ISO string or datetime")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class VaultSnapshot(VaultModel):
    knowledge_bases: tuple[KnowledgeBaseRecord, ...] = ()
    documents: tuple[DocumentRecord, ...] = ()

    @model_validator(mode="after")
    def validate_relations_and_uniqueness(self) -> "VaultSnapshot":
        kb_ids = [item.id for item in self.knowledge_bases]
        kb_names = [item.name for item in self.knowledge_bases]
        doc_ids = [item.id for item in self.documents]
        doc_titles = [(item.kb_id, item.title) for item in self.documents]
        if kb_ids != sorted(kb_ids) or doc_ids != sorted(doc_ids):
            raise ValueError("vault records must be ordered by id")
        if len(kb_ids) != len(set(kb_ids)) or len(kb_names) != len(set(kb_names)):
            raise ValueError("knowledge base ids and names must be unique")
        if len(doc_ids) != len(set(doc_ids)) or len(doc_titles) != len(set(doc_titles)):
            raise ValueError("document ids and titles must be unique")
        known_kbs = set(kb_ids)
        if any(item.kb_id not in known_kbs for item in self.documents):
            raise ValueError("document references an unknown knowledge base")

        source_owners: dict[str, int] = {}
        for document in self.documents:
            if (
                document.pending is not None
                and document.pending.path == document.active.path
                and document.pending != document.active
            ):
                raise ValueError("one source path has conflicting metadata")
            for source in (document.active, document.pending):
                if source is None:
                    continue
                owner = source_owners.setdefault(source.path, document.id)
                if owner != document.id:
                    raise ValueError("one source path belongs to multiple documents")
        return self

    def with_knowledge_base(self, record: KnowledgeBaseRecord) -> "VaultSnapshot":
        items = {item.id: item for item in self.knowledge_bases}
        items[record.id] = record
        return VaultSnapshot(
            knowledge_bases=tuple(items[key] for key in sorted(items)),
            documents=self.documents,
        )

    def without_knowledge_base(self, kb_id: int) -> "VaultSnapshot":
        return VaultSnapshot(
            knowledge_bases=tuple(
                item for item in self.knowledge_bases if item.id != kb_id
            ),
            documents=tuple(item for item in self.documents if item.kb_id != kb_id),
        )

    def with_document(self, record: DocumentRecord) -> "VaultSnapshot":
        if record.kb_id not in {item.id for item in self.knowledge_bases}:
            raise ValueError("document knowledge base is absent from the vault")
        items = {item.id: item for item in self.documents}
        items[record.id] = record
        return VaultSnapshot(
            knowledge_bases=self.knowledge_bases,
            documents=tuple(items[key] for key in sorted(items)),
        )

    def without_document(self, doc_id: int) -> "VaultSnapshot":
        return VaultSnapshot(
            knowledge_bases=self.knowledge_bases,
            documents=tuple(item for item in self.documents if item.id != doc_id),
        )

    def with_pending_sources_promoted(self) -> "VaultSnapshot":
        """把崩溃前已登记的候选原文提升为重建后的活动版本。"""
        return VaultSnapshot(
            knowledge_bases=self.knowledge_bases,
            documents=tuple(
                item.model_copy(update={"active": item.pending, "pending": None})
                if item.pending is not None
                else item
                for item in self.documents
            ),
        )
