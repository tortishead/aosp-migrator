"""workspace

Revision ID: 0001
Revises:
Create Date: 2026-10-25 20:05:57.551681
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("jql", sa.Text(), nullable=False),
        sa.Column("target_tree", sa.String(), nullable=False),
        sa.Column("target_tree_type", sa.String(), nullable=False),
        sa.Column("source_tree", sa.String(), nullable=True),
        sa.Column("source_tree_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("workspace")
