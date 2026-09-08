"""Recovery of native snapshots and child transcript forwarding."""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent import cursor_native_forwarder as transcript
from omnigent.inner.cursor_protocol import forwarder


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_terminal", [False, True])
async def test_snapshot_at_eof_is_republished_after_server_loses_cache(
    tmp_path, monkeypatch, restart_terminal
):
    epoch = "42080bb7-a540-4242-bb5b-03b4c8ddb131"
    directory = tmp_path / "protocol"
    directory.mkdir()
    event = {
        "root": True,
        "conversationId": "same-chat",
        "kind": "conversation_checkpoint_update",
        "message": {
            "todos": [{"content": "Remember me", "status": "TODO_STATUS_PENDING"}],
        },
    }
    journal = (json.dumps(event) + "\n").encode()
    (directory / f"events.{epoch}.jsonl").write_bytes(journal)
    active_epoch = "42080bb7-a540-4242-bb5b-03b4c8ddb132" if restart_terminal else epoch
    if restart_terminal:
        (directory / f"events.{active_epoch}.jsonl").write_text("")
    (directory / "active.json").write_text(
        json.dumps({"epoch": active_epoch, "conversationId": "same-chat", "pending": []})
    )
    (directory / "forwarder.json").write_text(json.dumps({"epoch": epoch, "offset": len(journal)}))
    delivered = []

    def handle(request):
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(forwarder.httpx, "AsyncClient", lambda **kwargs: client)
    ticks = iter([10, 10, 20, 20])
    monkeypatch.setattr(forwarder, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    async def tick(_):
        if len(delivered) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(forwarder.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_protocol_events(
            base_url="http://test",
            session_id="parent",
            bridge_dir=tmp_path,
            auth=httpx.BasicAuth("", ""),
        )
    assert len(delivered) == 2
    assert delivered[0] == delivered[1]
    assert delivered[0]["data"]["todos"][0]["activeForm"] == "Remember me"


@pytest.mark.asyncio
async def test_child_registration_starts_one_mirror_for_exact_native_store(tmp_path, monkeypatch):
    child = "c0204314-fa2b-4122-ae05-b4ed948a82a5"
    parent_store = tmp_path / "chats" / "workspace" / "parent-native" / "store.db"
    (tmp_path / "cursor_forwarder.json").write_text(json.dumps({"store_path": str(parent_store)}))
    event = {
        "root": True,
        "conversationId": "parent-native",
        "kind": "conversation_checkpoint_update",
        "message": {"subagents": [{"id": child, "title": "Cursor shell"}]},
    }
    supervise = AsyncMock()
    monkeypatch.setattr(transcript, "supervise_cursor_forwarder", supervise)
    registered, tasks = {}, {}

    def handle(request):
        return httpx.Response(200, json={"child_session_id": "omnigent-child"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        for _ in range(2):
            await forwarder.sync_children(
                client, "parent", tmp_path, event, registered, tasks, httpx.BasicAuth("", "")
            )
        await asyncio.gather(*tasks.values())
    supervise.assert_awaited_once()
    assert supervise.call_args.kwargs["session_id"] == "omnigent-child"
    assert (
        supervise.call_args.kwargs["fixed_store_path"]
        == parent_store.parent.parent / child / "store.db"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [False, True])
async def test_fixed_child_store_replays_chat_once_without_workspace_discovery(
    tmp_path, monkeypatch, protocol
):
    native_id = "74d76abd-d2db-4191-97da-98a79caa8ef6"
    store = tmp_path / "chats" / "workspace" / native_id / "store.db"
    store.parent.mkdir(parents=True)
    with sqlite3.connect(store) as db:
        db.execute("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB)")
        for role, text in [("user", "<user_query>Inspect</user_query>"), ("assistant", "Done")]:
            db.execute(
                "INSERT INTO blobs VALUES (?, ?)",
                (
                    role,
                    json.dumps(
                        {
                            "role": role,
                            "content": [{"type": "text", "text": text}],
                        }
                    ),
                ),
            )

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(transcript.asyncio, "to_thread", inline)
    monkeypatch.setattr(transcript.asyncio, "sleep", stop)
    monkeypatch.setattr(
        transcript, "_discover_store", lambda *args: pytest.fail("discovered parent")
    )
    monkeypatch.setattr(
        transcript, "_chat_claimed_by_other", lambda *args: pytest.fail("claimed child")
    )
    monkeypatch.setattr(transcript, "_patch_external_session_id", AsyncMock())
    monkeypatch.setattr(transcript, "_post_model_change_if_new", AsyncMock())
    post = AsyncMock()
    monkeypatch.setattr(transcript, "_post_conversation_item", post)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    if protocol:
        directory = bridge / "protocol"
        directory.mkdir()
        (directory / "enabled").touch()
        (directory / "active.json").write_text(
            json.dumps(
                {
                    "conversationId": native_id,
                    "controlsReady": True,
                    "pid": os.getpid(),
                }
            )
        )
        monkeypatch.setattr(transcript, "_cursor_chats_root", lambda: tmp_path / "chats")
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            await transcript.forward_cursor_store_to_session(
                base_url="http://test",
                headers={},
                session_id="child",
                bridge_dir=bridge,
                agent_name="Cursor shell",
                workspace="",
                launch_epoch_ms=0,
                fixed_store_path=None if protocol else store,
            )
    assert [c.kwargs["item"].item_data["role"] for c in post.call_args_list] == [
        "user",
        "assistant",
    ]
    assert transcript._read_state(bridge).last_rowid == 2


def test_legacy_forwarder_yields_to_native_chat_identity(tmp_path):
    legacy, native = tmp_path / "legacy", tmp_path / "native"
    legacy.mkdir()
    (native / "protocol").mkdir(parents=True)
    chat_id = "74d76abd-d2db-4191-97da-98a79caa8ef6"
    (native / "protocol" / "active.json").write_text(
        json.dumps(
            {
                "conversationId": chat_id,
                "controlsReady": True,
                "pid": os.getpid(),
            }
        )
    )
    assert transcript._chat_claimed_by_other(legacy, tmp_path / chat_id / "store.db", 0)


def test_checkpoint_recovery_crosses_epochs_only_for_same_chat(tmp_path):
    old, new = "old", "new"
    event = {
        "root": True,
        "kind": "conversation_checkpoint_update",
        "conversationId": "same-chat",
        "message": {"todos": []},
    }
    (tmp_path / f"events.{old}.jsonl").write_text(json.dumps(event) + "\n")
    (tmp_path / f"events.{new}.jsonl").write_text("")
    assert forwarder.latest_checkpoint(tmp_path, new, "same-chat") == event
    assert forwarder.latest_checkpoint(tmp_path, new, "different-chat") is None
    assert forwarder.latest_checkpoint(tmp_path, new) is None


@pytest.mark.parametrize("kind", ["command_error", "error"])
def test_async_native_errors_reach_session_status(kind):
    event = {"root": True, "kind": kind, "message": {"message": "Submission failed"}}
    payload = forwarder.event_payload(event)
    assert payload["type"] == "external_session_status"
    assert payload["data"]["status"] == "failed"
    assert payload["data"]["output"] == "Submission failed"


@pytest.mark.asyncio
async def test_error_delivery_retries_without_replacing_todo_snapshot(tmp_path, monkeypatch):
    epoch = "42080bb7-a540-4242-bb5b-03b4c8ddb131"
    directory = tmp_path / "protocol"
    directory.mkdir()
    checkpoint = {
        "root": True,
        "kind": "conversation_checkpoint_update",
        "message": {"todos": [{"content": "Keep me", "status": "TODO_STATUS_PENDING"}]},
    }
    error = {"root": True, "kind": "command_error", "message": {"message": "Network failed"}}
    journal = (json.dumps(checkpoint) + "\n" + json.dumps(error) + "\n").encode()
    (directory / f"events.{epoch}.jsonl").write_bytes(journal)
    (directory / "active.json").write_text(json.dumps({"epoch": epoch, "pending": []}))
    delivered = []
    failures = []

    def handle(request):
        payload = json.loads(request.content)
        if payload["type"] == "external_session_status" and not failures:
            failures.append(payload)
            return httpx.Response(503)
        delivered.append(payload)
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(forwarder.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(forwarder, "time", SimpleNamespace(monotonic=lambda: 10))
    polls = 0

    async def tick(_):
        nonlocal polls
        polls += 1
        if polls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(forwarder.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_protocol_events(
            base_url="http://test",
            session_id="parent",
            bridge_dir=tmp_path,
            auth=httpx.BasicAuth("", ""),
        )
    assert [p["type"] for p in delivered] == [
        "external_session_todos",
        "external_session_status",
        "external_session_todos",
    ]
    assert delivered[1]["data"]["output"] == "Network failed"
    assert json.loads((directory / "forwarder.json").read_text())["offset"] == len(journal)
