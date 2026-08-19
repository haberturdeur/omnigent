"""Add server-coordinated notification client activity.

Revision ID: pc1d2e3f4a5b
Revises: pb1c2d3e4f5a
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pc1d2e3f4a5b"
down_revision: str | None = "pb1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_clients",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("foreground", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_active_at", sa.Integer(), nullable=False),
        sa.Column("mobile_delay_seconds", sa.Integer(), server_default="60", nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "device_id"),
    )
    op.create_index(
        "ix_notification_clients_user",
        "notification_clients",
        ["workspace_id", "user_id", "last_active_at", "device_id"],
    )
    op.create_table(
        "notification_events",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("notification_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "notification_id"),
    )
    op.create_index(
        "ix_notification_events_session",
        "notification_events",
        [
            "workspace_id",
            "user_id",
            "session_id",
            "acknowledged_at",
            "notification_id",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_events_session", table_name="notification_events")
    op.drop_table("notification_events")
    op.drop_index("ix_notification_clients_user", table_name="notification_clients")
    op.drop_table("notification_clients")
