"""diff dependency

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diff_dependency",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("blob", sa.String(), nullable=False),
        sa.Column("depends_on_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_id"], ["attachment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_diff_dependency_attachment_id"), "diff_dependency", ["attachment_id"], unique=False)
    op.create_index(op.f("ix_diff_dependency_depends_on_id"), "diff_dependency", ["depends_on_id"], unique=False)
    op.create_index(op.f("ix_diff_dependency_workspace_id"), "diff_dependency", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_table("diff_dependency")
