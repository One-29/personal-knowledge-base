"""KnowBase 的可移植 ORM 数据模型。

PostgreSQL 使用 pgvector 与专用 HNSW/GIN 索引；SQLite 把向量保存为 JSON，
由本地检索后端做小规模余弦扫描，关键词索引由 schema 模块的 FTS5 管理。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.core.config import settings
from app.db import Base  # Base 来自 db.py，绝不自己再定义

# SQLite 只有类型名恰为 INTEGER 的主键才能自动分配 rowid。PostgreSQL 仍使用
# BIGINT，因此已有 Alembic schema 与容量上限均不改变。
PORTABLE_BIGINT = BigInteger().with_variant(Integer, "sqlite")
# 以 Vector 作为外层类型，保留 ``cosine_distance`` comparator；SQLite 方言
# 编译和绑定时改用 JSON。若把 JSON 放在外层，PostgreSQL 检索表达式会在 Python
# 侧丢失 pgvector comparator，即使数据库列本身仍编译成 vector。
PORTABLE_EMBEDDING = Vector(settings.embedding_dimension).with_variant(JSON(), "sqlite")


class AppMetadata(Base):
    """应用级键值元数据：schema/embedding 等跨业务表约束。"""

    __tablename__ = "app_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class KnowledgeBase(Base):
    """表 knowledge_bases：DDL 见 03 §3"""

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(PORTABLE_BIGINT, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ── 关系（父侧）：子表删除交给数据库 ON DELETE CASCADE（passive_deletes）
    documents: Mapped[list["Document"]] = relationship(
        back_populates="kb", passive_deletes=True
    )


class Document(Base):
    """表 documents：DDL 见 03 §3"""

    __tablename__ = "documents"

    __table_args__ = (
        UniqueConstraint("kb_id", "title"),
        CheckConstraint("status IN ('pending','processing','ready','failed')"),
    )

    id: Mapped[int] = mapped_column(PORTABLE_BIGINT, Identity(), primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        PORTABLE_BIGINT,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingest_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    pending_file_path: Mapped[str | None] = mapped_column(String(500))
    pending_content_hash: Mapped[str | None] = mapped_column(String(64))
    pending_char_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )

    # DDL: last_error_code    varchar(32),
    last_error_code: Mapped[str | None] = mapped_column(
        String(32),
    )

    # DDL: last_error_message text,
    last_error_message: Mapped[str | None] = mapped_column(
        Text,
    )

    # DDL: char_count    integer NOT NULL DEFAULT 0,
    char_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # DDL: chunk_count   integer NOT NULL DEFAULT 0,
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # DDL: processed_at  timestamptz,
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),  # timestamptz = 带时区的时间戳
    )

    # DDL: created_at    timestamptz NOT NULL DEFAULT now(),
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # DDL: updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── 关系（子侧）：指向父类，与父侧 back_populates 成对。
    kb: Mapped["KnowledgeBase"] = relationship(back_populates="documents")

    # ── 关系（子侧）：切块随文档删除由数据库级联清理（passive_deletes）
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="doc", passive_deletes=True
    )


class IngestTask(Base):
    """每篇文档当前入库版本的可恢复任务状态。"""

    __tablename__ = "ingest_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','superseded')"
        ),
        CheckConstraint(
            "stage IN ('queued','validating','reading','chunking','embedding',"
            "'publishing','complete')"
        ),
        CheckConstraint(
            "(status = 'queued' AND stage = 'queued') OR "
            "(status = 'running' AND stage IN "
            "('validating','reading','chunking','embedding','publishing')) OR "
            "(status IN ('succeeded','failed','superseded') AND stage = 'complete')"
        ),
        CheckConstraint("attempt_count >= 0"),
        CheckConstraint("recovery_count >= 0"),
        Index("ix_ingest_tasks_status_updated", "status", "updated_at"),
    )

    doc_id: Mapped[int] = mapped_column(
        PORTABLE_BIGINT,
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ingest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_path: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Chunk(Base):
    """表 chunks：检索原子单元 + 溯源锚点（M2 产物，03 §3）。

    冗余 kb_id（DM3）：检索按库过滤免 join documents。
    embedding 维度与模型唯一性由 04 DR2 约束（默认 BAAI/bge-m3 / 1024）。
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ).ddl_if(dialect="postgresql"),
        # 关键词通道（04 DR4）：pg_trgm 相似度查询的 GIN 索引，避免全表扫描
        Index(
            "ix_chunks_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[int] = mapped_column(PORTABLE_BIGINT, Identity(), primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        PORTABLE_BIGINT,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    kb_id: Mapped[int] = mapped_column(
        PORTABLE_BIGINT,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(nullable=False)
    char_end: Mapped[int] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        PORTABLE_EMBEDDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    doc: Mapped["Document"] = relationship(back_populates="chunks")
