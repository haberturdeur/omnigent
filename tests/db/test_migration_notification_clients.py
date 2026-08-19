"""Schema coverage for notification client and event indexes."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


def test_notification_indexes_end_with_primary_key(tmp_path: Path) -> None:
    """Every notification lookup index has its remaining PK column."""
    uri = f"sqlite:///{tmp_path / 'notification-clients.db'}"
    engine = get_or_create_engine(uri)
    try:
        inspector = sa.inspect(engine)
        client_indexes = {
            index["name"]: index for index in inspector.get_indexes("notification_clients")
        }
        event_indexes = {
            index["name"]: index for index in inspector.get_indexes("notification_events")
        }
        assert client_indexes["ix_notification_clients_user"]["column_names"][-1] == "device_id"
        assert event_indexes["ix_notification_events_session"]["column_names"][-1] == (
            "notification_id"
        )
    finally:
        engine.dispose()
        clear_engine_cache()
