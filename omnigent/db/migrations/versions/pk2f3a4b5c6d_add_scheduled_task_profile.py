"""Add the profile used by scheduled task sessions.

Revision ID: pk2f3a4b5c6d
Revises: pk1b2c3d4e5f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "pk2f3a4b5c6d"
down_revision: str | None = "pk1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("profile_id", Uuid16(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("profile_id")
