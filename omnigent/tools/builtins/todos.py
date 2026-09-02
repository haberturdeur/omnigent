"""Tool for publishing the agent's todo list to the Omnigent session."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# Statuses the session's todo panel renders. Mirrors the shape the native
# forwarders post as ``external_session_todos`` (see the pi extension's
# publishTaskList), so a tool-driven list and a TUI-scraped one are the same
# thing to the web UI.
TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")


class SysTodoWriteTool(Tool):
    """Schema-only tool that replaces the calling session's todo list.

    Vendors differ on whether a plan is readable: the native TUIs expose their
    task list to a forwarder, while the Copilot SDK only announces *that* its
    plan file changed. Giving the agent a tool sidesteps that entirely — any
    harness that receives Omnigent tools can drive the same panel, with no
    vendor file to parse and nothing to keep in sync when a vendor changes
    format.
    """

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_todo_write"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Publish or update your todo list for this session, so the user can "
            "follow multi-step work. Send the COMPLETE list every time — it "
            "replaces the previous one. Keep exactly one item 'in_progress'. "
            "Use it for multi-step tasks; skip it for a single trivial step."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "description": (
                                "The complete todo list, in order. Replaces the previous list."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {
                                        "type": "string",
                                        "description": (
                                            "The step, imperative and short, for "
                                            "example 'Add the migration'."
                                        ),
                                        "minLength": 1,
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": list(TODO_STATUSES),
                                        "description": "This step's current state.",
                                    },
                                    "activeForm": {
                                        "type": "string",
                                        "description": (
                                            "Present-continuous form shown while the "
                                            "step runs, for example 'Adding the "
                                            "migration'. Defaults to 'content'."
                                        ),
                                    },
                                },
                                "required": ["content", "status"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["todos"],
                    "additionalProperties": False,
                },
            },
        }
