"""Persistence tests for the vendor-neutral Web Push backend."""

from __future__ import annotations

import base64
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from omnigent.db.db_models import (
    SqlNotificationDelivery,
    SqlNotificationEvent,
    SqlNotificationIntent,
    workspace_scope,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.push_notifications import (
    PushNotificationDispatcher,
    PushSubscription,
    PushSubscriptionStore,
    VapidKeys,
)
from omnigent.server.routes.push_notifications import create_push_notifications_router


def _registered_store(tmp_path, name: str = "outbox.db") -> PushSubscriptionStore:
    store = PushSubscriptionStore(f"sqlite:///{tmp_path / name}")
    store.upsert(
        user_id=RESERVED_USER_LOCAL,
        device_id="phone",
        endpoint="https://push.example.test/message/one",
        p256dh="p" * 87,
        auth="a" * 22,
    )
    return store


def _enqueue_test_delivery(
    store: PushSubscriptionStore,
    *,
    notification_id: str = "event:session-1:one",
    available_at: float | None = None,
) -> None:
    payload = json.dumps(
        {
            "version": 1,
            "type": "session.completed",
            "session_id": "session-1",
            "notification_id": notification_id,
            "title": "Build",
        },
        separators=(",", ":"),
    )
    store.enqueue_notification(
        user_id=RESERVED_USER_LOCAL,
        notification_id=notification_id,
        conversation_id="session-1",
        payload=payload,
        subscriptions=store.list_for_users({RESERVED_USER_LOCAL}),
        available_at=time.time() if available_at is None else available_at,
    )


def test_subscription_round_trip_and_vapid_key_stability(tmp_path) -> None:
    store = PushSubscriptionStore(f"sqlite:///{tmp_path / 'push.db'}")

    first_keys = store.get_or_create_vapid_keys()
    second_keys = store.get_or_create_vapid_keys()
    assert first_keys == second_keys
    assert len(first_keys.public_key) == 87

    store.upsert(
        user_id="alice",
        device_id="phone-1",
        endpoint="https://push.example.test/message/one",
        p256dh="p" * 87,
        auth="a" * 22,
    )
    subscriptions = store.list_for_users({"alice"})
    assert len(subscriptions) == 1
    subscription = subscriptions[0]
    assert subscription.device_id == "phone-1"
    assert subscription.endpoint.endswith("/one")

    store.upsert(
        user_id="alice",
        device_id="phone-1",
        endpoint="https://push.example.test/message/two",
        p256dh="q" * 87,
        auth="b" * 22,
    )
    updated = store.list_for_users({"alice"})
    assert len(updated) == 1
    assert updated[0].endpoint.endswith("/two")
    assert store.delete(user_id="alice", device_id="phone-1")
    assert store.list_for_users({"alice"}) == []

    store.record_client_activity(
        user_id="alice",
        device_id="desktop-1",
        platform="web",
        foreground=True,
        active=True,
        mobile_delay_seconds=60,
    )
    assert store.mobile_push_delay_for_user("alice") > 0
    store.record_client_activity(
        user_id="alice",
        device_id="desktop-1",
        platform="web",
        foreground=False,
        active=False,
        mobile_delay_seconds=60,
    )
    assert store.mobile_push_delay_for_user("alice") == 0

    store.record_notification(
        user_id="alice",
        notification_id="event:session-1:one",
        conversation_id="session-1",
    )
    assert store.acknowledge_notifications(user_id="alice", conversation_id="session-1") == [
        "event:session-1:one"
    ]
    assert store.acknowledge_notifications(user_id="alice", conversation_id="session-1") == []


def test_registration_api_uses_local_identity_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.push_notifications.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class Store:
        def __init__(self) -> None:
            self.upserted: dict[str, str] = {}
            self.deleted: tuple[str, str] | None = None

        def get_or_create_vapid_keys(self) -> VapidKeys:
            return VapidKeys("private", "public")

        def upsert(self, **values: str) -> None:
            self.upserted = values

        def delete(self, *, user_id: str, device_id: str) -> bool:
            self.deleted = (user_id, device_id)
            return True

        def record_client_activity(self, **values: object) -> None:
            self.activity = values

    store = Store()
    app = FastAPI()
    app.include_router(
        create_push_notifications_router(cast(Any, store), auth_provider=None),
        prefix="/v1",
    )
    client = TestClient(app)

    assert client.get("/v1/push/config").json() == {"vapid_public_key": "public"}
    response = client.put(
        "/v1/push/subscriptions/phone-1",
        json={
            "endpoint": "https://push.example.test/message/one",
            "keys": {
                "p256dh": base64.urlsafe_b64encode(b"\x04" + b"p" * 64).rstrip(b"=").decode(),
                "auth": base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode(),
            },
        },
    )
    assert response.status_code == 204
    assert store.upserted["user_id"] == RESERVED_USER_LOCAL
    assert store.upserted["device_id"] == "phone-1"
    assert client.delete("/v1/push/subscriptions/phone-1").status_code == 204
    assert store.deleted == (RESERVED_USER_LOCAL, "phone-1")
    assert (
        client.post(
            "/v1/push/activity/desktop-1",
            json={"platform": "web", "foreground": True, "active": True},
        ).status_code
        == 204
    )
    assert store.activity["user_id"] == RESERVED_USER_LOCAL
    assert store.activity["mobile_delay_seconds"] == 60

    invalid = client.put(
        "/v1/push/subscriptions/phone-1",
        json={
            "endpoint": "http://push.example.test/message/one",
            "keys": {"p256dh": "not-a-key", "auth": "not-a-secret"},
        },
    )
    assert invalid.status_code == 422

    monkeypatch.setattr(
        "omnigent.server.push_notifications.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    private_endpoint = client.put(
        "/v1/push/subscriptions/phone-1",
        json={
            "endpoint": "https://push.example.test/message/one",
            "keys": {
                "p256dh": base64.urlsafe_b64encode(b"\x04" + b"p" * 64).rstrip(b"=").decode(),
                "auth": base64.urlsafe_b64encode(b"a" * 16).rstrip(b"=").decode(),
            },
        },
    )
    assert private_endpoint.status_code == 422
    assert "public IP" in private_endpoint.json()["detail"]


def test_dispatcher_sends_actionable_event_and_ignores_initial_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_WEBPUSH_VAPID_SUBJECT", raising=False)
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    delivered = threading.Event()
    payloads: list[dict[str, object]] = []

    class Store:
        def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
            assert user_ids == {RESERVED_USER_LOCAL}
            return [PushSubscription(RESERVED_USER_LOCAL, "phone", "https://push", "p", "a")]

        def get_or_create_vapid_keys(self) -> VapidKeys:
            return VapidKeys("private", "public")

        def delete_endpoint(self, endpoint: str) -> None:
            raise AssertionError(f"unexpected deletion: {endpoint}")

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build Android app", parent_conversation_id=None)

    def send(**kwargs: object) -> None:
        assert kwargs["vapid_private_key"] == "private"
        assert kwargs["vapid_claims"] == {"sub": "mailto:notifications@omnigent.ai"}
        assert kwargs["subscription_info"] == {
            "endpoint": "https://push",
            "keys": {"p256dh": "p", "auth": "a"},
        }
        payloads.append(json.loads(str(kwargs["data"])))
        delivered.set()

    dispatcher = PushNotificationDispatcher(
        store=cast(Any, Store()),
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        settle_seconds=0.01,
        send=send,
    )
    try:
        dispatcher.observe("session-1", {"type": "session.status", "status": "idle"})
        assert not delivered.wait(0.05)

        dispatcher.observe("session-1", {"type": "session.status", "status": "running"})
        dispatcher.observe("session-1", {"type": "session.status", "status": "idle"})
        assert delivered.wait(1)
        assert str(payloads[0]["notification_id"]).startswith("event:session-1:")
        assert {key: value for key, value in payloads[0].items() if key != "notification_id"} == {
            "version": 1,
            "type": "session.completed",
            "session_id": "session-1",
            "title": "Build Android app",
        }
        delivered.clear()
        dispatcher.observe("session-1", {"type": "session.status", "status": "idle"})
        assert not delivered.wait(0.05)

        dispatcher.observe(
            "session-1",
            {
                "type": "response.elicitation_request",
                "elicitation_id": "ask-1",
                "params": {
                    "message": "Codex needs approval",
                    "content_preview": "Codex wants to run **git status**",
                    "requestedSchema": None,
                    "target_session_id": "session-child",
                    "remember_scope": {"tool": "Bash"},
                },
            },
        )
        assert delivered.wait(1)
        assert payloads[-1]["type"] == "session.needs_input"
        assert payloads[-1]["notification_id"] == "approval:ask-1"
        assert payloads[-1]["approval"] == {
            "elicitation_id": "ask-1",
            "session_id": "session-child",
            "description": "Codex wants to run git status",
            "persistent": "remember",
        }

        delivered.clear()
        dispatcher.observe(
            "session-1",
            {"type": "response.elicitation_resolved", "elicitation_id": "ask-1"},
        )
        assert delivered.wait(1)
        assert payloads[-1] == {
            "version": 1,
            "type": "notification.dismissed",
            "session_id": "session-1",
            "notification_id": "approval:ask-1",
        }
    finally:
        dispatcher.close()


def test_dispatcher_suppresses_child_agent_conversations() -> None:
    class Store:
        def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
            raise AssertionError(f"child conversation unexpectedly had recipients: {user_ids}")

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(
                title="Background agent",
                parent_conversation_id="parent-session",
            )

    dispatcher = PushNotificationDispatcher(
        store=cast(Any, Store()),
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: pytest.fail(f"unexpected push: {kwargs}"),
    )
    try:
        dispatcher._deliver("child-session", "session.needs_input")
    finally:
        dispatcher.close()


def test_dispatcher_delays_mobile_until_desktop_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    delivered = threading.Event()

    class Store:
        checks = 0

        def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
            return [PushSubscription(RESERVED_USER_LOCAL, "phone", "https://push", "p", "a")]

        def mobile_push_delay_for_user(self, user_id: str) -> float:
            self.checks += 1
            return 0.05 if self.checks == 1 else 0

        def get_or_create_vapid_keys(self) -> VapidKeys:
            return VapidKeys("private", "public")

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build", parent_conversation_id=None)

    dispatcher = PushNotificationDispatcher(
        store=cast(Any, Store()),
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: delivered.set(),
    )
    try:
        dispatcher._deliver("session-1", "session.completed")
        assert not delivered.wait(0.02)
        assert delivered.wait(1)
    finally:
        dispatcher.close()


def test_acknowledgement_cancels_delayed_mobile_delivery() -> None:
    delivered = threading.Event()

    class Store:
        def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
            return [PushSubscription(RESERVED_USER_LOCAL, "phone", "https://push", "p", "a")]

        def mobile_push_delay_for_user(self, user_id: str) -> float:
            return 0.2

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build", parent_conversation_id=None)

    dispatcher = PushNotificationDispatcher(
        store=cast(Any, Store()),
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: delivered.set(),
    )
    try:
        dispatcher._deliver("session-1", "session.completed")
        dispatcher.acknowledge(user_id=RESERVED_USER_LOCAL, conversation_id="session-1")
        time.sleep(0.25)
        assert not delivered.is_set()
    finally:
        dispatcher.close()


def test_private_profile_redacts_push_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    payloads: list[dict[str, object]] = []

    class Store:
        def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
            return [PushSubscription(RESERVED_USER_LOCAL, "phone", "https://push", "p", "a")]

        def get_or_create_vapid_keys(self) -> VapidKeys:
            return VapidKeys("private", "public")

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(
                title="Top secret command",
                parent_conversation_id=None,
                profile_id="private-profile",
            )

    class Profiles:
        def get_protection(self, profile_id: str) -> dict[str, str]:
            assert profile_id == "private-profile"
            return {"notification_content": "generic"}

    dispatcher = PushNotificationDispatcher(
        store=cast(Any, Store()),
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        profile_store=cast(Any, Profiles()),
        send=lambda **kwargs: payloads.append(json.loads(str(kwargs["data"]))),
    )
    try:
        dispatcher._deliver(
            "session-1",
            "session.needs_input",
            event={
                "elicitation_id": "ask-1",
                "params": {"content_preview": "Run rm -rf private-data"},
            },
        )
    finally:
        dispatcher.close()

    assert payloads[0]["title"] == "Private Omnigent session"
    assert "approval" not in payloads[0]


def test_dispatcher_rechecks_profile_privacy_for_already_queued_payload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = _registered_store(tmp_path, "privacy-recheck.db")
    payload = json.dumps(
        {
            "version": 1,
            "type": "session.needs_input",
            "session_id": "session-1",
            "notification_id": "approval:secret",
            "title": "Secret project",
            "approval": {"description": "Read private.txt"},
        },
        separators=(",", ":"),
    )
    store.enqueue_notification(
        user_id=RESERVED_USER_LOCAL,
        notification_id="approval:secret",
        conversation_id="session-1",
        payload=payload,
        subscriptions=store.list_for_users({RESERVED_USER_LOCAL}),
        available_at=time.time(),
    )
    sent: list[dict[str, object]] = []

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(profile_id="private-profile")

    class Profiles:
        def get_protection(self, profile_id: str) -> dict[str, str]:
            return {"notification_content": "generic"}

    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        profile_store=cast(Any, Profiles()),
        send=lambda **kwargs: sent.append(json.loads(str(kwargs["data"]))),
    )
    try:
        deadline = time.time() + 2
        while not sent and time.time() < deadline:
            time.sleep(0.01)
    finally:
        dispatcher.close()

    assert sent == [
        {
            "version": 1,
            "type": "session.needs_input",
            "session_id": "session-1",
            "notification_id": "approval:secret",
            "title": "Private Omnigent session",
        }
    ]


def test_dispatcher_rechecks_privacy_after_registration_validation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = _registered_store(tmp_path, "privacy-send-boundary.db")
    _enqueue_test_delivery(store)
    sent: list[dict[str, object]] = []

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(profile_id="profile-1")

    class Profiles:
        notification_content = "detailed"

        def get_protection(self, profile_id: str) -> dict[str, str]:
            return {"notification_content": self.notification_content}

    profiles = Profiles()
    monkeypatch.setattr(PushNotificationDispatcher, "_ensure_thread", lambda self: None)
    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        profile_store=cast(Any, profiles),
        send=lambda **kwargs: sent.append(json.loads(str(kwargs["data"]))),
    )
    validate_registration = store.delivery_is_current_and_registered

    def make_profile_private(claim: object) -> bool:
        profiles.notification_content = "generic"
        return validate_registration(cast(Any, claim))

    monkeypatch.setattr(store, "delivery_is_current_and_registered", make_profile_private)

    dispatcher._drain_due_deliveries()

    assert sent == [
        {
            "version": 1,
            "type": "session.completed",
            "session_id": "session-1",
            "notification_id": "event:session-1:one",
            "title": "Private Omnigent session",
        }
    ]


def test_dispatcher_startup_drains_delivery_persisted_before_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = _registered_store(tmp_path)
    _enqueue_test_delivery(store)
    delivered = threading.Event()

    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, object()),
        permission_store=None,
        send=lambda **kwargs: delivered.set(),
    )
    try:
        assert delivered.wait(2), "startup worker did not drain the persisted delivery"
        with store._session("test_read_restart_delivery") as session:
            row = session.execute(select(SqlNotificationDelivery)).scalar_one()
            assert row.delivered_at is not None
            assert row.attempts == 1
    finally:
        dispatcher.close()


def test_dispatcher_startup_discovers_persisted_delivery_in_another_workspace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = PushSubscriptionStore(f"sqlite:///{tmp_path / 'multi-workspace.db'}")
    with workspace_scope(42):
        store.upsert(
            user_id=RESERVED_USER_LOCAL,
            device_id="phone",
            endpoint="https://push.example.test/message/other-workspace",
            p256dh="p" * 87,
            auth="a" * 22,
        )
        _enqueue_test_delivery(store)
    delivered = threading.Event()

    with workspace_scope(0):
        dispatcher = PushNotificationDispatcher(
            store=store,
            conversation_store=cast(Any, object()),
            permission_store=None,
            send=lambda **kwargs: delivered.set(),
        )
    try:
        assert delivered.wait(2), "startup worker did not discover another workspace"
        with workspace_scope(42), store._session("test_read_other_workspace_delivery") as session:
            row = session.execute(select(SqlNotificationDelivery)).scalar_one()
            assert row.delivered_at is not None
    finally:
        dispatcher.close()


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_complete(tmp_path) -> None:
    store = _registered_store(tmp_path)
    _enqueue_test_delivery(store, available_at=100)

    first = store.claim_due_deliveries(now=100, lease_seconds=5)[0]
    assert store.claim_due_deliveries(now=104, lease_seconds=5) == []
    second = store.claim_due_deliveries(now=106, lease_seconds=5)[0]

    assert first.lease_token != second.lease_token
    assert second.attempts == 2
    assert not store.complete_delivery(first, now=107)
    assert store.complete_delivery(second, now=107)


def test_claims_only_one_delivery_per_transport_lease(tmp_path) -> None:
    store = _registered_store(tmp_path, "single-claim.db")
    _enqueue_test_delivery(store, notification_id="event:one", available_at=100)
    _enqueue_test_delivery(store, notification_id="event:two", available_at=100)

    assert len(store.claim_due_deliveries(now=100)) == 1


def test_delivery_enqueue_is_idempotent_per_device_event_and_type(tmp_path) -> None:
    store = _registered_store(tmp_path, "dedupe.db")
    _enqueue_test_delivery(store, notification_id="approval:stable")
    _enqueue_test_delivery(store, notification_id="approval:stable")

    with store._session("test_read_deduplicated_delivery") as session:
        assert len(session.execute(select(SqlNotificationDelivery)).scalars().all()) == 1


def test_replacing_subscription_retargets_pending_delivery_and_delete_cancels_it(
    tmp_path,
) -> None:
    store = _registered_store(tmp_path, "registration-fence.db")
    _enqueue_test_delivery(store, available_at=100)
    claim = store.claim_due_deliveries(now=100)[0]

    store.upsert(
        user_id=RESERVED_USER_LOCAL,
        device_id="phone",
        endpoint="https://push.example.test/message/two",
        p256dh="q" * 87,
        auth="b" * 22,
    )
    assert not store.delivery_is_current_and_registered(claim)
    with store._session("test_read_replaced_delivery") as session:
        replacement = session.execute(select(SqlNotificationDelivery)).scalar_one()
        assert replacement.cancelled_at is None
        assert replacement.endpoint == "https://push.example.test/message/two"
        assert replacement.p256dh == "q" * 87
        assert replacement.auth == "b" * 22
        assert replacement.lease_token is None
    replacement_claim = store.claim_due_deliveries(now=100)[0]
    assert replacement_claim.endpoint == "https://push.example.test/message/two"

    _enqueue_test_delivery(store, notification_id="event:second", available_at=100)
    assert store.delete(user_id=RESERVED_USER_LOCAL, device_id="phone")
    with store._session("test_read_deleted_delivery") as session:
        pending = (
            session.execute(
                select(SqlNotificationDelivery).where(
                    SqlNotificationDelivery.cancelled_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert pending == []


def test_observe_persists_source_event_even_when_wakeup_queue_is_full(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _registered_store(tmp_path, "durable-intent.db")

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build", parent_conversation_id=None, profile_id=None)

    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: None,
    )
    try:
        wake_observed_persistence = threading.Event()

        def wake_after_persistence() -> None:
            with store._session("test_verify_intent_precedes_wakeup") as session:
                assert (
                    session.execute(select(SqlNotificationIntent)).scalar_one_or_none() is not None
                )
            wake_observed_persistence.set()

        monkeypatch.setattr(dispatcher, "_wake_delivery_worker", wake_after_persistence)
        dispatcher.observe(
            "session-1",
            {"type": "response.elicitation_request", "elicitation_id": "ask-1"},
        )
        assert wake_observed_persistence.is_set()
        with store._session("test_read_durable_source_intent") as session:
            intent = session.execute(select(SqlNotificationIntent)).scalar_one()
            assert json.loads(intent.payload)["elicitation_id"] == "ask-1"
    finally:
        dispatcher.close()


def test_settle_intent_survives_dispatcher_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = _registered_store(tmp_path, "settle-restart.db")
    store.schedule_settle_intent(conversation_id="session-1", status="idle", delay=0)
    delivered = threading.Event()

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build", parent_conversation_id=None, profile_id=None)

    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: delivered.set(),
    )
    try:
        assert delivered.wait(2)
        with store._session("test_read_completed_settle_intent") as session:
            assert (
                session.execute(select(SqlNotificationIntent)).scalar_one().completed_at
                is not None
            )
    finally:
        dispatcher.close()


def test_cancelled_settle_intent_cannot_enqueue_after_early_lease_check(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _registered_store(tmp_path, "settle-cancel-race.db")
    store.schedule_settle_intent(conversation_id="session-1", status="idle", delay=0)

    class Conversations:
        def get_conversation(self, conversation_id: str) -> object:
            return SimpleNamespace(title="Build", parent_conversation_id=None, profile_id=None)

    monkeypatch.setattr(PushNotificationDispatcher, "_ensure_thread", lambda self: None)
    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, Conversations()),
        permission_store=None,
        send=lambda **kwargs: None,
    )
    validate_intent = store.intent_is_current

    def cancel_after_validation(claim: object) -> bool:
        assert validate_intent(cast(Any, claim))
        store.cancel_settle_intent(conversation_id="session-1")
        return True

    monkeypatch.setattr(store, "intent_is_current", cancel_after_validation)

    dispatcher._drain_due_intents()

    with store._session("test_read_cancelled_settle_delivery") as session:
        assert session.execute(select(SqlNotificationDelivery)).scalars().all() == []


def test_sqlite_claim_starts_with_begin_immediate(tmp_path) -> None:
    store = _registered_store(tmp_path, "sqlite-claim.db")
    _enqueue_test_delivery(store, available_at=100)
    statements: list[str] = []

    def capture_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(store._engine, "before_cursor_execute", capture_sql)
    try:
        assert store.claim_due_deliveries(now=100)
    finally:
        event.remove(store._engine, "before_cursor_execute", capture_sql)
    assert "BEGIN IMMEDIATE" in statements


def test_postgresql_claim_uses_skip_locked() -> None:
    captured: list[object] = []

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return []

    class Session:
        def execute(self, statement: object) -> Result:
            captured.append(statement)
            return Result()

    @contextmanager
    def managed_session(query_name: str):
        assert query_name == "claim_due_notification_deliveries"
        yield Session()

    store = PushSubscriptionStore.__new__(PushSubscriptionStore)
    store._engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    store._immediate_session = managed_session

    assert store.claim_due_deliveries(now=100) == []
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.parametrize(
    ("attempts", "expected_delay", "terminal"),
    [(1, 1.0, False), (11, 300.0, False), (12, None, True)],
)
def test_transport_retry_uses_capped_exponential_backoff(
    tmp_path,
    attempts: int,
    expected_delay: float | None,
    terminal: bool,
) -> None:
    store = _registered_store(tmp_path, f"retry-{attempts}.db")
    _enqueue_test_delivery(store, available_at=100)
    claim = replace(store.claim_due_deliveries(now=100)[0], attempts=attempts)

    assert store.fail_delivery(claim, "temporary outage", now=200)
    with store._session("test_read_retry") as session:
        row = session.execute(select(SqlNotificationDelivery)).scalar_one()
        if expected_delay is not None:
            assert row.available_at == 200 + expected_delay
        assert (row.cancelled_at is not None) is terminal
        assert row.lease_token is None
        assert row.last_error == "temporary outage"


@pytest.mark.parametrize("status_code", [404, 410])
def test_terminal_push_response_deletes_endpoint_and_finishes_delivery(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setenv("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "true")
    store = _registered_store(tmp_path, f"terminal-{status_code}.db")
    _enqueue_test_delivery(store)
    attempted = threading.Event()

    class TerminalPushError(Exception):
        response = SimpleNamespace(status_code=status_code)

    def send(**kwargs: object) -> None:
        attempted.set()
        raise TerminalPushError(f"gone: {status_code}")

    dispatcher = PushNotificationDispatcher(
        store=store,
        conversation_store=cast(Any, object()),
        permission_store=None,
        send=send,
    )
    try:
        assert attempted.wait(2)
        deadline = time.time() + 2
        while time.time() < deadline:
            with store._session("test_read_terminal_delivery") as session:
                row = session.execute(select(SqlNotificationDelivery)).scalar_one()
                if row.delivered_at is not None:
                    break
            time.sleep(0.01)
        assert row.delivered_at is not None
        assert str(status_code) in (row.last_error or "")
        assert store.list_for_users({RESERVED_USER_LOCAL}) == []
    finally:
        dispatcher.close()


def test_acknowledgement_rolls_back_together_then_cancels_and_enqueues(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _registered_store(tmp_path, "ack.db")
    _enqueue_test_delivery(store, available_at=time.time() + 60)

    def fail_enqueue(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected dismissal enqueue failure")

    with monkeypatch.context() as context:
        context.setattr(store, "_add_delivery", fail_enqueue)
        with pytest.raises(RuntimeError, match="injected dismissal"):
            store.acknowledge_and_enqueue_dismissals(
                user_id=RESERVED_USER_LOCAL,
                conversation_id="session-1",
            )

    with store._session("test_read_rolled_back_ack") as session:
        event = session.execute(select(SqlNotificationEvent)).scalar_one()
        original = session.execute(select(SqlNotificationDelivery)).scalar_one()
        assert event.acknowledged_at is None
        assert original.cancelled_at is None

    assert store.acknowledge_and_enqueue_dismissals(
        user_id=RESERVED_USER_LOCAL,
        conversation_id="session-1",
    ) == ["event:session-1:one"]
    with store._session("test_read_committed_ack") as session:
        event = session.execute(select(SqlNotificationEvent)).scalar_one()
        deliveries = (
            session.execute(
                select(SqlNotificationDelivery).order_by(SqlNotificationDelivery.created_at)
            )
            .scalars()
            .all()
        )
        assert event.acknowledged_at is not None
        assert len(deliveries) == 2
        assert deliveries[0].cancelled_at is not None
        assert deliveries[0].lease_token is None
        assert json.loads(deliveries[1].payload)["type"] == "notification.dismissed"
        assert deliveries[1].cancelled_at is None


def test_pruning_expires_pending_and_removes_old_terminal_state(tmp_path) -> None:
    store = _registered_store(tmp_path, "prune.db")
    _enqueue_test_delivery(store, available_at=1)
    store.enqueue_intent(
        conversation_id="session-1",
        event={"type": "response.elicitation_request", "elicitation_id": "old"},
    )
    with store._session("test_age_notification_state") as session:
        delivery = session.execute(select(SqlNotificationDelivery)).scalar_one()
        delivery.created_at = 1
        intent = session.execute(select(SqlNotificationIntent)).scalar_one()
        intent.created_at = 1
    store.prune_notifications(now=100_000)

    with store._session("test_read_expired_notification_state") as session:
        delivery = session.execute(select(SqlNotificationDelivery)).scalar_one()
        intent = session.execute(select(SqlNotificationIntent)).scalar_one()
        assert delivery.cancelled_at == 100_000
        assert intent.cancelled_at == 100_000

    store.prune_notifications(now=100_000 + 8 * 24 * 60 * 60)
    with store._session("test_read_pruned_notification_state") as session:
        assert session.execute(select(SqlNotificationDelivery)).scalars().all() == []
        assert session.execute(select(SqlNotificationIntent)).scalars().all() == []
