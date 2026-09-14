# Cursor native controls (experimental)

This extends **Cursor Native** (`cursor-native`). Cursor's normal interactive
TUI runs in Omnigent's tmux terminal. Its native runtime sends `agent.v1`
protobuf messages over its existing backend connection. ACP is not launched.

Every Cursor session uses the native bridge, including sessions created from
the web UI. A standalone Cursor CLI installation is required:

```sh
omnigent cursor
```

The bridge patches the CLI's private internals, so it only runs against builds
it can drive. Build **2026.09.08-6caf4ff** is validated by hand and starts
immediately. Because the CLI updates itself, any other build is probed once
before the terminal launches — the probe loads the bundle, resolves every
interface the bridge patches, and decodes a checkpoint. Both outcomes are cached
in `cursor_protocol_probe.json` under the Omnigent state directory, keyed by the
bundle (version and stat) *and* the bridge scripts driving it, so a build already
judged is not re-probed while a rebuilt bundle or an upgraded Omnigent is. A
probe that could not be started at all is not recorded, since that says nothing
about the build.

A build that fails the probe does not cost the session: the plain TUI launches
with the protocol off, and the `protocol/enabled` marker is cleared so prompt
injection and model switching fall back to tmux. Native decision cards and
`/model` control need the protocol, so check the runner log for
`Cursor native protocol unavailable` if those stop working after a CLI update.

The bridge also pins the bundle's chunk id, which has held across builds so far.
A renumbered bundle fails the probe like any other incompatibility, so treat the
probe as a safety net for CLI updates rather than a guarantee that every future
build works.

The old `--omnigent-native-protocol` passthrough flag is accepted for existing
launch configurations but is no longer necessary. Incomplete CLI installations
fail at startup. Already-running terminals keep their launch mode; recreate the
terminal to activate the bridge after upgrading Omnigent.

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
| First prompt, follow-up | TUI's `sendMessage` callback |
| Queue display | TUI's native `queuedMessages` state |
| Steer queued head | TUI's `onSendQueuedNow` callback when native action is `steer` |
| Stop | TUI's native abort callback |
| Compact | TUI's summarize callback, including while idle |
| Model picker | Native model selection callback |
| Tool/image approvals | Native pending-decision store |
| Multiple-choice/free-text questions | Native selection state, then native submit/skip |
| Web and mode-switch decisions | Native dialog callbacks |
| Todo list | Native persisted checkpoints, with todo blobs decoded by Cursor |
| Subagent chats | Native child IDs registered as child sessions, with saved user/assistant text mirrored from each child's store |

Cursor sessions submit composer messages immediately to the native runtime,
even during an active turn. Omnigent's local delivery queue is bypassed; the
strip above the composer polls Cursor's own queued messages. Pending transcript
metadata is retained for attachment recovery but does not render a second queue.
Each web prompt carries a trailing correlation marker through Cursor's native
string queue and transcript. The web queue and mirrored chat hide this marker;
the server uses it to recover the exact prompt's attachments and author, even
when queued prompts have identical text or are edited in the terminal. Native
terminal text without a marker (including prompts queued before this change)
cannot claim pending attachment metadata. Removing the marker during a terminal
edit also removes that association. Deleted prompts' unused metadata expires
through the pending-input TTL.
The **Steer** button promotes the first native follow-up without resubmitting its
text. It appears only when Cursor exposes native steering, and stale snapshots
or repeated clicks during promotion are rejected. Cursor owns acknowledgement
and any fallback behavior. Edit, delete, and other native queue actions remain
available in Cursor's terminal.

In protocol mode these operations do not send tmux commands. The old
keystroke-based approval mirror is disabled. Tmux hosts and renders the TUI;
people can still type in it normally. The existing transcript, usage, and
compaction mirrors remain active, so native observations do not duplicate chat
items or token accounting.

The transcript mirror binds to the live native chat ID instead of choosing a
workspace store by creation time. Older discovery-based mirrors yield to that
owner. When attaching, it replays saved messages after its last delivered row.
Saved tool calls and results are mirrored as expandable chat cards, including
shell commands and file edits. Delivery also tracks progress within a row so a
restart or retry preserves messages containing several tools without duplicates.
Cards appear when Cursor commits their transcript records to its chat store.

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

Subagent chats replay existing messages, tool calls and results, then follow new records.
Each child has a separate persisted delivery cursor and is bound to its exact
native store, never discovered by workspace recency.

This is not yet full Codex feature parity. Plan-build/revision UI, checkpoint
selection, and subagent reasoning/live-status mirroring remain incomplete. The
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
