"""build jobs and runs, item build status

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "build_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("issue_keys", sa.JSON(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=True),
        sa.Column("skipped", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_build_job_workspace_id"), "build_job", ["workspace_id"], unique=False)
    op.create_table(
        "build_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("cluster_id", sa.Integer(), nullable=True),
        sa.Column("patch_ids", sa.JSON(), nullable=False),
        sa.Column("tool", sa.String(), nullable=True),
        sa.Column("modules", sa.JSON(), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("log_path", sa.String(), nullable=True),
        sa.Column("inflight", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["build_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_build_run_job_id"), "build_run", ["job_id"], unique=False)
    with op.batch_alter_table("item") as batch_op:
        batch_op.add_column(sa.Column("build_status", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("build_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("built_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_item_build_status"), ["build_status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("item") as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_build_status"))
        batch_op.drop_column("built_at")
        batch_op.drop_column("build_reason")
        batch_op.drop_column("build_status")
    op.drop_table("build_run")
    op.drop_table("build_job")
