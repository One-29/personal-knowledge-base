"""enable pg_trgm extension and gin index on chunks content

Revision ID: a3d0afb4b1d0
Revises: 16f47d8d4982
Create Date: 2026-09-10 12:51:53.417233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d0afb4b1d0'
down_revision: Union[str, Sequence[str], None] = '16f47d8d4982'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: 关键词通道（04 DR4）所需的 pg_trgm 扩展与 GIN 索引。

    顺序关键：先建扩展，才能创建 gin_trgm_ops 索引；扩展创建同样不被
    autogenerate 生成，必须手工补（迁移脚本自包含）。
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_chunks_content_trgm",
        "chunks",
        ["content"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chunks_content_trgm", table_name="chunks")
