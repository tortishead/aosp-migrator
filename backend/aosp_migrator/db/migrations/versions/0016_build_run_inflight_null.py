"""build run inflight as SQL NULL

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Restored runs stored JSON null, which `inflight IS NOT NULL` still matched at startup.
    op.execute("UPDATE build_run SET inflight = NULL WHERE inflight = 'null'")


def downgrade() -> None:
    pass
