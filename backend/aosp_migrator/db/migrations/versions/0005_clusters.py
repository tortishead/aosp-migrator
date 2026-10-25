"""clusters

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 23:19:32.905435
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cluster",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cluster", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cluster_workspace_id"), ["workspace_id"], unique=False)

    op.create_table(
        "cluster_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id"),
    )
    with op.batch_alter_table("cluster_member", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cluster_member_cluster_id"), ["cluster_id"], unique=False)

    op.create_table(
        "cluster_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cluster_note", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cluster_note_cluster_id"), ["cluster_id"], unique=False)

    op.create_table(
        "cluster_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cluster_event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cluster_event_cluster_id"), ["cluster_id"], unique=False)


def downgrade() -> None:
    op.drop_table("cluster_event")
    op.drop_table("cluster_note")
    op.drop_table("cluster_member")
    op.drop_table("cluster")
