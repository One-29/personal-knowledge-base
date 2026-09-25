"""add persistent ingest tasks

Revision ID: d8a4c1e7b902
Revises: 5d21c8a7e410
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d8a4c1e7b902"
down_revision: str | Sequence[str] | None = "5d21c8a7e410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """记录当前文档版本的队列状态，使中断任务可在下次启动恢复。"""
    op.create_table(
        "ingest_tasks",
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("ingest_version", sa.Integer(), nullable=False),
        sa.Column("candidate_path", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','superseded')"
        ),
        sa.CheckConstraint(
            "stage IN ('queued','validating','reading','chunking','embedding',"
            "'publishing','complete')"
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND stage = 'queued') OR "
            "(status = 'running' AND stage IN "
            "('validating','reading','chunking','embedding','publishing')) OR "
            "(status IN ('succeeded','failed','superseded') AND stage = 'complete')"
        ),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint("recovery_count >= 0"),
        sa.ForeignKeyConstraint(["doc_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("doc_id"),
    )
    op.create_index(
        "ix_ingest_tasks_status_updated",
        "ingest_tasks",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_tasks_status_updated", table_name="ingest_tasks")
    op.drop_table("ingest_tasks")
