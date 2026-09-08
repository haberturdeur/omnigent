# Cursor native controls (experimental)

This extends **Cursor Native** (`cursor-native`). Cursor's normal interactive
TUI runs in Omnigent's tmux terminal. Its native runtime sends `agent.v1`
protobuf messages over its existing backend connection. ACP is not launched.

Every Cursor session uses the native bridge, including sessions created from
the web UI. Standalone Cursor CLI **2026.09.02-c22c1a3** is required:

```sh
omnigent cursor
```

The old `--omnigent-native-protocol` passthrough flag is accepted for existing
launch configurations but is no longer necessary. Unsupported or incomplete
CLI installations fail at startup. Already-running terminals keep their launch
mode; recreate the terminal to activate the bridge after upgrading Omnigent.

For a source checkout, install the supervisor with
`cargo install --path dev/omnidev --force`, then start `omnidev`. In a second
terminal in the same checkout, run:

```sh
omnidev omnigent cursor -- --omnigent-native-protocol
```

Cursor uses the supervisor's `OMNIGENT_URL` ahead of a saved server setting;
an explicit `--server` still wins. The passthrough reads the supervisor's
persisted ports without allocating new ones. After upgrading from a supervisor
that changed ports during passthrough, restart it to refresh the saved ports.

## Control path

```text
Omnigent chat / decision card
  → private local control channel
  → running TUI's native submit / decision callbacks
  → Cursor action manager and interaction responses
  → existing agent.v1 backend connection
```

The bridge observes Cursor's React element construction to retain the native
callbacks passed to the chat component, without changing the elements or their
props. This exposes submission even before the first model run. Cursor's own
submit callback constructs the user-message action, runs prompt hooks, updates
the terminal, and chooses its queue/steering behavior. Decision responses resolve
the native pending store or call the native question/mode handlers, which also
update the terminal. Installed Cursor files are never modified.

The local control channel uses private files alongside the existing native
bridge directory; it is not a second backend connection. Requests carry a
terminal-generation id and deadline. An uncertain acknowledgement produces an
error and never falls back to typing the action again.

## Available paths

| Operation | Native behavior |
| --- | --- |
| First prompt, follow-up, steering | TUI's `sendMessage` callback |
| Stop | TUI's native abort callback |
| Compact | TUI's summarize callback, including while idle |
| Model picker | Native model selection callback |
| Tool/image approvals | Native pending-decision store |
| Multiple-choice/free-text questions | Native selection state, then native submit/skip |
| Web and mode-switch decisions | Native dialog callbacks |
| Todo list | Native persisted checkpoints, with todo blobs decoded by Cursor |
| Subagent chats | Native child IDs registered as child sessions, with saved user/assistant text mirrored from each child's store |

In protocol mode these operations do not send tmux commands. The old
keystroke-based approval mirror is disabled. Tmux hosts and renders the TUI;
people can still type in it normally. The existing transcript, usage, and
compaction mirrors remain active, so native observations do not duplicate chat
items or token accounting.

The transcript mirror binds to the live native chat ID instead of choosing a
workspace store by creation time. Older discovery-based mirrors yield to that
owner. When attaching, it replays saved messages after its last delivered row.

Native decisions are mirrored from the current pending-state snapshot and
cleared when answered in either interface. Stale ids are rejected. A web decline
is delivered to the native decision handler before any cancellation can remove
that decision. Approval cards show the operation and a readable command or path
preview.

The forwarder reads Cursor's current approval mode over Omnigent's HTTP API:

```http
GET /v1/sessions/{session_id}/cursor-approval-config
```

The authenticated route queries the runner, which reads the live Cursor store
through the native control channel. It returns `approval_mode` (`allowlist`,
`unrestricted`, or `auto-review`), `run_everything`, and `source: cursor-runtime`,
with `Cache-Control: no-store`. This is a client-side setting; the endpoint does
not query a Cursor cloud configuration service.

Each pending-decision poll uses a fresh read, not launch flags or a cached UI
snapshot. Tool approvals are answered through the native decision store without
a web card in **Run Everything** mode. Explicit questions and mode-switch
decisions still surface. Automatic replies recheck the mode before applying, so
disabling Run Everything restores prompting. An unavailable runtime read never
falls back to cached automatic approval.

## Verification and remaining limits

The installed-runtime probe and native control unit tests work without network
access. A Python-to-Node control-channel smoke test exercised prompt, compact,
model selection, and cancel without terminal input. An actual PTY launch rendered
Cursor's workspace-trust dialog; further startup failed on DNS resolution for
`api2.cursor.sh` in the restricted environment. An authenticated model run and
visual synchronization after native submission are therefore **not yet verified**.

Todos appear in the right pane's **Todos** tab and the **Plan** accordion above
the chat. The right-pane tab stays available in terminal view and shows an empty
state when no todos have arrived. The accordion hides when the list is empty.
The native forwarder refreshes the latest todo snapshot every five seconds so
server restarts recover the in-memory list without another agent turn.
After a terminal restart, recovery also checks previous journals for the exact
native chat ID. Snapshots from other chats are excluded.

Checkpoint observation failures are logged without aborting Cursor's run.
Asynchronous native command failures are forwarded as failed-session events,
with delivery retried if the server is temporarily unavailable.

Subagent chats replay existing user/assistant text and then follow new messages.
Each child has a separate persisted delivery cursor and is bound to its exact
native store, never discovered by workspace recency.

This is not yet full Codex feature parity. Plan-build/revision UI, checkpoint
selection, and subagent tool/reasoning/live-status mirroring remain incomplete. The
adapter relies on private callback shapes and must be revalidated for every
supported Cursor build. Prompt acknowledgements mean dispatch into Cursor's
native handler, not confirmation that the backend accepted or completed the turn.
Some existing asynchronous regression tests stall in this environment; the
focused tests and Python type checks are reported separately from live validation.

Run these focused checks from the repository root:

```sh
node tests/inner/cursor_protocol_bridge.test.cjs
node tests/inner/cursor_native_controls.test.cjs
.venv/bin/pytest tests/test_cursor_protocol.py -q --no-cov -p no:rerunfailures
cursor_dir="$(dirname "$(realpath "$(command -v cursor-agent)")")"
"$cursor_dir/node" omnigent/inner/cursor_protocol/bridge.cjs "$cursor_dir/index.js" --probe
```

For the live test, launch the command above in a disposable workspace, then:

1. Send the **first** prompt from Omnigent chat. Verify it appears in Cursor's
   terminal and the response appears in both interfaces.
2. Send a follow-up while Cursor is working, then press Stop. Verify the same
   turn stops and no text is inserted into the terminal composer.
3. Ask Cursor to ask a question and perform an operation requiring approval.
   Answer/reject in chat; verify the terminal dialog closes and the decision is
   respected. Also answer once in the terminal and verify the web card disappears.
4. Change models and compact while idle. Verify native state changes without
   `/model` or `/summarize` being typed into the composer.
5. Restart the terminal/runner and continue the same conversation. Verify old
   decision cards cannot affect new prompts and todos remain synchronized.

Protocol background: [reverse-cursor-agent](https://github.com/0xlane/reverse-cursor-agent/blob/main/docs/00-overview.md).
