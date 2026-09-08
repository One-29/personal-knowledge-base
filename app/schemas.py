from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class DocStatus(str, Enum):
    """文档处理状态（与 03 §3 DDL 的 CHECK 约束同源）。

    API 层用枚举做查询参数校验（非法值 → 422）；
    ORM 层仍存 str（DM6：数据库以 varchar + CHECK 为唯一约束）。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)


class KnowledgeBaseOut(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    doc_count: int = Field(default=0)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentContentOut(BaseModel):
    """原文响应：查看与溯源高亮用（US-M1-03、US-M3-03）。"""

    title: str
    content: str


class DocumentOut(BaseModel):
    """文档视图：列表/详情展示 + 状态机可见（不暴露 file_path/content_hash）。"""

    id: int
    kb_id: int
    title: str
    status: str                      # pending/processing/ready/failed
    char_count: int = 0
    chunk_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
