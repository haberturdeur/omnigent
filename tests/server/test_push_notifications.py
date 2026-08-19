"""Persistence tests for the vendor-neutral Web Push backend."""

from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.push_notifications import (
    PushNotificationDispatcher,
    PushSubscription,
    PushSubscriptionStore,
    VapidKeys,
)
from omnigent.server.routes.push_notifications import create_push_notifications_router


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
