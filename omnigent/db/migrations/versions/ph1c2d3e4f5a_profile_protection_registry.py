"""Persist the private-profile isolation registry in the shared database.

Revision ID: ph1c2d3e4f5a
Revises: pg1b2c3d4e5f
Create Date: 2026-08-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ph1c2d3e4f5a"
down_revision: str | None = "pg1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_protection_registry",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def downgrade() -> None:
    op.drop_table("profile_protection_registry")
