"""diff scan

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diff_scan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("project", sa.String(), nullable=True),
        sa.Column("mapping_source", sa.String(), nullable=True),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("presence", sa.String(), nullable=True),
        sa.Column("presence_detail", sa.JSON(), nullable=True),
        sa.Column("target_head", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attachment_id"),
    )


def downgrade() -> None:
    op.drop_table("diff_scan")
