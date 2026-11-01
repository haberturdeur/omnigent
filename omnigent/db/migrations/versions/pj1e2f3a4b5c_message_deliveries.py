"""Durable delivery receipts and revocable server queues."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "pj1e2f3a4b5c"
down_revision = "pi1d2e3f4a5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_deliveries",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("event", sa.LargeBinary(), nullable=False),
        sa.Column("actor", sa.String(768), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "delivery_id"),
    )
    op.create_index("ix_message_deliveries_status", "message_deliveries", ["status"])


def downgrade() -> None:
    op.drop_table("message_deliveries")
