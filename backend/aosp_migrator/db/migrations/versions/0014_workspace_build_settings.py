"""workspace build settings

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.add_column(sa.Column("envsetup_path", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("lunch_target", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("build_timeout", sa.Integer(), nullable=False, server_default="7200"))


def downgrade() -> None:
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.drop_column("build_timeout")
        batch_op.drop_column("lunch_target")
        batch_op.drop_column("envsetup_path")
