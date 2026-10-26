"""dependency edge

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dependency_edge",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("from_item_id", sa.Integer(), nullable=False),
        sa.Column("to_item_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("copilot", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_item_id"], ["item.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_item_id", "to_item_id", "reason"),
    )
    op.create_index(op.f("ix_dependency_edge_workspace_id"), "dependency_edge", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_dependency_edge_from_item_id"), "dependency_edge", ["from_item_id"], unique=False)
    op.create_index(op.f("ix_dependency_edge_to_item_id"), "dependency_edge", ["to_item_id"], unique=False)


def downgrade() -> None:
    op.drop_table("dependency_edge")
