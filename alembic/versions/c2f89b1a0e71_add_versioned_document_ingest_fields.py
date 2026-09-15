"""add versioned document ingest fields

Revision ID: c2f89b1a0e71
Revises: a3d0afb4b1d0
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c2f89b1a0e71"
down_revision: str | Sequence[str] | None = "a3d0afb4b1d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """记录当前候选原文及其单调递增的处理版本。"""
    op.add_column(
        "documents",
        sa.Column("ingest_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "documents", sa.Column("pending_file_path", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("pending_content_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("pending_char_count", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    """移除候选原文与处理版本字段。"""
    op.drop_column("documents", "pending_char_count")
    op.drop_column("documents", "pending_content_hash")
    op.drop_column("documents", "pending_file_path")
    op.drop_column("documents", "ingest_version")
