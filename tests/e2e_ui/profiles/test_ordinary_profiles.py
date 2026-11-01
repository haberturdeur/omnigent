"""Profile selection survives navigation and separates sidebar projects."""

import uuid

import httpx
from playwright.sync_api import Page, expect


def test_create_switch_and_open_profile_tab(page: Page, seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    name = f"Work-{uuid.uuid4().hex[:8]}"
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_role("button", name="Create profile", exact=True).click()
    dialog = page.get_by_role("dialog", name="Create profile")
    dialog.get_by_label("Name", exact=True).fill(name)
    dialog.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_label("Profile", exact=True).locator("option:checked")).to_have_text(name)
    profile_id = page.get_by_label("Profile", exact=True).input_value()
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name, "profile_id": profile_id})
    response.raise_for_status()
    page.reload()
    expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    with page.expect_popup() as popup:
        page.get_by_role("link", name="Open profile in new tab").click()
    other = popup.value
    expect(other.get_by_label("Profile", exact=True)).to_have_value(profile_id)
    page.get_by_label("Profile", exact=True).select_option(label="Personal")
    expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    expect(other.get_by_label("Profile", exact=True)).to_have_value(profile_id)
    other.close()
