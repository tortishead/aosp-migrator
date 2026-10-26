"""item triage label

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("item") as batch_op:
        batch_op.add_column(sa.Column("triage_label", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("triage_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("triaged_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_item_triage_label"), ["triage_label"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("item") as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_triage_label"))
        batch_op.drop_column("triaged_at")
        batch_op.drop_column("triage_reason")
        batch_op.drop_column("triage_label")
