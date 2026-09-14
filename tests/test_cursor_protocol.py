"""Native TUI launch and side-channel delivery contracts."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


def _fake_install(tmp_path: Path, version: str) -> Path:
    install = tmp_path / version
    install.mkdir()
    for name in ("cursor-agent", "node", "index.js"):
        (install / name).touch()
    return install


def test_shell_approval_has_readable_message_and_command_preview():
    from omnigent.inner.cursor_protocol.forwarder import decision_payload

    payload = decision_payload(
        {
            "kind": "tool",
            "args": {
                "type": "shell",
                "toolCallId": "call",
                "details": {"command": "git status"},
            },
        },
        "card",
    )
    assert payload["message"] == "Cursor wants to run shell"
    assert payload["content_preview"] == "git status"
    assert payload["operation_type"] == "shell"


def test_live_cursor_approval_mode_preserves_questions():
    from omnigent.inner.cursor_protocol.forwarder import pending_decisions

    active = {"pending": [{"id": "tool", "kind": "tool"}, {"id": "question", "kind": "question"}]}
    assert not any(d["auto_accept"] for d in pending_decisions(active, "epoch").values())
    assert [
        d["auto_accept"]
        for d in pending_decisions({**active, "runEverything": True}, "epoch").values()
    ] == [True, False]


@pytest.mark.asyncio
async def test_run_everything_uses_native_reply_without_web_approval(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from omnigent.inner.cursor_protocol import forwarder

    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(forwarder.asyncio, "to_thread", inline)
    send = Mock()
    monkeypatch.setattr(forwarder, "send_control", send)
    client = Mock(post=AsyncMock())
    await forwarder.answer_decision(
        client,
        "session",
        tmp_path,
        {"id": "tool", "epoch": "generation", "auto_accept": True},
        "card",
    )
    client.post.assert_not_called()
    assert send.call_args.args[1] == {
        "action": "reply",
        "id": "tool",
        "outcome": "accept",
        "expected_epoch": "generation",
        "require_run_everything": True,
    }


def test_protocol_launch_preserves_tui_arguments(tmp_path: Path) -> None:
    from omnigent.inner.cursor_protocol.transport import SUPPORTED_VERSION, prepare_launch

    install = _fake_install(tmp_path, SUPPORTED_VERSION)
    binary, args = prepare_launch(
        str(install / "cursor-agent"),
        ["--resume", "chat", "--model", "chosen"],
        tmp_path / "bridge",
    )
    assert binary == str(install / "node")
    assert args[-5:] == ["--", "--resume", "chat", "--model", "chosen"]
    assert "acp" not in args


def test_native_todos_decode_enum_and_ignore_child_checkpoints() -> None:
    from omnigent.inner.cursor_protocol.forwarder import event_payload

    event = {
        "root": True,
        "kind": "conversation_checkpoint_update",
        "message": {
            "todos": [
                {"id": "a", "content": "Work", "status": "TODO_STATUS_IN_PROGRESS"},
                {"id": "b", "content": "Dropped", "status": "TODO_STATUS_CANCELLED"},
            ]
        },
    }
    assert event_payload(event) == {
        "type": "external_session_todos",
        "data": {"todos": [{"content": "Work", "activeForm": "Work", "status": "in_progress"}]},
    }
    assert event_payload({**event, "root": False}) is None


def test_native_subagents_use_native_registration_event() -> None:
    from omnigent.inner.cursor_protocol.forwarder import subagent_payloads

    event = {
        "root": True,
        "kind": "conversation_checkpoint_update",
        "message": {"subagents": [{"id": "native-child", "title": "Cursor shell"}]},
    }
    assert subagent_payloads(event) == [
        {
            "type": "external_cursor_subagent_start",
            "data": {"subagent_id": "native-child", "title": "Cursor shell"},
        }
    ]
    assert subagent_payloads({**event, "root": False}) == []


def test_interrupt_uses_native_channel_without_sending_escape(tmp_path: Path, monkeypatch) -> None:
    from omnigent import cursor_native_bridge
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setattr(transport, "cancel_active_run", lambda _: True)
    monkeypatch.setattr(
        cursor_native_bridge,
        "_wait_for_tmux_info",
        lambda *a, **k: {
            "socket_path": "socket",
            "tmux_target": "pane",
        },
    )
    calls = []
    monkeypatch.setattr(cursor_native_bridge, "_run_tmux", lambda *args: calls.append(args))
    monkeypatch.setattr(cursor_native_bridge, "_wait_for_pane_settle", lambda *a, **k: None)
    monkeypatch.setattr(cursor_native_bridge, "_clear_composer", lambda *a, **k: None)
    cursor_native_bridge.inject_interrupt(tmp_path)
    assert not calls


def test_native_prompt_never_uses_tmux_or_retries_uncertain_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    from omnigent import cursor_native_bridge
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setattr(transport, "enabled", lambda _: True)
    send = Mock(return_value={"ok": True})
    monkeypatch.setattr(transport, "send_control", send)
    tmux = Mock(side_effect=AssertionError("Native protocol must not access tmux"))
    monkeypatch.setattr(cursor_native_bridge, "_wait_for_tmux_info", tmux)
    cursor_native_bridge.inject_user_message(tmp_path, content="first\nmessage")
    assert send.call_args.args[1] == {"action": "prompt", "content": "first\nmessage"}
    send.side_effect = RuntimeError("delivery is uncertain")
    with pytest.raises(RuntimeError, match="uncertain"):
        cursor_native_bridge.inject_user_message(tmp_path, content="second")
    assert send.call_count == 2
    tmux.assert_not_called()


def test_native_compact_and_model_do_not_type_slash_commands(tmp_path: Path, monkeypatch) -> None:
    from omnigent import cursor_native_bridge
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setattr(transport, "enabled", lambda _: True)
    send = Mock(return_value={"ok": True})
    monkeypatch.setattr(transport, "send_control", send)
    cursor_native_bridge.inject_user_message(tmp_path, content="/summarize")
    assert send.call_args.args[1] == {"action": "compact"}
    cursor_native_bridge.inject_model_command(tmp_path, model="selected")
    assert send.call_args.args[1] == {"action": "model", "model": "selected"}


def test_native_decision_payload_preserves_questions_and_rejection_ownership() -> None:
    from omnigent.inner.cursor_protocol.forwarder import decision_payload

    args = {
        "title": "Choose",
        "questions": [{"id": "q", "prompt": "Select", "options": [{"id": "a", "label": "Alpha"}]}],
    }
    payload = decision_payload({"kind": "question", "args": args}, "card")
    assert payload["native_control"] is True
    assert payload["ask_user_question"]["questions"][0]["id"] == "q"
    assert payload["ask_user_question"]["questions"][0]["options"] == [{"label": "Alpha"}]


def test_protocol_launch_rejects_unsupported_or_incomplete_install(tmp_path):
    from omnigent.inner.cursor_protocol.transport import SUPPORTED_VERSION, prepare_launch

    with pytest.raises(RuntimeError, match="standalone CLI"):
        prepare_launch(str(tmp_path / "other" / "cursor-agent"), [], tmp_path / "bridge")
    install = tmp_path / SUPPORTED_VERSION
    install.mkdir()
    (install / "cursor-agent").touch()
    with pytest.raises(RuntimeError, match="standalone CLI"):
        prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")


def test_unpinned_build_is_probed_once_and_cached(tmp_path, monkeypatch):
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    install = _fake_install(tmp_path, "2099.01.01-abcdef0")
    probes = []
    monkeypatch.setattr(transport, "_probe", lambda path: probes.append(path))
    binary, args = transport.prepare_launch(
        str(install / "cursor-agent"), ["--resume", "chat"], tmp_path / "bridge"
    )
    assert binary == str(install / "node")
    assert args[-3:] == ["--", "--resume", "chat"]
    assert probes == [install]
    # A second launch of the same bundle trusts the cached verdict.
    transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert probes == [install]
    # Rebuilding the bundle in place invalidates it.
    (install / "index.js").write_text("changed")
    transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert probes == [install, install]


def test_incompatible_build_is_rejected_from_cache_without_reprobing(tmp_path, monkeypatch):
    """An undrivable build must not reload the whole bundle on every launch."""
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    install = _fake_install(tmp_path, "2099.01.02-abcdef0")
    probes = []

    def reject(path):
        probes.append(path)
        return "Cursor bundle is missing the native protocol interfaces"

    monkeypatch.setattr(transport, "_probe", reject)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="missing the native protocol interfaces"):
            transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert probes == [install]


def test_a_changed_bridge_reprobes_an_already_verified_build(tmp_path, monkeypatch):
    """A verdict is about this bridge, so an Omnigent upgrade must not inherit it."""
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    install = _fake_install(tmp_path, "2099.03.01-abcdef0")
    probes = []
    monkeypatch.setattr(transport, "_probe", lambda path: probes.append(path))
    monkeypatch.setattr(transport, "_bridge_fingerprint", lambda: "bridge-one")
    for _ in range(2):
        transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert probes == [install]
    monkeypatch.setattr(transport, "_bridge_fingerprint", lambda: "bridge-two")
    transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert probes == [install, install]


def test_bridge_fingerprint_follows_the_content_of_both_patched_scripts(tmp_path, monkeypatch):
    from omnigent.inner.cursor_protocol import transport

    package = tmp_path / "cursor_protocol"
    package.mkdir()
    (package / "bridge.cjs").write_text("bridge")
    (package / "controls.cjs").write_text("controls")
    monkeypatch.setattr(transport, "__file__", str(package / "transport.py"))
    original = transport._bridge_fingerprint()
    assert original == transport._bridge_fingerprint()
    (package / "controls.cjs").write_text("controls, rewritten")
    assert transport._bridge_fingerprint() != original


def test_a_probe_that_could_not_run_leaves_the_build_unjudged(tmp_path, monkeypatch):
    from omnigent.inner.cursor_protocol import transport

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "state"))
    install = _fake_install(tmp_path, "2099.01.03-abcdef0")

    def unavailable(_path):
        raise RuntimeError("Cursor native protocol probe failed to run: boom")

    monkeypatch.setattr(transport, "_probe", unavailable)
    with pytest.raises(RuntimeError, match="failed to run"):
        transport.prepare_launch(str(install / "cursor-agent"), [], tmp_path / "bridge")
    assert transport._cached_verdicts() == []


def test_probe_drives_the_bridge_against_the_installed_bundle(tmp_path, monkeypatch):
    from omnigent.inner.cursor_protocol import transport

    install = _fake_install(tmp_path, "2099.02.01-abcdef0")
    seen = {}

    def ready(argv, **kwargs):
        seen.update(argv=argv, timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, '{"version": "x", "ready": true}\n', "")

    monkeypatch.setattr(transport.subprocess, "run", ready)
    assert transport._probe(install) is None
    assert seen["argv"][0] == str(install / "node")
    assert seen["argv"][1].endswith("bridge.cjs")
    assert seen["argv"][2:] == [str(install / "index.js"), "--probe"]
    assert seen["timeout"] == transport.PROBE_TIMEOUT_S


def test_probe_reports_the_bridge_diagnostic_a_timeout_and_a_failed_spawn(tmp_path, monkeypatch):
    from omnigent.inner.cursor_protocol import transport

    install = _fake_install(tmp_path, "2099.02.02-abcdef0")

    def rejected(argv, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "node:internal/modules/cjs/loader:1219\n"
            "Cursor native bridge: Cursor bundle chunk 7569 is missing: boom\n",
        )

    monkeypatch.setattr(transport.subprocess, "run", rejected)
    assert transport._probe(install) == "Cursor bundle chunk 7569 is missing: boom"

    def hangs(argv, **kwargs):
        del kwargs
        raise subprocess.TimeoutExpired(argv, transport.PROBE_TIMEOUT_S)

    monkeypatch.setattr(transport.subprocess, "run", hangs)
    assert "did not finish loading" in str(transport._probe(install))

    def missing(argv, **kwargs):
        del argv, kwargs
        raise OSError("node is not executable")

    monkeypatch.setattr(transport.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="failed to run"):
        transport._probe(install)


@pytest.mark.asyncio
async def test_protocol_forwarder_stops_when_the_protocol_is_off(tmp_path):
    """A fallback session has no journal, so the forwarder must not poll for one."""
    from omnigent.inner.cursor_protocol.forwarder import forward_protocol_events

    await asyncio.wait_for(
        forward_protocol_events(
            base_url="http://test-server",
            session_id="c42dbcb16fd3a87ee8f5d1fe4cabfdf8",
            bridge_dir=tmp_path / "bridge",
            auth=Mock(),
        ),
        timeout=5,
    )
