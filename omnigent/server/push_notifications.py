"""Persistent, vendor-neutral Web Push delivery for native clients."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult

from omnigent.db.db_models import (
    SqlNotificationClient,
    SqlNotificationDelivery,
    SqlNotificationEvent,
    SqlNotificationIntent,
    SqlPushSubscription,
    SqlWebPushConfig,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.permissions import LEVEL_OWNER
from omnigent.server.profile_protection import get_profile_protection_by_id
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.profile_store import ProfileStore

_logger = logging.getLogger(__name__)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PushSubscription:
    user_id: str
    device_id: str
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class VapidKeys:
    private_key: str
    public_key: str


@dataclass(frozen=True)
class NotificationDeliveryClaim:
    """One leased outbox delivery ready for a transport attempt."""

    id: str
    lease_token: str
    user_id: str
    device_id: str
    notification_id: str
    delivery_type: str
    session_id: str
    endpoint: str
    p256dh: str
    auth: str
    payload: str
    attempts: int
    created_at: float

    @property
    def subscription(self) -> PushSubscription:
        return PushSubscription(
            self.user_id,
            self.device_id,
            self.endpoint,
            self.p256dh,
            self.auth,
        )


@dataclass(frozen=True)
class NotificationIntentClaim:
    """One leased durable source event ready for derivation."""

    id: str
    lease_token: str
    conversation_id: str
    payload: str
    attempts: int


@dataclass(frozen=True)
class _TransportResult:
    delivered: bool
    terminal_endpoint: bool = False
    error: str | None = None


_DELIVERY_LEASE_SECONDS = 30.0
_DELIVERY_POLL_SECONDS = 0.1
_DELIVERY_RETRY_BASE_SECONDS = 1.0
_DELIVERY_RETRY_MAX_SECONDS = 300.0
_DELIVERY_BATCH_SIZE = 1
_DELIVERY_MAX_ATTEMPTS = 12
_DELIVERY_MAX_AGE_SECONDS = 24 * 60 * 60
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_EVENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_PRUNE_INTERVAL_SECONDS = 60 * 60


def _payload_type(payload: str) -> str:
    with contextlib.suppress(TypeError, ValueError):
        value = json.loads(payload).get("type")
        if isinstance(value, str) and value:
            return value[:32]
    return "unknown"


def _notification_id(
    conversation_id: str,
    kind: str,
    event: dict[str, Any] | None,
) -> str:
    """Return one stable ID shared by every delivery of this event."""
    if kind == "session.needs_input" and event is not None:
        elicitation_id = event.get("elicitation_id")
        if isinstance(elicitation_id, str) and elicitation_id:
            return f"approval:{elicitation_id}"
    if event is not None:
        stable_id = event.get("notification_id")
        if isinstance(stable_id, str) and stable_id:
            return stable_id[:128]
    return f"event:{conversation_id}:{uuid.uuid4().hex}"


def _notification_approval(
    event: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any] | None:
    """Return opaque action metadata for one binary elicitation."""
    elicitation_id = event.get("elicitation_id")
    params = event.get("params")
    if not isinstance(elicitation_id, str) or not elicitation_id or not isinstance(params, dict):
        return None
    requested_schema = params.get("requestedSchema")
    if isinstance(requested_schema, dict) and requested_schema.get("properties"):
        return None
    if params.get("ask_user_question") or params.get("exit_plan_mode"):
        return None
    url = params.get("url")
    if isinstance(url, str) and url and not url.startswith("/approve/"):
        return None

    target_session_id = params.get("target_session_id")
    if not isinstance(target_session_id, str) or not target_session_id:
        target_session_id = conversation_id
    approval: dict[str, Any] = {
        "elicitation_id": elicitation_id,
        "session_id": target_session_id,
    }
    message = params.get("content_preview") or params.get("message")
    if isinstance(message, str) and message.strip():
        description = " ".join(message.replace("**", "").replace("`", "").split())
        approval["description"] = description[:512]
    if params.get("allow_all_edits") is True:
        approval["persistent"] = "allow_all_edits"
    elif isinstance(params.get("remember_scope"), dict):
        approval["persistent"] = "remember"
    return approval


class PushSubscriptionStore:
    """SQL persistence for Web Push endpoints and the server VAPID key."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.push_subscription_store",
        )
        self._immediate_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.push_subscription_store",
            immediate=True,
        )

    def upsert(
        self,
        *,
        user_id: str,
        device_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> None:
        now = int(time.time())
        endpoint_hash = hashlib.sha256(endpoint.encode()).digest()
        with self._immediate_session("upsert_device_subscription") as session:
            workspace_id = current_workspace_id()
            row = session.get(SqlPushSubscription, (workspace_id, user_id, device_id))
            registration_changed = row is not None and (
                row.endpoint != endpoint or row.p256dh != p256dh or row.auth != auth
            )
            conflicts = (
                session.execute(
                    select(SqlPushSubscription).where(
                        SqlPushSubscription.workspace_id == workspace_id,
                        SqlPushSubscription.endpoint_hash == endpoint_hash,
                        (
                            (SqlPushSubscription.user_id != user_id)
                            | (SqlPushSubscription.device_id != device_id)
                        ),
                    )
                )
                .scalars()
                .all()
            )
            replaced_devices = {(item.user_id, item.device_id) for item in conflicts}
            for replaced_user_id, replaced_device_id in replaced_devices:
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == workspace_id,
                        SqlNotificationDelivery.user_id == replaced_user_id,
                        SqlNotificationDelivery.device_id == replaced_device_id,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        cancelled_at=time.time(),
                        lease_token=None,
                        lease_expires_at=None,
                        last_error="device subscription replaced",
                    )
                )
            if registration_changed:
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == workspace_id,
                        SqlNotificationDelivery.user_id == user_id,
                        SqlNotificationDelivery.device_id == device_id,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=None,
                    )
                )
            # An endpoint is a bearer capability and belongs to one device.
            session.execute(
                delete(SqlPushSubscription).where(
                    SqlPushSubscription.workspace_id == workspace_id,
                    SqlPushSubscription.endpoint_hash == endpoint_hash,
                    (
                        (SqlPushSubscription.user_id != user_id)
                        | (SqlPushSubscription.device_id != device_id)
                    ),
                )
            )
            if row is None:
                session.add(
                    SqlPushSubscription(
                        user_id=user_id,
                        device_id=device_id,
                        endpoint=endpoint,
                        endpoint_hash=endpoint_hash,
                        p256dh=p256dh,
                        auth=auth,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.endpoint = endpoint
                row.endpoint_hash = endpoint_hash
                row.p256dh = p256dh
                row.auth = auth
                row.updated_at = now

    def delete(self, *, user_id: str, device_id: str) -> bool:
        with self._immediate_session("delete_device_subscription") as session:
            session.execute(
                update(SqlNotificationDelivery)
                .where(
                    SqlNotificationDelivery.workspace_id == current_workspace_id(),
                    SqlNotificationDelivery.user_id == user_id,
                    SqlNotificationDelivery.device_id == device_id,
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                )
                .values(
                    cancelled_at=time.time(),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="device subscription removed",
                )
            )
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlPushSubscription).where(
                        SqlPushSubscription.workspace_id == current_workspace_id(),
                        SqlPushSubscription.user_id == user_id,
                        SqlPushSubscription.device_id == device_id,
                    )
                ),
            )
            return bool(result.rowcount)

    def delete_endpoint(self, endpoint: str) -> None:
        digest = hashlib.sha256(endpoint.encode()).digest()
        with self._session("delete_expired_subscription") as session:
            session.execute(
                delete(SqlPushSubscription).where(
                    SqlPushSubscription.workspace_id == current_workspace_id(),
                    SqlPushSubscription.endpoint_hash == digest,
                )
            )

    def list_for_users(self, user_ids: set[str]) -> list[PushSubscription]:
        if not user_ids:
            return []
        with self._session("list_recipient_subscriptions") as session:
            rows = (
                session.execute(
                    select(SqlPushSubscription).where(
                        SqlPushSubscription.workspace_id == current_workspace_id(),
                        SqlPushSubscription.user_id.in_(user_ids),
                    )
                )
                .scalars()
                .all()
            )
            return [
                PushSubscription(r.user_id, r.device_id, r.endpoint, r.p256dh, r.auth)
                for r in rows
            ]

    def record_client_activity(
        self,
        *,
        user_id: str,
        device_id: str,
        platform: str,
        foreground: bool,
        active: bool,
        mobile_delay_seconds: int,
    ) -> None:
        """Record foreground and user-activity state for push routing."""
        now = int(time.time())
        with self._session("record_notification_client_activity") as session:
            row = session.get(
                SqlNotificationClient,
                (current_workspace_id(), user_id, device_id),
            )
            if row is None:
                row = SqlNotificationClient(
                    user_id=user_id,
                    device_id=device_id,
                    platform=platform,
                    foreground=foreground,
                    last_active_at=now if active else 0,
                    mobile_delay_seconds=mobile_delay_seconds,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.platform = platform
                row.foreground = foreground
                if active:
                    row.last_active_at = now
                row.mobile_delay_seconds = mobile_delay_seconds
                row.updated_at = now

    def mobile_push_delay_for_user(self, user_id: str) -> float:
        """Return seconds until mobile pushes may resume for this user."""
        now = int(time.time())
        with self._session("get_mobile_push_delay") as session:
            rows = (
                session.execute(
                    select(SqlNotificationClient).where(
                        SqlNotificationClient.workspace_id == current_workspace_id(),
                        SqlNotificationClient.user_id == user_id,
                        SqlNotificationClient.platform.in_(("web", "electron")),
                        SqlNotificationClient.foreground.is_(True),
                    )
                )
                .scalars()
                .all()
            )
        return float(
            max(
                (row.last_active_at + row.mobile_delay_seconds - now for row in rows),
                default=0,
            )
        )

    def record_notification(
        self,
        *,
        user_id: str,
        notification_id: str,
        conversation_id: str,
    ) -> None:
        """Persist one canonical notification before any device delivery."""
        now = int(time.time())
        with self._session("record_notification_event") as session:
            row = session.get(
                SqlNotificationEvent,
                (current_workspace_id(), user_id, notification_id),
            )
            if row is None:
                session.add(
                    SqlNotificationEvent(
                        user_id=user_id,
                        notification_id=notification_id,
                        session_id=conversation_id,
                        created_at=now,
                        acknowledged_at=None,
                    )
                )

    @staticmethod
    def _add_delivery(
        session: Any,
        *,
        subscription: PushSubscription,
        notification_id: str,
        delivery_type: str,
        conversation_id: str,
        payload: str,
        available_at: float,
        now: float,
    ) -> None:
        workspace_id = current_workspace_id()
        identity = "\0".join(
            (
                str(workspace_id),
                subscription.user_id,
                subscription.device_id,
                notification_id,
                delivery_type,
            )
        )
        values = {
            "workspace_id": workspace_id,
            "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
            "user_id": subscription.user_id,
            "device_id": subscription.device_id,
            "notification_id": notification_id,
            "delivery_type": delivery_type,
            "session_id": conversation_id,
            "endpoint": subscription.endpoint,
            "p256dh": subscription.p256dh,
            "auth": subscription.auth,
            "payload": payload,
            "available_at": available_at,
            "lease_token": None,
            "lease_expires_at": None,
            "attempts": 0,
            "created_at": now,
            "delivered_at": None,
            "cancelled_at": None,
            "last_error": None,
        }
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(SqlNotificationDelivery).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    "workspace_id",
                    "user_id",
                    "device_id",
                    "notification_id",
                    "delivery_type",
                ]
            )
            session.execute(statement)
            return
        if dialect == "postgresql":
            statement = postgresql_insert(SqlNotificationDelivery).values(**values)
            statement = statement.on_conflict_do_nothing(
                constraint="uq_notification_deliveries_device_event_type"
            )
            session.execute(statement)
            return
        if session.get(SqlNotificationDelivery, (workspace_id, values["id"])) is None:
            session.add(SqlNotificationDelivery(**values))

    def enqueue_notification(
        self,
        *,
        user_id: str,
        notification_id: str,
        conversation_id: str,
        payload: str,
        subscriptions: list[PushSubscription],
        available_at: float,
        source_intent: NotificationIntentClaim | None = None,
    ) -> bool:
        """Atomically persist a notification and its per-device deliveries."""
        now = time.time()
        with self._immediate_session("enqueue_notification_deliveries") as session:
            if source_intent is not None:
                intent_query = select(SqlNotificationIntent.id).where(
                    SqlNotificationIntent.workspace_id == current_workspace_id(),
                    SqlNotificationIntent.id == source_intent.id,
                    SqlNotificationIntent.lease_token == source_intent.lease_token,
                    SqlNotificationIntent.completed_at.is_(None),
                    SqlNotificationIntent.cancelled_at.is_(None),
                )
                if self._engine.dialect.name == "postgresql":
                    intent_query = intent_query.with_for_update()
                if session.execute(intent_query).scalar_one_or_none() is None:
                    return False
            row = session.get(
                SqlNotificationEvent,
                (current_workspace_id(), user_id, notification_id),
            )
            if row is None:
                session.add(
                    SqlNotificationEvent(
                        user_id=user_id,
                        notification_id=notification_id,
                        session_id=conversation_id,
                        created_at=int(now),
                        acknowledged_at=None,
                    )
                )
            for subscription in subscriptions:
                self._add_delivery(
                    session,
                    subscription=subscription,
                    notification_id=notification_id,
                    delivery_type=_payload_type(payload),
                    conversation_id=conversation_id,
                    payload=payload,
                    available_at=available_at,
                    now=now,
                )
        return True

    def enqueue_dismissal(
        self,
        *,
        user_id: str,
        notification_id: str,
        conversation_id: str,
        payload: str,
        subscriptions: list[PushSubscription],
    ) -> None:
        """Resolve one notification and durably enqueue its dismissals."""
        now = time.time()
        with self._immediate_session("resolve_and_enqueue_notification_dismissal") as session:
            row = session.get(
                SqlNotificationEvent,
                (current_workspace_id(), user_id, notification_id),
            )
            if row is not None and row.acknowledged_at is None:
                row.acknowledged_at = int(now)
            session.execute(
                update(SqlNotificationDelivery)
                .where(
                    SqlNotificationDelivery.workspace_id == current_workspace_id(),
                    SqlNotificationDelivery.user_id == user_id,
                    SqlNotificationDelivery.notification_id == notification_id,
                    SqlNotificationDelivery.delivery_type != "notification.dismissed",
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                )
                .values(cancelled_at=now, lease_token=None, lease_expires_at=None)
            )
            for subscription in subscriptions:
                self._add_delivery(
                    session,
                    subscription=subscription,
                    notification_id=notification_id,
                    delivery_type="notification.dismissed",
                    conversation_id=conversation_id,
                    payload=payload,
                    available_at=now,
                    now=now,
                )

    def acknowledge_notifications(self, *, user_id: str, conversation_id: str) -> list[str]:
        """Mark and return outstanding notification IDs for a viewed session."""
        now = int(time.time())
        with self._session("acknowledge_session_notifications") as session:
            rows = (
                session.execute(
                    select(SqlNotificationEvent).where(
                        SqlNotificationEvent.workspace_id == current_workspace_id(),
                        SqlNotificationEvent.user_id == user_id,
                        SqlNotificationEvent.session_id == conversation_id,
                        SqlNotificationEvent.acknowledged_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.acknowledged_at = now
            return [row.notification_id for row in rows]

    def acknowledge_and_enqueue_dismissals(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> list[str]:
        """Cancel pending pushes and enqueue dismissals in one transaction."""
        now = time.time()
        workspace_id = current_workspace_id()
        with self._immediate_session("acknowledge_and_enqueue_dismissals") as session:
            event_query = select(SqlNotificationEvent).where(
                SqlNotificationEvent.workspace_id == workspace_id,
                SqlNotificationEvent.user_id == user_id,
                SqlNotificationEvent.session_id == conversation_id,
                SqlNotificationEvent.acknowledged_at.is_(None),
            )
            if self._engine.dialect.name == "postgresql":
                event_query = event_query.with_for_update()
            events = session.execute(event_query).scalars().all()
            if not events:
                return []
            notification_ids = [row.notification_id for row in events]
            for row in events:
                row.acknowledged_at = int(now)
            session.execute(
                update(SqlNotificationDelivery)
                .where(
                    SqlNotificationDelivery.workspace_id == workspace_id,
                    SqlNotificationDelivery.user_id == user_id,
                    SqlNotificationDelivery.session_id == conversation_id,
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                )
                .values(cancelled_at=now, lease_token=None, lease_expires_at=None)
            )
            subscriptions = (
                session.execute(
                    select(SqlPushSubscription).where(
                        SqlPushSubscription.workspace_id == workspace_id,
                        SqlPushSubscription.user_id == user_id,
                    )
                )
                .scalars()
                .all()
            )
            for notification_id in notification_ids:
                payload = json.dumps(
                    {
                        "version": 1,
                        "type": "notification.dismissed",
                        "session_id": conversation_id,
                        "notification_id": notification_id,
                    },
                    separators=(",", ":"),
                )
                for row in subscriptions:
                    self._add_delivery(
                        session,
                        subscription=PushSubscription(
                            row.user_id,
                            row.device_id,
                            row.endpoint,
                            row.p256dh,
                            row.auth,
                        ),
                        notification_id=notification_id,
                        delivery_type="notification.dismissed",
                        conversation_id=conversation_id,
                        payload=payload,
                        available_at=now,
                        now=now,
                    )
            return notification_ids

    def enqueue_intent(self, *, conversation_id: str, event: dict[str, Any]) -> str:
        """Persist a source event before waking the in-memory worker."""
        intent_id = uuid.uuid4().hex
        payload = dict(event)
        payload["_notification_intent_id"] = intent_id
        now = time.time()
        with self._session("enqueue_notification_intent") as session:
            session.add(
                SqlNotificationIntent(
                    id=intent_id,
                    conversation_id=conversation_id,
                    payload=json.dumps(payload, separators=(",", ":")),
                    available_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    attempts=0,
                    created_at=now,
                    completed_at=None,
                    cancelled_at=None,
                    last_error=None,
                )
            )
        return intent_id

    @staticmethod
    def _settle_intent_id(conversation_id: str) -> str:
        return "settle:" + hashlib.sha256(conversation_id.encode()).hexdigest()[:56]

    def schedule_settle_intent(
        self,
        *,
        conversation_id: str,
        status: str,
        delay: float,
    ) -> None:
        """Durably replace the pending completion intent for one session."""
        now = time.time()
        intent_id = self._settle_intent_id(conversation_id)
        payload = json.dumps(
            {
                "type": "push.settled",
                "status": status,
                "notification_id": f"event:{conversation_id}:{uuid.uuid4().hex}",
                "_notification_intent_id": intent_id,
            },
            separators=(",", ":"),
        )
        with self._immediate_session("schedule_settled_notification_intent") as session:
            row = session.get(
                SqlNotificationIntent,
                (current_workspace_id(), intent_id),
            )
            if row is None:
                session.add(
                    SqlNotificationIntent(
                        id=intent_id,
                        conversation_id=conversation_id,
                        payload=payload,
                        available_at=now + max(delay, 0.0),
                        lease_token=None,
                        lease_expires_at=None,
                        attempts=0,
                        created_at=now,
                        completed_at=None,
                        cancelled_at=None,
                        last_error=None,
                    )
                )
            else:
                row.payload = payload
                row.available_at = now + max(delay, 0.0)
                row.lease_token = None
                row.lease_expires_at = None
                row.attempts = 0
                row.created_at = now
                row.completed_at = None
                row.cancelled_at = None
                row.last_error = None

    def cancel_settle_intent(self, *, conversation_id: str) -> None:
        """Invalidate any pending completion before a resumed turn can notify."""
        with self._immediate_session("cancel_settled_notification_intent") as session:
            intent_query = select(SqlNotificationIntent).where(
                SqlNotificationIntent.workspace_id == current_workspace_id(),
                SqlNotificationIntent.id == self._settle_intent_id(conversation_id),
                SqlNotificationIntent.completed_at.is_(None),
                SqlNotificationIntent.cancelled_at.is_(None),
            )
            if self._engine.dialect.name == "postgresql":
                intent_query = intent_query.with_for_update()
            row = session.execute(intent_query).scalar_one_or_none()
            if row is None:
                return
            notification_id = None
            with contextlib.suppress(TypeError, ValueError):
                candidate = json.loads(row.payload).get("notification_id")
                if isinstance(candidate, str):
                    notification_id = candidate
            cancelled_at = time.time()
            row.cancelled_at = cancelled_at
            row.lease_token = None
            row.lease_expires_at = None
            if notification_id is not None:
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.session_id == conversation_id,
                        SqlNotificationDelivery.notification_id == notification_id,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        cancelled_at=cancelled_at,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error="settled notification cancelled",
                    )
                )

    def list_pending_workspace_ids(self) -> set[int]:
        """Return every workspace with durable notification work."""
        with self._session("list_pending_notification_workspaces") as session:
            intent_ids = session.execute(
                select(SqlNotificationIntent.workspace_id)
                .where(
                    SqlNotificationIntent.completed_at.is_(None),
                    SqlNotificationIntent.cancelled_at.is_(None),
                )
                .distinct()
            ).scalars()
            delivery_ids = session.execute(
                select(SqlNotificationDelivery.workspace_id)
                .where(
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                )
                .distinct()
            ).scalars()
            return {*intent_ids, *delivery_ids}

    def claim_due_intent(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = _DELIVERY_LEASE_SECONDS,
    ) -> NotificationIntentClaim | None:
        """Lease one source intent so its processing cannot outlive a batch lease."""
        claimed_at = time.time() if now is None else now
        with self._immediate_session("claim_due_notification_intent") as session:
            query = (
                select(SqlNotificationIntent)
                .where(
                    SqlNotificationIntent.workspace_id == current_workspace_id(),
                    SqlNotificationIntent.completed_at.is_(None),
                    SqlNotificationIntent.cancelled_at.is_(None),
                    SqlNotificationIntent.available_at <= claimed_at,
                    SqlNotificationIntent.attempts < _DELIVERY_MAX_ATTEMPTS,
                    SqlNotificationIntent.created_at >= claimed_at - _DELIVERY_MAX_AGE_SECONDS,
                    or_(
                        SqlNotificationIntent.lease_token.is_(None),
                        SqlNotificationIntent.lease_expires_at.is_(None),
                        SqlNotificationIntent.lease_expires_at <= claimed_at,
                    ),
                )
                .order_by(SqlNotificationIntent.available_at, SqlNotificationIntent.id)
                .limit(1)
            )
            if self._engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = session.execute(query).scalar_one_or_none()
            if row is None:
                return None
            token = uuid.uuid4().hex
            row.lease_token = token
            row.lease_expires_at = claimed_at + lease_seconds
            row.attempts += 1
            return NotificationIntentClaim(
                id=row.id,
                lease_token=token,
                conversation_id=row.conversation_id,
                payload=row.payload,
                attempts=row.attempts,
            )

    def intent_is_current(self, claim: NotificationIntentClaim) -> bool:
        with self._session("validate_notification_intent_lease") as session:
            return (
                session.execute(
                    select(SqlNotificationIntent.id).where(
                        SqlNotificationIntent.workspace_id == current_workspace_id(),
                        SqlNotificationIntent.id == claim.id,
                        SqlNotificationIntent.lease_token == claim.lease_token,
                        SqlNotificationIntent.completed_at.is_(None),
                        SqlNotificationIntent.cancelled_at.is_(None),
                    )
                ).scalar_one_or_none()
                is not None
            )

    def complete_intent(self, claim: NotificationIntentClaim, *, now: float | None = None) -> bool:
        completed_at = time.time() if now is None else now
        with self._session("complete_notification_intent") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationIntent)
                    .where(
                        SqlNotificationIntent.workspace_id == current_workspace_id(),
                        SqlNotificationIntent.id == claim.id,
                        SqlNotificationIntent.lease_token == claim.lease_token,
                        SqlNotificationIntent.completed_at.is_(None),
                        SqlNotificationIntent.cancelled_at.is_(None),
                    )
                    .values(
                        completed_at=completed_at,
                        lease_token=None,
                        lease_expires_at=None,
                    )
                ),
            )
            return bool(result.rowcount)

    def fail_intent(
        self,
        claim: NotificationIntentClaim,
        error: str,
        *,
        now: float | None = None,
    ) -> bool:
        failed_at = time.time() if now is None else now
        terminal = claim.attempts >= _DELIVERY_MAX_ATTEMPTS
        delay = min(
            _DELIVERY_RETRY_BASE_SECONDS * (2 ** min(max(claim.attempts - 1, 0), 30)),
            _DELIVERY_RETRY_MAX_SECONDS,
        )
        with self._session("retry_notification_intent") as session:
            values: dict[str, Any] = {
                "lease_token": None,
                "lease_expires_at": None,
                "last_error": error[:2048],
            }
            if terminal:
                values["cancelled_at"] = failed_at
            else:
                values["available_at"] = failed_at + delay
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationIntent)
                    .where(
                        SqlNotificationIntent.workspace_id == current_workspace_id(),
                        SqlNotificationIntent.id == claim.id,
                        SqlNotificationIntent.lease_token == claim.lease_token,
                        SqlNotificationIntent.completed_at.is_(None),
                        SqlNotificationIntent.cancelled_at.is_(None),
                    )
                    .values(**values)
                ),
            )
            return bool(result.rowcount)

    def claim_due_deliveries(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = _DELIVERY_LEASE_SECONDS,
        limit: int = _DELIVERY_BATCH_SIZE,
    ) -> list[NotificationDeliveryClaim]:
        """Lease due rows using the locking primitive for the active dialect."""
        claimed_at = time.time() if now is None else now
        workspace_id = current_workspace_id()
        with self._immediate_session("claim_due_notification_deliveries") as session:
            query = (
                select(SqlNotificationDelivery)
                .where(
                    SqlNotificationDelivery.workspace_id == workspace_id,
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                    SqlNotificationDelivery.available_at <= claimed_at,
                    SqlNotificationDelivery.attempts < _DELIVERY_MAX_ATTEMPTS,
                    SqlNotificationDelivery.created_at >= claimed_at - _DELIVERY_MAX_AGE_SECONDS,
                    or_(
                        SqlNotificationDelivery.lease_token.is_(None),
                        SqlNotificationDelivery.lease_expires_at.is_(None),
                        SqlNotificationDelivery.lease_expires_at <= claimed_at,
                    ),
                )
                .order_by(
                    SqlNotificationDelivery.available_at,
                    SqlNotificationDelivery.id,
                )
                .limit(limit)
            )
            if self._engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            rows = session.execute(query).scalars().all()
            claims: list[NotificationDeliveryClaim] = []
            for row in rows:
                lease_token = uuid.uuid4().hex
                row.lease_token = lease_token
                row.lease_expires_at = claimed_at + lease_seconds
                row.attempts += 1
                claims.append(
                    NotificationDeliveryClaim(
                        id=row.id,
                        lease_token=lease_token,
                        user_id=row.user_id,
                        device_id=row.device_id,
                        notification_id=row.notification_id,
                        delivery_type=row.delivery_type,
                        session_id=row.session_id,
                        endpoint=row.endpoint,
                        p256dh=row.p256dh,
                        auth=row.auth,
                        payload=row.payload,
                        attempts=row.attempts,
                        created_at=row.created_at,
                    )
                )
            return claims

    def delivery_is_current_and_registered(self, claim: NotificationDeliveryClaim) -> bool:
        """Revalidate both the lease and current device registration before I/O."""
        with self._session("validate_notification_delivery_registration") as session:
            delivery = session.execute(
                select(SqlNotificationDelivery.id).where(
                    SqlNotificationDelivery.workspace_id == current_workspace_id(),
                    SqlNotificationDelivery.id == claim.id,
                    SqlNotificationDelivery.lease_token == claim.lease_token,
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                )
            ).scalar_one_or_none()
            if delivery is None:
                return False
            subscription = session.get(
                SqlPushSubscription,
                (current_workspace_id(), claim.user_id, claim.device_id),
            )
            return bool(
                subscription is not None
                and subscription.endpoint == claim.endpoint
                and subscription.p256dh == claim.p256dh
                and subscription.auth == claim.auth
            )

    def update_delivery_payload(
        self,
        claim: NotificationDeliveryClaim,
        payload: str,
    ) -> NotificationDeliveryClaim | None:
        """Redact a payload only while the caller still owns its lease."""
        with self._session("redact_notification_delivery_payload") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.id == claim.id,
                        SqlNotificationDelivery.lease_token == claim.lease_token,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(payload=payload)
                ),
            )
            return replace(claim, payload=payload) if result.rowcount else None

    def cancel_delivery(
        self,
        claim: NotificationDeliveryClaim,
        reason: str,
        *,
        now: float | None = None,
    ) -> bool:
        cancelled_at = time.time() if now is None else now
        with self._session("cancel_notification_delivery") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.id == claim.id,
                        SqlNotificationDelivery.lease_token == claim.lease_token,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        cancelled_at=cancelled_at,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=reason[:2048],
                    )
                ),
            )
            return bool(result.rowcount)

    def complete_delivery(
        self,
        claim: NotificationDeliveryClaim,
        *,
        terminal_endpoint: bool = False,
        last_error: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Finish only the still-current lease; stale workers become no-ops."""
        completed_at = time.time() if now is None else now
        with self._session("complete_notification_delivery") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.id == claim.id,
                        SqlNotificationDelivery.lease_token == claim.lease_token,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        delivered_at=completed_at,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=last_error,
                    )
                ),
            )
            if result.rowcount and terminal_endpoint:
                digest = hashlib.sha256(claim.endpoint.encode()).digest()
                session.execute(
                    delete(SqlPushSubscription).where(
                        SqlPushSubscription.workspace_id == current_workspace_id(),
                        SqlPushSubscription.endpoint_hash == digest,
                    )
                )
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.user_id == claim.user_id,
                        SqlNotificationDelivery.device_id == claim.device_id,
                        SqlNotificationDelivery.endpoint == claim.endpoint,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        cancelled_at=completed_at,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=last_error or "terminal push endpoint",
                    )
                )
            return bool(result.rowcount)

    def fail_delivery(
        self,
        claim: NotificationDeliveryClaim,
        error: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Release the current lease with capped exponential backoff."""
        failed_at = time.time() if now is None else now
        terminal = claim.attempts >= _DELIVERY_MAX_ATTEMPTS or (
            claim.created_at < failed_at - _DELIVERY_MAX_AGE_SECONDS
        )
        delay = min(
            _DELIVERY_RETRY_BASE_SECONDS * (2 ** min(max(claim.attempts - 1, 0), 30)),
            _DELIVERY_RETRY_MAX_SECONDS,
        )
        with self._session("retry_notification_delivery") as session:
            values: dict[str, Any] = {
                "lease_token": None,
                "lease_expires_at": None,
                "last_error": error[:2048],
            }
            if terminal:
                values["cancelled_at"] = failed_at
            else:
                values["available_at"] = failed_at + delay
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.id == claim.id,
                        SqlNotificationDelivery.lease_token == claim.lease_token,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(**values)
                ),
            )
            return bool(result.rowcount)

    def defer_delivery(
        self,
        claim: NotificationDeliveryClaim,
        delay: float,
        *,
        now: float | None = None,
    ) -> bool:
        """Return a current lease to the outbox without recording a failure."""
        deferred_at = time.time() if now is None else now
        with self._session("defer_notification_delivery") as session:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    update(SqlNotificationDelivery)
                    .where(
                        SqlNotificationDelivery.workspace_id == current_workspace_id(),
                        SqlNotificationDelivery.id == claim.id,
                        SqlNotificationDelivery.lease_token == claim.lease_token,
                        SqlNotificationDelivery.delivered_at.is_(None),
                        SqlNotificationDelivery.cancelled_at.is_(None),
                    )
                    .values(
                        available_at=deferred_at + max(delay, _DELIVERY_POLL_SECONDS),
                        lease_token=None,
                        lease_expires_at=None,
                        attempts=max(claim.attempts - 1, 0),
                    )
                ),
            )
            return bool(result.rowcount)

    def prune_notifications(self, *, now: float | None = None) -> None:
        """Bound pending work and remove old terminal notification state."""
        current = time.time() if now is None else now
        terminal_cutoff = current - _TERMINAL_RETENTION_SECONDS
        event_cutoff = current - _EVENT_RETENTION_SECONDS
        pending_cutoff = current - _DELIVERY_MAX_AGE_SECONDS
        with self._immediate_session("prune_notification_state") as session:
            session.execute(
                update(SqlNotificationDelivery)
                .where(
                    SqlNotificationDelivery.workspace_id == current_workspace_id(),
                    SqlNotificationDelivery.delivered_at.is_(None),
                    SqlNotificationDelivery.cancelled_at.is_(None),
                    or_(
                        SqlNotificationDelivery.created_at < pending_cutoff,
                        SqlNotificationDelivery.attempts >= _DELIVERY_MAX_ATTEMPTS,
                    ),
                )
                .values(
                    cancelled_at=current,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="notification delivery expired",
                )
            )
            session.execute(
                update(SqlNotificationIntent)
                .where(
                    SqlNotificationIntent.workspace_id == current_workspace_id(),
                    SqlNotificationIntent.completed_at.is_(None),
                    SqlNotificationIntent.cancelled_at.is_(None),
                    or_(
                        SqlNotificationIntent.created_at < pending_cutoff,
                        SqlNotificationIntent.attempts >= _DELIVERY_MAX_ATTEMPTS,
                    ),
                )
                .values(
                    cancelled_at=current,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error="notification intent expired",
                )
            )
            session.execute(
                delete(SqlNotificationDelivery).where(
                    SqlNotificationDelivery.workspace_id == current_workspace_id(),
                    or_(
                        SqlNotificationDelivery.delivered_at < terminal_cutoff,
                        SqlNotificationDelivery.cancelled_at < terminal_cutoff,
                    ),
                )
            )
            session.execute(
                delete(SqlNotificationIntent).where(
                    SqlNotificationIntent.workspace_id == current_workspace_id(),
                    or_(
                        SqlNotificationIntent.completed_at < terminal_cutoff,
                        SqlNotificationIntent.cancelled_at < terminal_cutoff,
                    ),
                )
            )
            session.execute(
                delete(SqlNotificationEvent).where(
                    SqlNotificationEvent.workspace_id == current_workspace_id(),
                    or_(
                        SqlNotificationEvent.acknowledged_at < int(terminal_cutoff),
                        SqlNotificationEvent.created_at < int(event_cutoff),
                    ),
                )
            )

    def resolve_notification(self, *, user_id: str, notification_id: str) -> None:
        """Mark one approval notification resolved by the runtime."""
        with self._session("resolve_notification_event") as session:
            row = session.get(
                SqlNotificationEvent,
                (current_workspace_id(), user_id, notification_id),
            )
            if row is not None and row.acknowledged_at is None:
                row.acknowledged_at = int(time.time())

    def get_or_create_vapid_keys(self) -> VapidKeys:
        with self._session("get_or_create_vapid_keys") as session:
            row = session.get(SqlWebPushConfig, current_workspace_id())
            if row is None:
                private = ec.generate_private_key(ec.SECP256R1())
                private_value = private.private_numbers().private_value.to_bytes(32, "big")
                public_value = private.public_key().public_bytes(
                    encoding=serialization.Encoding.X962,
                    format=serialization.PublicFormat.UncompressedPoint,
                )
                row = SqlWebPushConfig(
                    private_key=_b64url(private_value),
                    public_key=_b64url(public_value),
                    created_at=int(time.time()),
                )
                session.add(row)
            return VapidKeys(row.private_key, row.public_key)


def validate_web_push_endpoint(endpoint: str) -> None:
    """Reject endpoints that could make Web Push delivery reach local services."""
    if os.environ.get("OMNIGENT_WEBPUSH_ALLOW_PRIVATE_ENDPOINTS", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    host = urlsplit(endpoint).hostname
    if not host:
        raise ValueError("Web Push endpoint must have a hostname")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("Web Push endpoint hostname could not be resolved") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Web Push endpoint must resolve only to public IP addresses")


class PushNotificationDispatcher:
    """Translate session events to encrypted Web Push messages off-thread."""

    def __init__(
        self,
        *,
        store: PushSubscriptionStore,
        conversation_store: ConversationStore,
        permission_store: PermissionStore | None,
        settle_seconds: float = 10.0,
        send: Callable[..., Any] | None = None,
        profile_store: ProfileStore | None = None,
    ) -> None:
        self._store = store
        self._conversations = conversation_store
        self._permissions = permission_store
        self._settle_seconds = settle_seconds
        self._send = send
        self._profiles = profile_store
        self._queue: queue.Queue[tuple[int, str, dict[str, Any]] | None] = queue.Queue(1024)
        self._lock = threading.Lock()
        self._generation: dict[tuple[int, str], int] = {}
        self._timers: dict[tuple[int, str], threading.Timer] = {}
        # Compatibility-only timers for duck-typed test stores without an outbox.
        self._delivery_timers: dict[tuple[int, str, str, str, str], threading.Timer] = {}
        self._active: set[tuple[int, str]] = set()
        self._thread: threading.Thread | None = None
        self._startup_workspace_id = current_workspace_id()
        self._last_prune: dict[int, float] = {}
        self._ensure_thread()

    def observe(self, conversation_id: str, event: dict[str, Any]) -> None:
        if event.get("type") not in {
            "session.status",
            "response.elicitation_request",
            "response.elicitation_resolved",
        }:
            return
        self._ensure_thread()
        enqueue_intent = getattr(self._store, "enqueue_intent", None)
        if enqueue_intent is not None:
            enqueue_intent(conversation_id=conversation_id, event=event)
            self._wake_delivery_worker()
            return
        try:
            self._queue.put_nowait((current_workspace_id(), conversation_id, event))
        except queue.Full:
            _logger.warning("dropping push event: delivery queue is full")

    def close(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            for timer in self._delivery_timers.values():
                timer.cancel()
            self._delivery_timers.clear()
            thread = self._thread
            self._thread = None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=5)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="omnigent-web-push",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        workspaces = {self._startup_workspace_id}
        while True:
            try:
                item = self._queue.get(timeout=_DELIVERY_POLL_SECONDS)
            except queue.Empty:
                item = None
            if item is None:
                if self._thread is None:
                    return
            else:
                workspace_id, conversation_id, event = item
                workspaces.add(workspace_id)
                try:
                    with workspace_scope(workspace_id):
                        self._process(conversation_id, event)
                except Exception:  # One event must not stop delivery.
                    _logger.exception("failed to process push event for %s", conversation_id)
            discover_workspaces = getattr(self._store, "list_pending_workspace_ids", None)
            if discover_workspaces is not None:
                try:
                    workspaces.update(discover_workspaces())
                except Exception:  # A database outage must not stop polling.
                    _logger.exception("failed to discover notification workspaces")
            for workspace_id in workspaces:
                try:
                    with workspace_scope(workspace_id):
                        self._drain_due_intents()
                        self._drain_due_deliveries()
                        self._prune_if_due(workspace_id)
                except Exception:  # A database outage must not stop polling.
                    _logger.exception("failed to drain notification deliveries")

    def _process(
        self,
        conversation_id: str,
        event: dict[str, Any],
        *,
        source_intent: NotificationIntentClaim | None = None,
    ) -> None:
        event_type = event.get("type")
        if event_type == "push.drain":
            return
        if event_type == "push.acknowledged":
            user_id = event.get("user_id")
            notification_ids = event.get("notification_ids")
            if isinstance(user_id, str) and isinstance(notification_ids, list):
                self._send_acknowledgements(
                    user_id,
                    conversation_id,
                    [value for value in notification_ids if isinstance(value, str)],
                )
            return
        key = (current_workspace_id(), conversation_id)
        if event_type == "response.elicitation_request":
            self._cancel_completion(conversation_id)
            self._active.discard(key)
            self._deliver(conversation_id, "session.needs_input", event=event)
            return
        if event_type == "response.elicitation_resolved":
            self._deliver(conversation_id, "notification.dismissed", event=event)
            return
        if event_type == "push.settled":
            kind = "session.failed" if event.get("status") == "failed" else "session.completed"
            self._deliver(conversation_id, kind, source_intent=source_intent)
            return
        status = event.get("status")
        cancel_settle = getattr(self._store, "cancel_settle_intent", None)
        schedule_settle = getattr(self._store, "schedule_settle_intent", None)
        if cancel_settle is not None and schedule_settle is not None:
            if status in {"running", "waiting", "launching"}:
                cancel_settle(conversation_id=conversation_id)
            elif status in {"idle", "failed"}:
                schedule_settle(
                    conversation_id=conversation_id,
                    status=str(status),
                    delay=self._settle_seconds,
                )
            return
        if status in {"running", "waiting", "launching"}:
            self._active.add(key)
            self._cancel_completion(conversation_id)
        elif status in {"idle", "failed"} and key in self._active:
            self._active.discard(key)
            self._schedule_completion(conversation_id, status)

    def _cancel_completion(self, conversation_id: str) -> None:
        key = (current_workspace_id(), conversation_id)
        with self._lock:
            self._generation[key] = self._generation.get(key, 0) + 1
            timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _schedule_completion(self, conversation_id: str, status: str) -> None:
        key = (current_workspace_id(), conversation_id)
        self._cancel_completion(conversation_id)
        with self._lock:
            generation = self._generation[key]

        def settled() -> None:
            with self._lock:
                if self._generation.get(key) != generation:
                    return
                self._timers.pop(key, None)
            try:
                self._queue.put_nowait(
                    (key[0], conversation_id, {"type": "push.settled", "status": status})
                )
            except queue.Full:
                _logger.warning("dropping settled push event: delivery queue is full")

        timer = threading.Timer(self._settle_seconds, settled)
        timer.daemon = True
        with self._lock:
            self._timers[key] = timer
        timer.start()

    def _deliver(
        self,
        conversation_id: str,
        kind: str,
        *,
        event: dict[str, Any] | None = None,
        source_intent: NotificationIntentClaim | None = None,
    ) -> None:
        conversation = self._conversations.get_conversation(conversation_id)
        if conversation is None:
            return
        if getattr(conversation, "parent_conversation_id", None) is not None:
            return
        profile_id = getattr(conversation, "profile_id", None)
        protection = (
            self._profiles.get_protection(profile_id)
            if self._profiles is not None and profile_id is not None
            else {}
        )
        notification_content = protection.get("notification_content")
        if notification_content == "disabled":
            return
        generic_content = notification_content == "generic" or (
            profile_id is not None and get_profile_protection_by_id(profile_id) is not None
        )
        recipients = self._recipients(
            conversation_id,
            approvals_only=kind in {"session.needs_input", "notification.dismissed"},
        )
        subscriptions = self._store.list_for_users(recipients)
        if not subscriptions:
            return
        if kind == "notification.dismissed":
            elicitation_id = event.get("elicitation_id") if event is not None else None
            if not isinstance(elicitation_id, str) or not elicitation_id:
                return
            payload_data: dict[str, Any] = {
                "version": 1,
                "type": kind,
                "session_id": conversation_id,
                "notification_id": f"approval:{elicitation_id}",
            }
        else:
            payload_data = {
                "version": 1,
                "type": kind,
                "session_id": conversation_id,
                "notification_id": _notification_id(conversation_id, kind, event),
                "title": (
                    "Private Omnigent session"
                    if generic_content
                    else (conversation.title or "Omnigent session")[:256]
                ),
            }
        if kind == "session.needs_input" and event is not None:
            approval = _notification_approval(event, conversation_id)
            # A locked profile cannot safely authorize a background action:
            # the notification receiver has no user-present unlock bearer.
            if approval is not None and not generic_content:
                payload_data["approval"] = approval
        payload = json.dumps(payload_data, separators=(",", ":"))
        notification_id = str(payload_data["notification_id"])
        immediate = kind in {"session.needs_input", "notification.dismissed"}
        enqueue = getattr(self._store, "enqueue_notification", None)
        enqueue_dismissal = getattr(self._store, "enqueue_dismissal", None)
        if enqueue is not None and enqueue_dismissal is not None:
            now = time.time()
            for user_id in recipients:
                user_subscriptions = [
                    subscription
                    for subscription in subscriptions
                    if subscription.user_id == user_id
                ]
                if not user_subscriptions:
                    continue
                if kind == "notification.dismissed":
                    enqueue_dismissal(
                        user_id=user_id,
                        notification_id=notification_id,
                        conversation_id=conversation_id,
                        payload=payload,
                        subscriptions=user_subscriptions,
                    )
                else:
                    available_at = now + (0.0 if immediate else self._mobile_delay(user_id))
                    enqueue_args: dict[str, Any] = {
                        "user_id": user_id,
                        "notification_id": notification_id,
                        "conversation_id": conversation_id,
                        "payload": payload,
                        "subscriptions": user_subscriptions,
                        "available_at": available_at,
                    }
                    if source_intent is not None:
                        enqueue_args["source_intent"] = source_intent
                    if enqueue(**enqueue_args) is False:
                        return
            self._wake_delivery_worker()
            return

        if kind == "notification.dismissed":
            resolver = getattr(self._store, "resolve_notification", None)
            if resolver is not None:
                for user_id in recipients:
                    resolver(user_id=user_id, notification_id=notification_id)
        else:
            recorder = getattr(self._store, "record_notification", None)
            if recorder is not None:
                for user_id in recipients:
                    recorder(
                        user_id=user_id,
                        notification_id=notification_id,
                        conversation_id=conversation_id,
                    )
        for subscription in subscriptions:
            delay = 0.0 if immediate else self._mobile_delay(subscription.user_id)
            if delay > 0:
                self._schedule_mobile_delivery(
                    subscription,
                    conversation_id,
                    notification_id,
                    payload,
                    delay,
                )
            else:
                self._send_payload(subscription, payload)

    def acknowledge(self, *, user_id: str, conversation_id: str) -> None:
        """Cancel mobile deliveries for a session the user has already viewed."""
        workspace_id = current_workspace_id()
        durable_acknowledge = getattr(self._store, "acknowledge_and_enqueue_dismissals", None)
        if durable_acknowledge is not None:
            if durable_acknowledge(user_id=user_id, conversation_id=conversation_id):
                self._wake_delivery_worker()
            return
        with self._lock:
            keys = [
                key
                for key in self._delivery_timers
                if key[0] == workspace_id and key[1] == user_id and key[2] == conversation_id
            ]
            timers = [self._delivery_timers.pop(key) for key in keys]
        for timer in timers:
            timer.cancel()
        acknowledge = getattr(self._store, "acknowledge_notifications", None)
        notification_ids = (
            acknowledge(user_id=user_id, conversation_id=conversation_id)
            if acknowledge is not None
            else []
        )
        if not notification_ids:
            return
        self._ensure_thread()
        try:
            self._queue.put_nowait(
                (
                    workspace_id,
                    conversation_id,
                    {
                        "type": "push.acknowledged",
                        "user_id": user_id,
                        "notification_ids": notification_ids,
                    },
                )
            )
        except queue.Full:
            _logger.warning("dropping notification acknowledgement: delivery queue is full")

    def _wake_delivery_worker(self) -> None:
        self._ensure_thread()
        # A full queue is harmless because idle polling still sees durable rows.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait((current_workspace_id(), "", {"type": "push.drain"}))

    def _drain_due_intents(self) -> None:
        claim_due = getattr(self._store, "claim_due_intent", None)
        if claim_due is None:
            return
        while (claim := claim_due()) is not None:
            try:
                if not self._store.intent_is_current(claim):
                    continue
                event = json.loads(claim.payload)
                if not isinstance(event, dict):
                    raise ValueError("notification intent payload must be an object")
                self._process(claim.conversation_id, event, source_intent=claim)
            except Exception as exc:  # Intent remains retryable.
                self._store.fail_intent(claim, str(exc) or type(exc).__name__)
                _logger.exception("failed to derive notification intent %s", claim.id)
            else:
                self._store.complete_intent(claim)

    def _prune_if_due(self, workspace_id: int) -> None:
        prune = getattr(self._store, "prune_notifications", None)
        if prune is None:
            return
        now = time.time()
        if now - self._last_prune.get(workspace_id, 0.0) < _PRUNE_INTERVAL_SECONDS:
            return
        prune(now=now)
        self._last_prune[workspace_id] = now

    def _drain_due_deliveries(self) -> None:
        claim_due = getattr(self._store, "claim_due_deliveries", None)
        if claim_due is None:
            return
        while claims := claim_due():
            for claim in claims:
                payload_type = None
                with contextlib.suppress(AttributeError, TypeError, ValueError):
                    payload_type = json.loads(claim.payload).get("type")
                if payload_type not in {"session.needs_input", "notification.dismissed"}:
                    remaining = self._mobile_delay(claim.user_id)
                    if remaining > 0:
                        self._store.defer_delivery(claim, remaining)
                        continue
                claim = self._prepare_delivery(claim)
                if claim is None:
                    continue
                if not self._store.delivery_is_current_and_registered(claim):
                    self._store.cancel_delivery(claim, "device registration changed")
                    continue
                claim = self._prepare_delivery(claim)
                if claim is None:
                    continue
                result = self._send_payload(claim.subscription, claim.payload)
                if result.delivered:
                    self._store.complete_delivery(
                        claim,
                        terminal_endpoint=result.terminal_endpoint,
                        last_error=result.error,
                    )
                else:
                    self._store.fail_delivery(
                        claim,
                        result.error or "unknown Web Push transport failure",
                    )

    def _prepare_delivery(
        self,
        claim: NotificationDeliveryClaim,
    ) -> NotificationDeliveryClaim | None:
        """Apply the profile's current notification policy immediately before send."""
        if claim.delivery_type == "notification.dismissed":
            return claim
        get_conversation = getattr(self._conversations, "get_conversation", None)
        if get_conversation is None:
            return claim
        conversation = get_conversation(claim.session_id)
        if conversation is None:
            self._store.cancel_delivery(claim, "notification session no longer exists")
            return None
        profile_id = getattr(conversation, "profile_id", None)
        protection = (
            self._profiles.get_protection(profile_id)
            if self._profiles is not None and profile_id is not None
            else {}
        )
        notification_content = protection.get("notification_content")
        if notification_content == "disabled":
            self._store.cancel_delivery(claim, "profile notifications disabled")
            return None
        generic = notification_content == "generic" or (
            profile_id is not None and get_profile_protection_by_id(profile_id) is not None
        )
        if not generic:
            return claim
        try:
            payload = json.loads(claim.payload)
        except (TypeError, ValueError):
            self._store.cancel_delivery(claim, "invalid notification payload")
            return None
        payload["title"] = "Private Omnigent session"
        payload.pop("approval", None)
        redacted = json.dumps(payload, separators=(",", ":"))
        if redacted == claim.payload:
            return claim
        return self._store.update_delivery_payload(claim, redacted)

    def _send_acknowledgements(
        self,
        user_id: str,
        conversation_id: str,
        notification_ids: list[str],
    ) -> None:
        subscriptions = self._store.list_for_users({user_id})
        for notification_id in notification_ids:
            payload = json.dumps(
                {
                    "version": 1,
                    "type": "notification.dismissed",
                    "session_id": conversation_id,
                    "notification_id": notification_id,
                },
                separators=(",", ":"),
            )
            for subscription in subscriptions:
                self._send_payload(subscription, payload)

    def _schedule_mobile_delivery(
        self,
        subscription: PushSubscription,
        conversation_id: str,
        notification_id: str,
        payload: str,
        delay: float,
    ) -> None:
        key = (
            current_workspace_id(),
            subscription.user_id,
            conversation_id,
            notification_id,
            subscription.device_id,
        )

        def deliver_when_inactive() -> None:
            with workspace_scope(key[0]):
                remaining = self._mobile_delay(subscription.user_id)
                if remaining > 0:
                    self._schedule_mobile_delivery(
                        subscription,
                        conversation_id,
                        notification_id,
                        payload,
                        remaining,
                    )
                    return
                with self._lock:
                    if self._delivery_timers.pop(key, None) is None:
                        return
                self._send_payload(subscription, payload)

        timer = threading.Timer(max(delay, 0.1), deliver_when_inactive)
        timer.daemon = True
        with self._lock:
            previous = self._delivery_timers.pop(key, None)
            self._delivery_timers[key] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _mobile_delay(self, user_id: str) -> float:
        provider = getattr(self._store, "mobile_push_delay_for_user", None)
        return float(provider(user_id)) if provider is not None else 0.0

    def _send_payload(self, subscription: PushSubscription, payload: str) -> _TransportResult:
        keys = self._store.get_or_create_vapid_keys()
        sender = self._send
        production_sender = sender is None
        if sender is None:
            from pywebpush import webpush

            sender = webpush
        requests_session = None
        try:
            validate_web_push_endpoint(subscription.endpoint)
            send_options: dict[str, Any] = {}
            if production_sender:
                from requests import Session

                requests_session = Session()
                requests_session.max_redirects = 0
                send_options["requests_session"] = requests_session
            sender(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                data=payload,
                vapid_private_key=keys.private_key,
                vapid_claims={
                    "sub": os.environ.get(
                        "OMNIGENT_WEBPUSH_VAPID_SUBJECT",
                        "mailto:notifications@omnigent.ai",
                    )
                },
                ttl=300,
                timeout=10,
                **send_options,
            )
            return _TransportResult(delivered=True)
        except Exception as exc:  # noqa: BLE001 - transport errors are isolated
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) in {404, 410}:
                # Durable stores delete in the same guarded completion transaction.
                if getattr(self._store, "complete_delivery", None) is None:
                    self._store.delete_endpoint(subscription.endpoint)
                return _TransportResult(
                    delivered=True,
                    terminal_endpoint=True,
                    error=str(exc)[:2048],
                )
            _logger.warning("Web Push delivery failed: %s", exc)
            return _TransportResult(delivered=False, error=str(exc)[:2048])
        finally:
            if requests_session is not None:
                requests_session.close()

    def _recipients(self, conversation_id: str, *, approvals_only: bool) -> set[str]:
        if self._permissions is None:
            return {RESERVED_USER_LOCAL}
        grants, cursor = self._permissions.list_for_session(conversation_id, limit=100)
        while cursor is not None:
            page, cursor = self._permissions.list_for_session(
                conversation_id,
                limit=100,
                after_user_id=cursor,
            )
            grants.extend(page)
        recipients = {
            grant.user_id
            for grant in grants
            if grant.user_id != RESERVED_USER_PUBLIC
            and (not approvals_only or grant.level >= LEVEL_OWNER)
        }
        owner = self._conversations.get_session_owner(conversation_id)
        if owner is not None:
            recipients.add(owner)
        return recipients
