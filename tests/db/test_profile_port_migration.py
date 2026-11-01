"""Ordinary profiles retain legacy data and refuse implicit private-profile conversion."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config


@pytest.mark.parametrize("protected", [False, True])
def test_legacy_profile_upgrade(tmp_path: Path, protected: bool) -> None:
    uri = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = _build_alembic_config(uri)
    command.upgrade(config, "pj1e2f3a4b5c")
    engine = sa.create_engine(uri)
    profile_id = bytes.fromhex("12345678123456781234567812345678")
    with engine.begin() as connection:
        connection.execute(
            sa.text("""INSERT INTO profiles
            (workspace_id, id, name, user_id, owner_is_anonymous, owner_scope, is_default,
             default_slot, created_at, config, protection)
            VALUES (0, :id, 'Work', NULL, 1, '', 0, NULL, 1, :config, :protection)"""),
            {
                "id": profile_id,
                "config": '{"workspace":"/work"}',
                "protection": '{"lock":"passcode"}' if protected else None,
            },
        )
    try:
        if protected:
            with pytest.raises(RuntimeError, match="Convert protected profiles"):
                command.upgrade(config, "head")
        else:
            command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT id, name, config, protection FROM profiles")
            ).one()
            assert row.id == profile_id
            assert row.name == "Work"
            assert row.config == '{"workspace":"/work"}'
            assert row.protection == ('{"lock":"passcode"}' if protected else None)
    finally:
        engine.dispose()
