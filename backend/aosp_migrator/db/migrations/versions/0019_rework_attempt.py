"""rework attempts, item rework status, patch attempt

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rework_attempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("heads", sa.JSON(), nullable=False),
        sa.Column("base_patch_ids", sa.JSON(), nullable=False),
        sa.Column("rungs", sa.JSON(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("calls", sa.JSON(), nullable=False),
        sa.Column("build_run_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rework_attempt_workspace_id"), "rework_attempt", ["workspace_id"], unique=False)
    with op.batch_alter_table("migration_patch") as batch_op:
        batch_op.add_column(sa.Column("attempt_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_migration_patch_attempt_id"), ["attempt_id"], unique=False)
        batch_op.create_foreign_key("fk_migration_patch_attempt_id_rework_attempt", "rework_attempt", ["attempt_id"], ["id"], ondelete="SET NULL")
    with op.batch_alter_table("item") as batch_op:
        batch_op.add_column(sa.Column("rework_status", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("rework_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reworked_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_item_rework_status"), ["rework_status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("item") as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_rework_status"))
        batch_op.drop_column("reworked_at")
        batch_op.drop_column("rework_reason")
        batch_op.drop_column("rework_status")
    with op.batch_alter_table("migration_patch") as batch_op:
        batch_op.drop_constraint("fk_migration_patch_attempt_id_rework_attempt", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_migration_patch_attempt_id"))
        batch_op.drop_column("attempt_id")
    op.drop_table("rework_attempt")
