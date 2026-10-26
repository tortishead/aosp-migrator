"""migration patch

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-26 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "migration_patch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=True),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("base_head", sa.String(), nullable=True),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_migration_patch_attachment_id"), "migration_patch", ["attachment_id"], unique=False)
    op.create_index(op.f("ix_migration_patch_item_id"), "migration_patch", ["item_id"], unique=False)


def downgrade() -> None:
    op.drop_table("migration_patch")
