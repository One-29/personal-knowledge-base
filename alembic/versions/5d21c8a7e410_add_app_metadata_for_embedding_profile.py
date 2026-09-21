"""add app metadata for embedding profile

Revision ID: 5d21c8a7e410
Revises: c2f89b1a0e71
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "5d21c8a7e410"
down_revision: str | Sequence[str] | None = "c2f89b1a0e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加应用级元数据表；指纹在首次 embedding 操作时采用并写入。"""
    op.create_table(
        "app_metadata",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_metadata")
