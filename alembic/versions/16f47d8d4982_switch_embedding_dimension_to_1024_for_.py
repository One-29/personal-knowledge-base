"""switch embedding dimension to 1024 for bge-m3

Revision ID: 16f47d8d4982
Revises: 81e964106c94
Create Date: 2026-09-09 23:27:04.423156

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16f47d8d4982'
down_revision: Union[str, Sequence[str], None] = '81e964106c94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: 1536 → 1024 维（embedding 模型由 text-embedding-3-small 换为 BAAI/bge-m3）。

    04 DR2 的代价条款实操：换 embedding 模型必须全库重向量化 + 维度迁移。
    HNSW 索引依赖列维度，需先删索引再改列类型最后重建。
    """
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1024)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema: 回到 1536 维。"""
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1536)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
