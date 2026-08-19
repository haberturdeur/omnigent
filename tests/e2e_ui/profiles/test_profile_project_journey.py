"""Browser journey for profile switching, project movement, and locking."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect


def _switch_profile(page: Page, name: str) -> None:
    """Choose a profile from the sidebar switcher."""
    page.get_by_test_id("profile-switcher").click()
    page.get_by_role("menuitem", name=name, exact=True).click()


def test_switch_move_and_unlock_profile_journey(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """The UI moves a project with history and prompts before a private switch."""
    base_url, session_id = seeded_session
    suffix = uuid.uuid4().hex[:8]
    work_name = f"Work {suffix}"
    private_name = f"Private {suffix}"
    project_name = f"Project {suffix}"
    session_title = f"profile-journey-{suffix}"
    history_marker = f"retained-history-{suffix}"
    private_root = tmp_path / "private-root"

    profiles_response = httpx.get(f"{base_url}/v1/profiles", timeout=10.0)
    profiles_response.raise_for_status()
    personal_id = next(
        profile["id"] for profile in profiles_response.json()["data"] if profile["is_default"]
    )
    work_response = httpx.post(
        f"{base_url}/v1/profiles",
        json={"name": work_name},
        timeout=10.0,
    )
    work_response.raise_for_status()
    work_id = work_response.json()["id"]
    private_response = httpx.post(
        f"{base_url}/v1/profiles",
        json={
            "name": private_name,
            "config": {"workspace": str(private_root)},
        },
        timeout=10.0,
    )
    private_response.raise_for_status()
    private_id = private_response.json()["id"]
    protection_response = httpx.put(
        f"{base_url}/v1/profiles/{private_id}/protection",
        json={
            "passcode": "browser-passcode",
            "protected_roots": [str(private_root)],
        },
        timeout=10.0,
    )
    protection_response.raise_for_status()

    project_response = httpx.post(
        f"{base_url}/v1/projects",
        json={"name": project_name, "profile_id": personal_id},
        timeout=10.0,
    )
    project_response.raise_for_status()
    project_id = project_response.json()["id"]
    session_response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={
            "title": session_title,
            "profile_id": personal_id,
            "project_id": project_id,
        },
        timeout=10.0,
    )
    session_response.raise_for_status()
    history_response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "hello_world", "text": history_marker},
        },
        timeout=10.0,
    )
    history_response.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("profile-switcher")).to_contain_text("Personal")
    expect(page.get_by_text(history_marker, exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name=project_name, exact=True)).to_be_visible()

    _switch_profile(page, work_name)
    expect(page.get_by_test_id("profile-switcher")).to_contain_text(work_name)
    expect(page.get_by_role("button", name=project_name, exact=True)).to_have_count(0)

    _switch_profile(page, "Personal")
    project_header = page.get_by_role("button", name=project_name, exact=True)
    expect(project_header).to_be_visible()
    project_header.hover()
    page.get_by_role("button", name=f"Project actions for {project_name}").click()
    page.get_by_test_id("move-project").click()

    move_dialog = page.get_by_role("dialog", name="Move project to profile")
    expect(move_dialog).to_be_visible()
    move_dialog.get_by_role("button", name=re.compile(rf"^{re.escape(work_name)}")).click()
    move_dialog.get_by_test_id("move-project-confirm").click()
    expect(move_dialog).not_to_be_visible(timeout=15_000)
    expect(page.get_by_role("button", name=project_name, exact=True)).to_have_count(0)

    _switch_profile(page, work_name)
    moved_project = page.get_by_role("button", name=project_name, exact=True)
    expect(moved_project).to_be_visible(timeout=15_000)
    if moved_project.get_attribute("aria-expanded") != "true":
        moved_project.click()
    session_link = page.locator(f'a[href="/c/{session_id}"]')
    expect(session_link).to_be_visible()
    session_link.click()
    expect(page.get_by_text(history_marker, exact=True)).to_be_visible(timeout=30_000)

    _switch_profile(page, private_name)
    unlock_dialog = page.get_by_role("dialog", name="Unlock private profile")
    expect(unlock_dialog).to_be_visible()
    expect(page.get_by_test_id("profile-switcher")).to_contain_text(work_name)
    unlock_dialog.get_by_placeholder("Passcode").fill("browser-passcode")
    unlock_dialog.get_by_role("button", name="Unlock", exact=True).click()
    expect(unlock_dialog).not_to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("profile-switcher")).to_contain_text(private_name)

    moved_snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    moved_snapshot.raise_for_status()
    assert moved_snapshot.json()["profile_id"] == work_id
    assert moved_snapshot.json()["project_id"] == project_id
