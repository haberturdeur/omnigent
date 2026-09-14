"""Private file channel beside the existing Cursor-native tmux bridge."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, TypeAlias

Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

_logger = logging.getLogger(__name__)

LAUNCH_FLAG = "--omnigent-native-protocol"
# Build validated by hand against the current bridge, so it skips the probe;
# re-check it when bridge.cjs changes. The CLI updates itself, so any other
# build is probed once and cached rather than refused outright.
SUPPORTED_VERSION = "2026.09.08-6caf4ff"
PROBE_TIMEOUT_S = 120
PROBE_CACHE_NAME = "cursor_protocol_probe.json"
PROBE_CACHE_LIMIT = 8


def _probe_cache_path() -> Path:
    from omnigent.install_ledger import state_dir

    return state_dir() / PROBE_CACHE_NAME


def _bridge_fingerprint() -> str:
    """Hash the bridge's own patch surface.

    A verdict says the bridge could drive the bundle, so an Omnigent upgrade
    that patches different internals invalidates it just as a rebuilt bundle
    does. Content, not stat: the same bridge in a fresh checkout must match.
    """
    digest = hashlib.sha256()
    try:
        for name in ("bridge.cjs", "controls.cjs"):
            digest.update(Path(__file__).with_name(name).read_bytes())
    except OSError as exc:
        raise RuntimeError(f"Cursor native protocol bridge is unreadable: {exc}") from exc
    return digest.hexdigest()[:16]


def _fingerprint(install: Path) -> str:
    """Identify the build and the bridge, so a change to either is re-probed."""
    stat = (install / "index.js").stat()
    return f"{install.name}:{stat.st_size}:{stat.st_mtime_ns}:{_bridge_fingerprint()}"


def _cached_verdicts() -> list[Json]:
    """Read the recorded verdicts, most recent first; a bad cache just re-probes."""
    try:
        cached = json.loads(_probe_cache_path().read_text())
    except (OSError, ValueError):
        return []
    builds = cached.get("builds") if isinstance(cached, dict) else None
    if not isinstance(builds, list):
        return []
    return [b for b in builds if isinstance(b, dict) and isinstance(b.get("id"), str)]


def _cached_verdict(fingerprint: str) -> Json | None:
    return next((b for b in _cached_verdicts() if b["id"] == fingerprint), None)


def _record_verdict(fingerprint: str, reason: str | None) -> None:
    """Remember both outcomes so an undrivable build is probed once, not per launch."""
    verdict: Json = {"id": fingerprint, "ok": reason is None}
    if reason is not None:
        verdict["reason"] = reason
    builds = [verdict, *(b for b in _cached_verdicts() if b["id"] != fingerprint)]
    path = _probe_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"builds": builds[:PROBE_CACHE_LIMIT]}))
        temporary.replace(path)
    except OSError:
        _logger.debug("Could not persist the Cursor protocol probe result", exc_info=True)


def _probe(install: Path) -> str | None:
    """Load the bundle and touch every interface the bridge patches.

    :param install: The resolved standalone CLI directory.
    :returns: ``None`` when the bridge can drive the build, else the reason it
        cannot. A bundle that never finishes loading is a property of the build,
        so the timeout is a verdict rather than an error.
    :raises RuntimeError: If the probe could not be started at all, which leaves
        the build's compatibility unknown instead of decided.
    """
    try:
        result = subprocess.run(
            [
                str(install / "node"),
                str(Path(__file__).with_name("bridge.cjs")),
                str(install / "index.js"),
                "--probe",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"the bundle did not finish loading within {PROBE_TIMEOUT_S}s"
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Cursor native protocol probe failed to run: {exc}") from exc
    if result.returncode != 0 or '"ready":true' not in result.stdout.replace(" ", ""):
        return _probe_reason(result.stderr or result.stdout)
    return None


def _probe_reason(output: str) -> str:
    """Pick the bridge's own diagnostic out of a require stack or traceback."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    prefix = "Cursor native bridge:"
    reported = next((line for line in lines if line.startswith(prefix)), None)
    if reported is not None:
        return reported[len(prefix) :].strip()
    return lines[0] if lines else "probe reported no result"


def _incompatible(install: Path, reason: str) -> str:
    return f"Cursor native protocol is not compatible with CLI {install.name}: {reason}"


def verify_install(install: Path) -> None:
    """Raise unless the bridge is known to drive this CLI build.

    Probing loads the whole Cursor bundle, so callers must not run this on an
    event loop; it is cheap only on the second and later launches of a build.

    :param install: The resolved standalone CLI directory.
    :raises RuntimeError: If the installation is incomplete or the build's
        native interfaces do not match what the bridge patches.
    """
    if not (install / "node").is_file() or not (install / "index.js").is_file():
        raise RuntimeError("Cursor native protocol requires the standalone CLI installation")
    if install.name == SUPPORTED_VERSION:
        return
    fingerprint = _fingerprint(install)
    cached = _cached_verdict(fingerprint)
    if cached is not None:
        if cached.get("ok") is True:
            return
        raise RuntimeError(_incompatible(install, str(cached.get("reason") or "unknown")))
    reason = _probe(install)
    _record_verdict(fingerprint, reason)
    if reason is not None:
        raise RuntimeError(_incompatible(install, reason))
    _logger.info("Cursor native protocol validated CLI %s", install.name)


def prepare_launch(executable: str, args: list[str], bridge_dir: Path) -> tuple[str, list[str]]:
    """Wrap the installed runtime while preserving Cursor's interactive argv."""
    install = Path(executable).resolve().parent
    verify_install(install)
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


def disable(bridge_dir: Path) -> None:
    """Clear a marker left by an earlier launch so consumers use the tmux path."""
    (bridge_dir / "protocol" / "enabled").unlink(missing_ok=True)


def _terminal_alive(pid: object) -> bool:
    """Whether the terminal that published a state file is still running.

    State written before the pid was recorded counts as live, since an absent
    pid says nothing either way.
    """
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


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
                # A bridge killed by a signal never runs its exit hook, so
                # ``controlsReady`` outlives it. Say the terminal is gone rather
                # than post a request nothing will ever answer.
                if not _terminal_alive(state.get("pid")):
                    raise RuntimeError(
                        "cursor terminal is no longer running (the TUI exited); "
                        "restart the session"
                    )
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
