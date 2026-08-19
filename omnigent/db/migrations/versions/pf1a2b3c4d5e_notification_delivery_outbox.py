"""Add the durable notification delivery outbox.

Revision ID: pf1a2b3c4d5e
Revises: pe1f2a3b4c5d
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "pf1a2b3c4d5e"
down_revision: str | None = "pe1f2a3b4c5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("notification_id", sa.String(128), nullable=False),
        sa.Column("delivery_type", sa.String(32), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(128), nullable=False),
        sa.Column("auth", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("delivered_at", sa.Float(), nullable=True),
        sa.Column("cancelled_at", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "device_id",
            "notification_id",
            "delivery_type",
            name="uq_notification_deliveries_device_event_type",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        [
            "workspace_id",
            "delivered_at",
            "cancelled_at",
            "available_at",
            "lease_expires_at",
            "id",
        ],
    )
    op.create_table(
        "notification_intents",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("cancelled_at", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_notification_intents_due",
        "notification_intents",
        [
            "workspace_id",
            "completed_at",
            "cancelled_at",
            "available_at",
            "lease_expires_at",
            "id",
        ],
    )
    op.create_index(
        "ix_notification_intents_conversation",
        "notification_intents",
        [
            "workspace_id",
            "conversation_id",
            "completed_at",
            "cancelled_at",
            "id",
        ],
    )
    op.create_index(
        "ix_notification_deliveries_session",
        "notification_deliveries",
        [
            "workspace_id",
            "user_id",
            "session_id",
            "delivered_at",
            "cancelled_at",
            "id",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_intents_conversation", table_name="notification_intents")
    op.drop_index("ix_notification_intents_due", table_name="notification_intents")
    op.drop_table("notification_intents")
    op.drop_index(
        "ix_notification_deliveries_session",
        table_name="notification_deliveries",
    )
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
