"""workspace repo root

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.add_column(sa.Column("repo_root", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.drop_column("repo_root")
