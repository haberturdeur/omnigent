"""Native controls wait for startup without replaying uncertain deliveries."""

import json
import subprocess
import sys
import time

import pytest

from omnigent.inner.cursor_protocol import transport


def _reaped_pid() -> int:
    """A pid that has certainly exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


@pytest.mark.parametrize("initial", [None, {"epoch": "launch", "controlsReady": False}])
def test_prompt_waits_for_native_startup(tmp_path, monkeypatch, initial):
    directory = tmp_path / "protocol"
    directory.mkdir()
    active = directory / "active.json"
    if initial is not None:
        active.write_text(json.dumps(initial))
    delivered = []

    def tick(_):
        requests = list(directory.glob("request.*.json"))
        if requests:
            request = requests[0]
            delivered.append(json.loads(request.read_text()))
            request.with_name(request.name.replace("request.", "response.")).write_text(
                json.dumps({"ok": True})
            )
        else:
            active.write_text(json.dumps({"epoch": "launch", "controlsReady": True}))

    monkeypatch.setattr(transport.time, "sleep", tick)
    assert transport.send_control(tmp_path, {"action": "prompt", "content": "hello"}) == {
        "ok": True
    }
    assert len(delivered) == 1
    assert delivered[0]["content"] == "hello"
    assert not list(directory.glob("request.*.json"))


def test_startup_timeout_does_not_dispatch(tmp_path, monkeypatch):
    directory = tmp_path / "protocol"
    directory.mkdir()
    clock = iter([0, 0, 2])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match=r"not ready.*not sent"):
        transport.send_control(tmp_path, {"action": "prompt", "content": "hello"}, timeout=1)
    assert not list(directory.glob("request.*.json"))


def test_stale_decision_is_not_sent_to_restarted_terminal(tmp_path):
    directory = tmp_path / "protocol"
    directory.mkdir()
    (directory / "active.json").write_text(json.dumps({"epoch": "new", "controlsReady": False}))
    with pytest.raises(RuntimeError, match="stale"):
        transport.send_control(tmp_path, {"action": "reply", "expected_epoch": "old"})
    assert not list(directory.glob("request.*.json"))


def test_controls_report_a_dead_terminal_instead_of_waiting_on_its_leftovers(tmp_path):
    """A signalled bridge never clears ``controlsReady``; callers must not hang.

    Killing the host leaves that state behind, so every queued message would
    post a request nothing answers and wait out the full timeout.
    """
    directory = tmp_path / "protocol"
    directory.mkdir()
    (directory / "active.json").write_text(
        json.dumps({"epoch": "launch", "pid": _reaped_pid(), "controlsReady": True})
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="no longer running"):
        transport.send_control(tmp_path, {"action": "prompt", "content": "hello"}, timeout=10)
    assert time.monotonic() - started < 2
    assert not list(directory.glob("request.*.json"))


def test_acknowledgement_timeout_does_not_resend(tmp_path, monkeypatch):
    directory = tmp_path / "protocol"
    directory.mkdir()
    (directory / "active.json").write_text(json.dumps({"epoch": "launch", "controlsReady": True}))
    now = [0.0]
    requests = set()

    def tick(delay):
        requests.update(directory.glob("request.*.json"))
        now[0] += delay

    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transport.time, "sleep", tick)
    with pytest.raises(RuntimeError, match="delivery is uncertain"):
        transport.send_control(tmp_path, {"action": "prompt", "content": "hello"}, timeout=0.2)
    assert len(requests) == 1
    assert not list(directory.glob("request.*.json"))
