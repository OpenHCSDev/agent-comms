# Codex and comms

Each bridge connects one named Core thread to one existing Codex conversation.
Each conversation has its own systemd instance and saved binding.

## Current behavior

Core messages appear as visible Codex messages through App-server `turn/start`.
During an active turn this server accepts them into that turn; while idle they
can start a response. Receiving a message does not authorize resuming a parked goal.

Send through `codex-comms send --to user --body 'Your reply'`.
The command sends once through Core, prints its result, then displays that result
as Codex `comms_send` tool output. A display failure never repeats the send.
Sends made outside this command do not automatically get that display.

Assistant, tool, and public reasoning-summary activity flowing back into Toad is
being implemented. It is not live yet. That stream feeds typed transcript
presentation, not a bus message per update. Private reasoning is not exported.

## Ownership

- `agent_comms.cli_bridges.shared` owns Core watching/sending, transport attempt tracking, the
  saved binding, locking and process shutdown. `CliBridge` shares that lifecycle;
  it contains no Codex routing or protocol decisions.
- Parsed `Inbox`, `Send` and `Watch` operations own execution. `Send` carries
  its required target and body; only argument decoding interprets command names.
- `TranscriptBridge` adds event publication. `LineMonitorBridge` supplies Claude
  Monitor's stdout transport without fake transcript methods. Both use the same
  full-message formatting and delivery journal.
- Core's `Message.from_wire` decodes messages; its declared
  `conversation_message` property separates conversation from claim notices.
  The bridge no longer guesses from wording or copies the send-kind roster.
- `agent_comms.cli_bridges.codex_protocol` declares typed Codex protocol shapes. Decoded items
  supply their own Core transcript events. Externally defined tool arguments
  remain typed JSON, not an untyped application record.
- `CodexConnection` owns the WebSocket and response matching. Incoming messages
  and outgoing send results use distinct request types.
- `DeliveryJournal` owns transport attempts and the conversation binding, not
  Core inbox state or routing.
- systemd owns reconnecting a failed Core read stream.

## Operations

```sh
agent-comms-codex-bridge watch --root ROOT --bus-thread NAME --session ID
# After the first bind, end the foreground watcher and start that session's service:
systemctl --user daemon-reload
systemctl --user enable --now codex-comms-inbox@ID
systemctl --user status codex-comms-inbox@ID
journalctl --user -u codex-comms-inbox@ID -n 30 --no-pager
```

The template is `~/.config/systemd/user/codex-comms-inbox@.service`. Each instance
uses its session ID to select
`~/.local/state/codex-comms-inbox/ID/delivery.sqlite3`, containing that session's
binding and message attempts. Install retires the superseded single-instance unit
during maintenance.

For another existing conversation, supply its own `--bus-thread` and `--session`.
Watch registers the Core thread if missing; it never creates a Codex conversation.
Later starts read that session's saved binding. Conflicting bindings are rejected.
The shared session lock prevents concurrent watchers even with different state directories.

## Failure and delivery semantics

Incoming IDs are committed as `unconfirmed` before requesting Codex delivery.
Only an App-server acknowledgment changes one to `delivered`. Both prevent
automatic replay. An uncertain older message is reported on startup without
blocking unrelated new messages; inspect its actual conversation before deciding
whether anything should be resent.

Only the known Core `database is locked` watch failure exits 75 and reconnects
after two seconds. This does not retry sends or uncertain Codex requests. Other
failures stop the service and attempt one visible failure report. Full multiline
Core errors are retained in `watch-failure.txt`.

There is no distributed exactly-once guarantee. Bridge watches explicitly
acknowledge exact Core message references only after the harness confirms.
Advancing the watch stream does not mark a bridge message read. A crash after
journal confirmation but before Core acknowledgment is repaired by repeating
only that idempotent acknowledgment, never by resubmitting the message.
An App-server acknowledgment does not prove a particular terminal painted it.

Original envelopes remain in `delivery.payload`. Visible bodies are complete,
preserving line breaks and human/agent origin labels.

## Adding another CLI

Implement `CliBridge.deliver`, `show_sent` and `report_failure` using the CLI's
actual input and display protocol. `deliver` returns the actual transport outcome:
`DELIVERED` after acknowledgment, or `EMITTED` for a monitor pipe write;
`show_sent` receives an already published Core result and must never resend it.
Put CLI-specific connection options on an `Options` subclass and pass it to
`parse_options` and `run_bridge`. Core decoding, filtering, journaling, locking
and cleanup are inherited unchanged. The Codex adapter is `agent_comms.cli_bridges.codex` (command `agent-comms-codex-bridge`).
Harnesses with line monitors compose `LineMonitorBridge` with `TranscriptBridge`,
as Claude does. Its decoded session notifications must confirm the fresh startup
challenge and the complete numbered body parts; a pipe write alone stays pending.

Transcript export is a separate capability still being implemented, represented
by `TranscriptBridge`. The running service explicitly selects `inbox`. Its shared
destination is Core's typed transcript API; each CLI must decode its own event
protocol into those existing event types. This bridge does not expose private
reasoning or assume that another CLI has Codex's subscription protocol.

## Observed results

Actual user messages arrived in this conversation. Reply `f7fba8fb7713` went
through the refactored command and its Core result appeared as Codex tool output.
Eleven retained real bus envelopes decoded through Core with matching identities.

The earlier `thread/inject_items` path provided model context but no visible chat
message; it was replaced. Earlier error handling lost buffered multiline error
text; that was also replaced. A later complete error established actual Core
database contention. Read-stream reconnection does not prove that contention is
fixed.

Official [App-server documentation](https://learn.chatgpt.com/docs/app-server)
describes turn inputs, tool outputs and item notifications. Existing managed
Codex hooks and configuration are unchanged.
