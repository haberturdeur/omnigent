"""Browser composer submits directly while Cursor owns follow-ups and steering."""

import re
from pathlib import Path

from playwright.sync_api import Page, Route, expect

from .test_message_render_parity import _ensure_chat_view


def test_cursor_native_queue_bypasses_local_delivery_queue(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    messages = []
    submissions = []
    promotions = []

    def session_snapshot(route: Route) -> None:
        response = route.fetch()
        body = response.json()
        body.update(
            harness="cursor-native",
            status="running",
            pending_inputs=[
                {
                    "pending_id": f"pending-{index}",
                    "content": [{"type": "input_text", "text": text}],
                }
                for index, text in enumerate(submissions)
            ],
        )
        route.fulfill(response=response, json=body)

    def queue_control(route: Route) -> None:
        if route.request.method == "POST":
            promotions.append(route.request.post_data_json)
            messages.pop(0)
            route.fulfill(json={"status": "submitted"})
        else:
            route.fulfill(
                json={
                    "revision": "native-snapshot",
                    "can_steer": True,
                    "messages": [
                        {"index": index, "text": text} for index, text in enumerate(messages)
                    ],
                }
            )

    def submit(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        body = route.request.post_data_json
        assert body["type"] == "message"
        text = body["data"]["content"][0]["text"]
        submissions.append(text)
        messages.append(text)
        route.fulfill(json={"queued": True, "pending_id": "pending-native"})

    page.route(re.compile(rf"/v1/sessions/{session_id}(\?.*)?$"), session_snapshot)
    page.route(f"**/v1/sessions/{session_id}/cursor-queue", queue_control)
    page.route(f"**/v1/sessions/{session_id}/events", submit)

    def send(text: str) -> None:
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_visible()
        composer.fill(text)
        page.get_by_role("button", name="Send", exact=True).click()

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    send("Please inspect the other file instead")
    strip = page.get_by_test_id("cursor-queued-strip")
    expect(strip.get_by_text("Please inspect the other file instead")).to_be_visible()
    send("Run the tests afterwards")
    expect(strip.get_by_text("Run the tests afterwards")).to_be_visible()
    assert submissions == ["Please inspect the other file instead", "Run the tests afterwards"]
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)
    expect(page.get_by_text("Please inspect the other file instead", exact=True)).to_have_count(1)
    page.screenshot(path=str(tmp_path / "cursor-native-queue.png"), full_page=True)
    strip.get_by_role("button", name="Steer queued message").click()
    expect(strip.get_by_text("Please inspect the other file instead")).to_have_count(0)
    assert promotions == [{"action": "steer_queue", "revision": "native-snapshot", "index": 0}]
    assert len(submissions) == 2
    page.reload()
    _ensure_chat_view(page)
    expect(strip.get_by_text("Run the tests afterwards")).to_be_visible()
    expect(page.get_by_text("Run the tests afterwards", exact=True)).to_have_count(1)
    expect(page.get_by_text("Please inspect the other file instead", exact=True)).to_have_count(0)
    messages.clear()
    expect(strip).to_have_count(0)
