"""migration patch base and review

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_patch") as batch_op:
        batch_op.add_column(sa.Column("base_patch_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        # Existing patches are all `original`: accepted.
        batch_op.add_column(sa.Column("review", sa.String(), server_default="accepted", nullable=False))
        batch_op.add_column(sa.Column("review_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("findings", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("migration_patch") as batch_op:
        batch_op.drop_column("findings")
        batch_op.drop_column("review_note")
        batch_op.drop_column("review")
        batch_op.drop_column("base_patch_ids")
