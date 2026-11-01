"""Persist native input metadata across coordinator restarts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pi1d2e3f4a5b"
down_revision = "ph1c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("pending_inputs", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "pending_inputs")
