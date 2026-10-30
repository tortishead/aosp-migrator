"""rework jobs, attempt notes, build runs of rework attempts

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-30 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rework_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("issue_keys", sa.JSON(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("skipped", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rework_job_workspace_id"), "rework_job", ["workspace_id"], unique=False)
    with op.batch_alter_table("rework_attempt") as batch_op:
        batch_op.add_column(sa.Column("notes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch_op.create_index(batch_op.f("ix_rework_attempt_job_id"), ["job_id"], unique=False)
        batch_op.create_foreign_key("fk_rework_attempt_job_id_rework_job", "rework_job", ["job_id"], ["id"], ondelete="SET NULL")
    with op.batch_alter_table("build_run") as batch_op:
        batch_op.alter_column("job_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("attempt_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_build_run_attempt_id"), ["attempt_id"], unique=False)
        batch_op.create_foreign_key("fk_build_run_attempt_id_rework_attempt", "rework_attempt", ["attempt_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    with op.batch_alter_table("build_run") as batch_op:
        batch_op.drop_constraint("fk_build_run_attempt_id_rework_attempt", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_build_run_attempt_id"))
        batch_op.drop_column("attempt_id")
        batch_op.alter_column("job_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("rework_attempt") as batch_op:
        batch_op.drop_constraint("fk_rework_attempt_job_id_rework_job", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_rework_attempt_job_id"))
        batch_op.drop_column("notes")
    op.drop_table("rework_job")
