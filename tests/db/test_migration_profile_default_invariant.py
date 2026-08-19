"""Upgrade and downgrade coverage for the profile default invariant."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_BEFORE_INVARIANT = "pc1d2e3f4a5b"


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'profiles.db'}")
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def _migrate(engine: Engine, revision: str) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if revision == _BEFORE_INVARIANT:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def _profile_values(
    profile_id: str,
    *,
    user_id: str | None,
    created_at: int,
    is_default: bool,
) -> dict[str, object]:
    return {
        "workspace_id": 0,
        "id": bytes.fromhex(profile_id),
        "name": profile_id,
        "user_id": user_id,
        "is_default": is_default,
        "created_at": created_at,
    }


def test_upgrade_reconciles_duplicate_defaults_and_enforces_one_per_owner(
    db_engine: Engine,
) -> None:
    _migrate(db_engine, _BEFORE_INVARIANT)
    before = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    rows = [
        _profile_values("10" * 16, user_id=None, created_at=100, is_default=True),
        _profile_values("20" * 16, user_id=None, created_at=200, is_default=True),
        _profile_values("30" * 16, user_id="alice@example.com", created_at=100, is_default=True),
        _profile_values("40" * 16, user_id="alice@example.com", created_at=100, is_default=True),
        _profile_values("50" * 16, user_id="alice@example.com", created_at=50, is_default=False),
    ]
    with db_engine.begin() as connection:
        connection.execute(before.insert(), rows)

    _migrate(db_engine, "head")

    after = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    column_types = {
        column["name"]: column["type"] for column in sa.inspect(db_engine).get_columns("profiles")
    }
    assert isinstance(column_types["config"], sa.LargeBinary)
    assert isinstance(column_types["protection"], sa.LargeBinary)
    with db_engine.connect() as connection:
        migrated = connection.execute(
            sa.select(
                after.c.id,
                after.c.owner_is_anonymous,
                after.c.owner_scope,
                after.c.is_default,
                after.c.default_slot,
            ).order_by(after.c.id)
        ).all()

    assert [
        (
            bytes(profile_id).hex(),
            bool(owner_is_anonymous),
            owner_scope,
            bool(is_default),
            default_slot,
        )
        for profile_id, owner_is_anonymous, owner_scope, is_default, default_slot in migrated
    ] == [
        ("10" * 16, True, "", True, 1),
        ("20" * 16, True, "", False, None),
        ("30" * 16, False, "alice@example.com", True, 1),
        ("40" * 16, False, "alice@example.com", False, None),
        ("50" * 16, False, "alice@example.com", False, None),
    ]

    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            after.insert().values(
                **_profile_values(
                    "60" * 16,
                    user_id=None,
                    created_at=300,
                    is_default=True,
                ),
                owner_is_anonymous=True,
                owner_scope="",
                default_slot=1,
            )
        )
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            after.insert().values(
                **_profile_values(
                    "75" * 16,
                    user_id="bob@example.com",
                    created_at=300,
                    is_default=True,
                ),
                owner_is_anonymous=False,
                owner_scope="someone-else@example.com",
                default_slot=1,
            )
        )
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            after.insert().values(
                **_profile_values(
                    "70" * 16,
                    user_id="alice@example.com",
                    created_at=300,
                    is_default=True,
                ),
                owner_is_anonymous=False,
                owner_scope="alice@example.com",
                default_slot=1,
            )
        )


@pytest.mark.parametrize("user_id", [None, "alice@example.com"])
def test_upgrade_promotes_oldest_profile_when_owner_has_no_default(
    db_engine: Engine,
    user_id: str | None,
) -> None:
    _migrate(db_engine, _BEFORE_INVARIANT)
    before = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    rows = [
        _profile_values("30" * 16, user_id=user_id, created_at=200, is_default=False),
        _profile_values("20" * 16, user_id=user_id, created_at=100, is_default=False),
        _profile_values("10" * 16, user_id=user_id, created_at=100, is_default=False),
    ]
    with db_engine.begin() as connection:
        connection.execute(before.insert(), rows)

    _migrate(db_engine, "head")

    after = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.connect() as connection:
        migrated = connection.execute(
            sa.select(after.c.id, after.c.is_default, after.c.default_slot).order_by(after.c.id)
        ).all()

    assert [
        (bytes(profile_id).hex(), bool(is_default), default_slot)
        for profile_id, is_default, default_slot in migrated
    ] == [
        ("10" * 16, True, 1),
        ("20" * 16, False, None),
        ("30" * 16, False, None),
    ]


def test_downgrade_removes_invariant_columns_without_losing_profiles(
    db_engine: Engine,
) -> None:
    profiles = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            profiles.insert().values(
                **_profile_values("80" * 16, user_id=None, created_at=100, is_default=True),
                owner_is_anonymous=True,
                owner_scope="",
                default_slot=1,
            )
        )

    _migrate(db_engine, _BEFORE_INVARIANT)

    inspector = sa.inspect(db_engine)
    assert {column["name"] for column in inspector.get_columns("profiles")}.isdisjoint(
        {"owner_is_anonymous", "owner_scope", "default_slot"}
    )
    assert "uq_profiles_workspace_owner_default" not in {
        constraint["name"] for constraint in inspector.get_unique_constraints("profiles")
    }
    downgraded = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.connect() as connection:
        row = connection.execute(sa.select(downgraded.c.id, downgraded.c.is_default)).one()
    assert bytes(row.id).hex() == "80" * 16
    assert bool(row.is_default) is True

    _migrate(db_engine, "head")
    upgraded = sa.Table("profiles", sa.MetaData(), autoload_with=db_engine)
    with db_engine.connect() as connection:
        row = connection.execute(
            sa.select(
                upgraded.c.owner_is_anonymous,
                upgraded.c.owner_scope,
                upgraded.c.default_slot,
            )
        ).one()
    assert row == (True, "", 1)
