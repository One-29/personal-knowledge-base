"""models.py —— 03 DDL 直译（M1：仅两表；任务 B：Document 收尾 + relationship）"""

from datetime import datetime

from sqlalchemy import BigInteger, Integer, DateTime, CheckConstraint, ForeignKey, Identity, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base  # Base 来自 db.py，绝不自己再定义


class KnowledgeBase(Base):
    """表 knowledge_bases：DDL 见 03 §3"""

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    
    # ── 关系空①（父侧）：一 对 多 → 子列表。back_populates 点名子侧属性名
    documents: Mapped[list["Document"]] = relationship(back_populates="kb")


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
