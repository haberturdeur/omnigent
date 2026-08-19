"""Persistent, vendor-neutral Web Push delivery for native clients."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from omnigent.db.db_models import (
    SqlPushSubscription,
    SqlWebPushConfig,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.permissions import LEVEL_OWNER
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

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
    message = params.get("message")
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
        with self._session("upsert_device_subscription") as session:
            # An endpoint is a bearer capability and belongs to one device.
            session.execute(
                delete(SqlPushSubscription).where(
                    SqlPushSubscription.workspace_id == current_workspace_id(),
                    SqlPushSubscription.endpoint_hash == endpoint_hash,
                    (
                        (SqlPushSubscription.user_id != user_id)
                        | (SqlPushSubscription.device_id != device_id)
                    ),
                )
            )
            row = session.get(
                SqlPushSubscription,
                (current_workspace_id(), user_id, device_id),
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
        with self._session("delete_device_subscription") as session:
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
                session.flush()
            return VapidKeys(row.private_key, row.public_key)


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
    ) -> None:
        self._store = store
        self._conversations = conversation_store
        self._permissions = permission_store
        self._settle_seconds = settle_seconds
        self._send = send
        self._queue: queue.Queue[tuple[int, str, dict[str, Any]] | None] = queue.Queue(1024)
        self._lock = threading.Lock()
        self._generation: dict[tuple[int, str], int] = {}
        self._timers: dict[tuple[int, str], threading.Timer] = {}
        self._active: set[tuple[int, str]] = set()
        self._thread = threading.Thread(
            target=self._run,
            name="omnigent-web-push",
            daemon=True,
        )
        self._thread.start()

    def observe(self, conversation_id: str, event: dict[str, Any]) -> None:
        if event.get("type") not in {
            "session.status",
            "response.elicitation_request",
            "response.elicitation_resolved",
        }:
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
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            workspace_id, conversation_id, event = item
            try:
                with workspace_scope(workspace_id):
                    self._process(conversation_id, event)
            except Exception:  # One event must not stop delivery.
                _logger.exception("failed to process push event for %s", conversation_id)

    def _process(self, conversation_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type")
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
            self._deliver(conversation_id, kind)
            return
        status = event.get("status")
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
    ) -> None:
        conversation = self._conversations.get_conversation(conversation_id)
        if conversation is None:
            return
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
                "title": conversation.title or "Omnigent session",
            }
        if kind == "session.needs_input" and event is not None:
            approval = _notification_approval(event, conversation_id)
            if approval is not None:
                payload_data["approval"] = approval
        payload = json.dumps(payload_data, separators=(",", ":"))
        keys = self._store.get_or_create_vapid_keys()
        sender = self._send
        if sender is None:
            from pywebpush import webpush

            sender = webpush
        for subscription in subscriptions:
            try:
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
                )
            except Exception as exc:  # noqa: BLE001 - transport errors are isolated
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) in {404, 410}:
                    self._store.delete_endpoint(subscription.endpoint)
                else:
                    _logger.warning("Web Push delivery failed: %s", exc)

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
