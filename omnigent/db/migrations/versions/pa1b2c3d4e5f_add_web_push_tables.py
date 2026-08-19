"""Add vendor-neutral Web Push subscriptions and VAPID configuration.

Revision ID: pa1b2c3d4e5f
Revises: ga1b2c3d4e5f
Revises: za2b3c4d5e6f
Create Date: 2026-08-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pa1b2c3d4e5f"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("endpoint_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("p256dh", sa.String(128), nullable=False),
        sa.Column("auth", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "device_id"),
    )
    op.create_index(
        "uq_push_subscriptions_endpoint",
        "push_subscriptions",
        ["workspace_id", "endpoint_hash"],
        unique=True,
    )
    op.create_index(
        "ix_push_subscriptions_user",
        "push_subscriptions",
        ["workspace_id", "user_id"],
    )
    op.create_table(
        "web_push_config",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("private_key", sa.String(128), nullable=False),
        sa.Column("public_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def downgrade() -> None:
    op.drop_table("web_push_config")
    op.drop_index("ix_push_subscriptions_user", table_name="push_subscriptions")
    op.drop_index("uq_push_subscriptions_endpoint", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
