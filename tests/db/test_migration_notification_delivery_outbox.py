"""Upgrade and downgrade coverage for the notification delivery outbox."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine


def test_notification_delivery_outbox_migration_round_trip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'notification-outbox.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "pe1f2a3b4c5d")
        assert "notification_deliveries" not in sa.inspect(engine).get_table_names()

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "pf1a2b3c4d5e")

        inspector = sa.inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("notification_deliveries")}
        assert columns == {
            "workspace_id",
            "id",
            "user_id",
            "device_id",
            "notification_id",
            "delivery_type",
            "session_id",
            "endpoint",
            "p256dh",
            "auth",
            "payload",
            "available_at",
            "lease_token",
            "lease_expires_at",
            "attempts",
            "created_at",
            "delivered_at",
            "cancelled_at",
            "last_error",
        }
        delivery_indexes = {
            index["name"]: index for index in inspector.get_indexes("notification_deliveries")
        }
        assert set(delivery_indexes) == {
            "ix_notification_deliveries_due",
            "ix_notification_deliveries_session",
        }
        assert delivery_indexes["ix_notification_deliveries_due"]["column_names"][-1] == "id"
        assert delivery_indexes["ix_notification_deliveries_session"]["column_names"][-1] == "id"
        assert "notification_intents" in inspector.get_table_names()
        intent_columns = {
            column["name"] for column in inspector.get_columns("notification_intents")
        }
        assert intent_columns == {
            "workspace_id",
            "id",
            "conversation_id",
            "payload",
            "available_at",
            "lease_token",
            "lease_expires_at",
            "attempts",
            "created_at",
            "completed_at",
            "cancelled_at",
            "last_error",
        }
        intent_indexes = {
            index["name"]: index for index in inspector.get_indexes("notification_intents")
        }
        assert intent_indexes["ix_notification_intents_due"]["column_names"][-1] == "id"
        assert intent_indexes["ix_notification_intents_conversation"]["column_names"][-1] == "id"
        unique_constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("notification_deliveries")
        }
        assert "uq_notification_deliveries_device_event_type" in unique_constraints
    finally:
        engine.dispose()
        clear_engine_cache()
