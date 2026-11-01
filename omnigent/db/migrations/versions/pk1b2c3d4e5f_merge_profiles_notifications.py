"""Merge ordinary profiles and notifications with upstream history.

Legacy revisions remain readable so existing installations keep their data.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import decode

revision = "pk1b2c3d4e5f"
down_revision = ("d0a1f879a01d", "pj1e2f3a4b5c")
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for (raw,) in bind.execute(
        sa.text("SELECT protection FROM profiles WHERE protection IS NOT NULL")
    ):
        protection = json.loads(decode(raw) or "{}")
        if protection.get("lock") in {"passcode", "device"}:
            raise RuntimeError(
                "Convert protected profiles to ordinary profiles in the old application "
                "before upgrading."
            )
    inspector = sa.inspect(bind)
    # Upstream project writes no longer maintain these redundant owner columns.
    names = {c["name"] for c in inspector.get_columns("projects")}
    with op.batch_alter_table("projects") as batch:
        for constraint in inspector.get_unique_constraints("projects"):
            name = constraint["name"]
            if name is not None and name.startswith("uq_projects_workspace_owner"):
                batch.drop_constraint(name, type_="unique")
        for constraint in inspector.get_check_constraints("projects"):
            if constraint["name"] in {"ck_projects_owner_scope", "ck_projects_profile_slot"}:
                batch.drop_constraint(constraint["name"], type_="check")
        for name in ("owner_is_anonymous", "owner_scope", "profile_unassigned_slot"):
            if name in names:
                batch.drop_column(name)


def downgrade() -> None:
    raise RuntimeError("Restore a database backup to return to the private-profile release.")
