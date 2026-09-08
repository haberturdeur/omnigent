"""Native Cursor payloads must survive the server's validation contract."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.inner.cursor_protocol.forwarder import event_payload, subagent_payloads
from omnigent.server.routes._sessions import helpers
from omnigent.server.schemas import SessionEventInput


def test_cursor_todos_survive_server_filter(monkeypatch):
    cache = {}
    publish = Mock()
    monkeypatch.setattr(helpers, "_session_todos_cache", cache)
    monkeypatch.setattr(helpers.session_stream, "publish", publish)
    payload = event_payload(
        {
            "root": True,
            "kind": "conversation_checkpoint_update",
            "message": {
                "todos": [{"content": "Native task", "status": "TODO_STATUS_IN_PROGRESS"}]
            },
        }
    )
    helpers._handle_external_session_todos("parent", SessionEventInput.model_validate(payload))
    assert cache["parent"] == [
        {"content": "Native task", "activeForm": "Native task", "status": "in_progress"}
    ]
    assert publish.call_args.args[1]["todos"] == cache["parent"]


@pytest.mark.asyncio
async def test_cursor_children_register_once_with_native_labels(monkeypatch):
    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(helpers.asyncio, "to_thread", inline_thread)
    children = []
    store = Mock()
    store.list_conversations.side_effect = lambda **kwargs: SimpleNamespace(
        data=children, has_more=False, last_id=None
    )

    def create(**kwargs):
        child = SimpleNamespace(id="child", labels={}, **kwargs)
        children.append(child)
        return child

    store.create_conversation.side_effect = create
    store.set_labels.side_effect = lambda child_id, labels: children[0].labels.update(labels)
    publish = Mock()
    monkeypatch.setattr(helpers, "_publish_session_created", publish)
    parent = SimpleNamespace(agent_id="cursor-agent", runner_id="runner")
    payload = subagent_payloads(
        {
            "root": True,
            "kind": "conversation_checkpoint_update",
            "message": {"subagents": [{"id": "native-child", "title": "Cursor shell"}]},
        }
    )[0]
    for _ in range(2):
        assert (
            await helpers._persist_external_acp_subagent_start(
                "parent",
                parent,
                SessionEventInput.model_validate(payload),
                store,
                namespace="cursor",
            )
            == "child"
        )
    store.create_conversation.assert_called_once()
    publish.assert_called_once_with("parent", "child", "cursor-agent")
    assert children[0].parent_conversation_id == "parent"
    assert children[0].labels["omnigent.cursor.subagent_id"] == "native-child"
    assert "omnigent.wrapper" not in children[0].labels
