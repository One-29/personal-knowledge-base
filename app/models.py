"""models.py —— 03 DDL 直译（M1：仅两表；任务 B：Document 收尾 + relationship）"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.core.config import settings
from app.db import Base  # Base 来自 db.py，绝不自己再定义


class KnowledgeBase(Base):
    """表 knowledge_bases：DDL 见 03 §3"""

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    
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

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending"
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
        server_default="0"
    )

    # DDL: chunk_count   integer NOT NULL DEFAULT 0,
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0"
    )

    # DDL: processed_at  timestamptz,
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),  # timestamptz = 带时区的时间戳
    )

    # DDL: created_at    timestamptz NOT NULL DEFAULT now(),
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    # DDL: updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    # ── 关系空⑨（子侧）：指向父类。back_populates 点名父侧属性名（与空①成对）
    kb: Mapped["KnowledgeBase"] = relationship(back_populates="documents")

    # ── 关系（子侧）：切块随文档删除由数据库级联清理（passive_deletes）
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="doc", passive_deletes=True
    )


class Chunk(Base):
    """表 chunks：检索原子单元 + 溯源锚点（M2 产物，03 §3）。

    冗余 kb_id（DM3）：检索按库过滤免 join documents。
    embedding 维度与模型唯一性由 04 DR2 约束（默认 text-embedding-3-small / 1536）。
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(nullable=False)
    char_end: Mapped[int] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dimension), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    doc: Mapped["Document"] = relationship(back_populates="chunks")
