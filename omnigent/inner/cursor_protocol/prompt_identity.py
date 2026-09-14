"""Correlate native Cursor prompts with their web pending-input metadata."""

import re

_MARKER = re.compile(r"\s*<!-- omnigent-input:(pending_[0-9a-f]{32}) -->\s*$")


def input_marker(pending_id: str) -> str:
    """Return the marker carried through Cursor's string-only prompt queue."""
    return f"<!-- omnigent-input:{pending_id} -->"


def split_input_marker(text: str) -> tuple[str, str | None]:
    """Separate a trailing correlation marker from the displayed prompt."""
    match = _MARKER.search(text)
    if match is None:
        return text, None
    return text[: match.start()], match.group(1)
