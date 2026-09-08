"""Private file channel beside the existing Cursor-native tmux bridge."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, TypeAlias

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

LAUNCH_FLAG = "--omnigent-native-protocol"
SUPPORTED_VERSION = "2026.09.02-c22c1a3"


def prepare_launch(executable: str, args: list[str], bridge_dir: Path) -> tuple[str, list[str]]:
    """Wrap the installed runtime while preserving Cursor's interactive argv."""
    install = Path(executable).resolve().parent
    if install.name != SUPPORTED_VERSION:
        raise RuntimeError(f"Cursor native protocol requires CLI {SUPPORTED_VERSION}")
    if not (install / "node").is_file() or not (install / "index.js").is_file():
        raise RuntimeError("Cursor native protocol requires the standalone CLI installation")
    directory = bridge_dir / "protocol"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    (directory / "enabled").touch(mode=0o600)
    return str(install / "node"), [
        str(Path(__file__).with_name("bridge.cjs")),
        str(install / "index.js"),
        str(directory),
        "--",
        *args,
    ]


def enabled(bridge_dir: Path) -> bool:
    return (bridge_dir / "protocol" / "enabled").exists()


def send_control(bridge_dir: Path, payload: Json, *, timeout: float = 10) -> Json:
    """Dispatch once; an uncertain acknowledgement must never cause a keypress retry."""
    directory = bridge_dir / "protocol"
    request: Path | None = None
    response: Path | None = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                state = json.loads((directory / "active.json").read_text())
            except FileNotFoundError:
                state = None
            if state is not None:
                if payload.get("expected_epoch") not in (None, state.get("epoch")):
                    raise RuntimeError("Cursor terminal was restarted; this decision is stale")
                if state.get("controlsReady"):
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError(
                "Cursor native controls are not ready; command was not sent. Check the terminal."
            )
        request_id = uuid.uuid4().hex
        request = directory / f"request.{request_id}.json"
        response = directory / f"response.{request_id}.json"
        temporary = directory / f".{request_id}.tmp"
        with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(
                {
                    **payload,
                    "epoch": state["epoch"],
                    "deadline": (time.time() + max(0, deadline - time.monotonic())) * 1000,
                },
                stream,
            )
        temporary.replace(request)
        while time.monotonic() < deadline:
            if response.exists():
                result = json.loads(response.read_text())
                if result.get("ok") is not True:
                    raise RuntimeError(result.get("error", "Cursor rejected the native control"))
                return result
            time.sleep(0.05)
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError("Cursor native control channel is unavailable") from exc
    finally:
        if request is not None:
            request.unlink(missing_ok=True)
        if response is not None:
            response.unlink(missing_ok=True)
    raise RuntimeError(
        "Cursor control acknowledgement timed out; delivery is uncertain. "
        "Check the terminal before resending."
    )


def cancel_active_run(bridge_dir: Path, *, timeout: float = 2) -> bool:
    if not enabled(bridge_dir):
        return False
    send_control(bridge_dir, {"action": "cancel"}, timeout=timeout)
    return True


def read_approval_config(bridge_dir: Path) -> Json:
    """Read the effective mode from the live Cursor process, without cached flags."""
    result = send_control(bridge_dir, {"action": "get_approval_config"}, timeout=2)
    mode = result.get("approval_mode")
    if mode not in ("allowlist", "unrestricted", "auto-review"):
        raise RuntimeError("Cursor returned an unsupported approval mode")
    return {
        "approval_mode": mode,
        "run_everything": mode == "unrestricted",
        "source": "cursor-runtime",
    }
