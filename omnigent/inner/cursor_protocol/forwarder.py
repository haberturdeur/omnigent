"""Forward native todo snapshots from the TUI's private journal."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from omnigent.cursor_native_permissions import (
    _post_external_elicitation_resolved,
    cursor_tool_call_elicitation_id,
)
from omnigent.inner.cursor_protocol.transport import send_control

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
logger = logging.getLogger(__name__)


def decision_payload(decision: Json, elicitation_id: str) -> Json:
    args = decision.get("args") or {}
    kind = decision["kind"]
    operation = args.get("type") or kind
    details = args.get("details") or args
    preview = next(
        (
            details[key]
            for key in ("command", "path", "filePath", "url")
            if isinstance(details.get(key), str) and details[key]
        ),
        json.dumps(details, ensure_ascii=False),
    )
    payload: Json = {
        "elicitation_id": elicitation_id,
        "native_control": True,
        "operation_type": operation,
        "message": f"Cursor wants to run {operation}",
        "content_preview": preview[:1024],
    }
    if kind == "question":
        payload["message"] = args.get("title") or "Cursor needs input"
        payload["ask_user_question"] = {
            "questions": [
                {
                    "id": q["id"],
                    "question": q["prompt"],
                    "multiSelect": True,
                    "options": [{"label": o["label"]} for o in q.get("options", [])],
                }
                for q in args.get("questions", [])
            ]
        }
    return payload


def pending_decisions(active: Json, epoch: str) -> dict[str, Json]:
    """Use the mode read from Cursor's live runtime, never launch flags."""
    run_everything = active.get("runEverything") is True
    pending = {}
    for decision in active.get("pending", []):
        auto_accept = run_everything and decision.get("kind") == "tool"
        key = f"{epoch}:{decision['id']}:{auto_accept}"
        pending[key] = {**decision, "epoch": epoch, "auto_accept": auto_accept}
    return pending


async def read_pending_decisions(
    client: httpx.AsyncClient, session_id: str, active: Json, epoch: str
) -> dict[str, Json]:
    if not active.get("pending"):
        return {}
    response = await client.get(f"/v1/sessions/{session_id}/cursor-approval-config", timeout=5)
    response.raise_for_status()
    mode = response.json().get("approval_mode")
    if mode not in ("allowlist", "unrestricted", "auto-review"):
        raise ValueError("Cursor approval configuration is unavailable")
    return pending_decisions({**active, "runEverything": mode == "unrestricted"}, epoch)


async def answer_decision(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    decision: Json,
    elicitation_id: str,
) -> None:
    """Resolve through the native store/callback which also dismisses the TUI dialog."""
    while True:
        try:
            if decision.get("auto_accept") is True:
                await asyncio.to_thread(
                    send_control,
                    bridge_dir,
                    {
                        "action": "reply",
                        "id": decision["id"],
                        "outcome": "accept",
                        "expected_epoch": decision.get("epoch"),
                        "require_run_everything": True,
                    },
                )
                return
            response = await client.post(
                f"/v1/sessions/{session_id}/hooks/cursor-permission-request",
                json=decision_payload(decision, elicitation_id),
                timeout=None,
            )
            response.raise_for_status()
            if not response.content:
                return
            result = response.json()
            content = result.get("content") or {}
            await asyncio.to_thread(
                send_control,
                bridge_dir,
                {
                    "action": "reply",
                    "id": decision["id"],
                    "outcome": result.get("action"),
                    "answers": content,
                    "feedback": content.get("feedback"),
                    "expected_epoch": decision.get("epoch"),
                },
            )
            return
        except RuntimeError:
            logger.warning(
                "Cursor native decision was not acknowledged; no keyboard retry", exc_info=True
            )
            return
        except (httpx.HTTPError, ValueError, KeyError):
            logger.debug("Cursor native decision delivery will retry", exc_info=True)
            await asyncio.sleep(2)


def event_payload(event: Json) -> Json | None:
    """Translate authoritative parent state without duplicating transcript items."""
    if event.get("root") is not True:
        return None
    message = event.get("message") or {}
    if event.get("kind") in ("command_error", "error"):
        return {
            "type": "external_session_status",
            "data": {
                "status": "failed",
                "output": message.get("message") or "Cursor command failed",
            },
        }
    if event.get("kind") == "conversation_checkpoint_update":
        statuses = {
            "TODO_STATUS_UNSPECIFIED": "pending",
            "TODO_STATUS_PENDING": "pending",
            "TODO_STATUS_IN_PROGRESS": "in_progress",
            "TODO_STATUS_COMPLETED": "completed",
        }
        todos = []
        for todo in message.get("todos", []):
            status = todo.get("status", "TODO_STATUS_UNSPECIFIED")
            if status == "TODO_STATUS_CANCELLED":
                continue
            if status not in statuses:
                return None
            todos.append(
                {
                    "content": todo["content"],
                    "activeForm": todo["content"],
                    "status": statuses[status],
                }
            )
        return {"type": "external_session_todos", "data": {"todos": todos}}
    return None


def subagent_payloads(event: Json) -> list[Json]:
    """Register native child identities from the parent's persisted checkpoint."""
    if event.get("root") is not True or event.get("kind") != "conversation_checkpoint_update":
        return []
    return [
        {
            "type": "external_cursor_subagent_start",
            "data": {"subagent_id": child["id"], "title": child.get("title") or "Cursor subagent"},
        }
        for child in event.get("message", {}).get("subagents", [])
    ]


def latest_checkpoint(
    directory: Path, epoch: str, conversation_id: str | None = None
) -> Json | None:
    """Recover the latest snapshot, including prior processes of this exact chat."""
    current = directory / f"events.{epoch}.jsonl"
    paths = [current]
    if conversation_id:
        paths.extend(
            sorted(
                (p for p in directory.glob("events.*.jsonl") if p != current),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
        )
    for path in paths:
        latest = None
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                event = json.loads(line)
                if (
                    event.get("root") is True
                    and event.get("kind") == "conversation_checkpoint_update"
                    and (conversation_id is None or event.get("conversationId") == conversation_id)
                ):
                    latest = event
        if latest is not None:
            return latest
    return None


async def sync_children(
    client: httpx.AsyncClient,
    session_id: str,
    bridge_dir: Path,
    event: Json,
    registered: dict[str, str],
    tasks: dict[str, asyncio.Task[None]],
    auth: httpx.Auth,
) -> None:
    from omnigent.cursor_native_forwarder import supervise_cursor_forwarder

    for payload in subagent_payloads(event):
        native_id = str(uuid.UUID(payload["data"]["subagent_id"]))
        if native_id not in registered:
            response = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
            response.raise_for_status()
            child_id = response.json().get("child_session_id")
            if not isinstance(child_id, str) or not child_id:
                raise ValueError("Cursor child registration did not return a session id")
            registered[native_id] = child_id
        if native_id in tasks:
            continue
        try:
            parent = json.loads((bridge_dir / "cursor_forwarder.json").read_text())
            store = Path(parent["store_path"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if store.parent.name != event.get("conversationId"):
            continue
        child_bridge = bridge_dir / "protocol" / "children" / native_id
        child_bridge.mkdir(parents=True, exist_ok=True, mode=0o700)
        tasks[native_id] = asyncio.create_task(
            supervise_cursor_forwarder(
                base_url=str(client.base_url),
                headers={},
                session_id=registered[native_id],
                bridge_dir=child_bridge,
                agent_name=payload["data"]["title"],
                workspace="",
                launch_epoch_ms=0,
                auth=auth,
                fixed_store_path=store.parent.parent / native_id / "store.db",
            )
        )


async def forward_protocol_events(
    *,
    base_url: str,
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth,
) -> None:
    """Retry delivery without advancing the durable journal position on failure."""
    directory = bridge_dir / "protocol"
    position = directory / "forwarder.json"
    epoch, offset = "", 0
    try:
        saved = json.loads(position.read_text())
        epoch, offset = saved["epoch"], saved["offset"]
    except (OSError, ValueError, KeyError):
        pass
    async with httpx.AsyncClient(base_url=base_url, auth=auth, timeout=30) as client:
        tasks: dict[str, tuple[str, asyncio.Task[None]]] = {}
        registered_children: dict[str, str] = {}
        child_tasks: dict[str, asyncio.Task[None]] = {}
        recovered_identity = None
        snapshot = None
        last_refresh = 0.0
        try:
            while True:
                try:
                    active = json.loads((directory / "active.json").read_text())
                    current = str(uuid.UUID(active["epoch"]))
                    conversation_id = active.get("conversationId")
                    identity = (current, conversation_id)
                    if identity != recovered_identity:
                        snapshot = latest_checkpoint(directory, current, conversation_id)
                        recovered_identity = identity
                        last_refresh = 0.0
                    pending = await read_pending_decisions(client, session_id, active, current)
                    for key in list(tasks):
                        if key not in pending:
                            elicitation_id, task = tasks.pop(key)
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await task
                            await _post_external_elicitation_resolved(
                                client, session_id, elicitation_id
                            )
                    for key, decision in pending.items():
                        if key not in tasks:
                            elicitation_id = cursor_tool_call_elicitation_id(session_id, key)
                            tasks[key] = (
                                elicitation_id,
                                asyncio.create_task(
                                    answer_decision(
                                        client, session_id, bridge_dir, decision, elicitation_id
                                    )
                                ),
                            )
                    if current != epoch:
                        epoch, offset = current, 0
                    with (directory / f"events.{epoch}.jsonl").open("rb") as stream:
                        stream.seek(offset)
                        for _ in range(100):
                            line = stream.readline()
                            if not line.endswith(b"\n"):
                                break
                            event = json.loads(line)
                            payload = event_payload(event)
                            if payload is not None:
                                response = await client.post(
                                    f"/v1/sessions/{session_id}/events", json=payload
                                )
                                response.raise_for_status()
                                if event.get("kind") == "conversation_checkpoint_update":
                                    snapshot = event
                                await sync_children(
                                    client,
                                    session_id,
                                    bridge_dir,
                                    event,
                                    registered_children,
                                    child_tasks,
                                    auth,
                                )
                            offset = stream.tell()
                            temporary = position.with_suffix(".tmp")
                            temporary.write_text(json.dumps({"epoch": epoch, "offset": offset}))
                            temporary.replace(position)
                    if snapshot is not None and time.monotonic() - last_refresh >= 5:
                        payload = event_payload(snapshot)
                        if payload is not None:
                            response = await client.post(
                                f"/v1/sessions/{session_id}/events", json=payload
                            )
                            response.raise_for_status()
                        await sync_children(
                            client,
                            session_id,
                            bridge_dir,
                            snapshot,
                            registered_children,
                            child_tasks,
                            auth,
                        )
                        last_refresh = time.monotonic()
                except (OSError, ValueError, KeyError, httpx.HTTPError):
                    logger.debug("Cursor native protocol delivery will retry", exc_info=True)
                await asyncio.sleep(0.2)
        finally:
            for _, task in tasks.values():
                task.cancel()
            await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)
            for task in child_tasks.values():
                task.cancel()
            await asyncio.gather(*child_tasks.values(), return_exceptions=True)
