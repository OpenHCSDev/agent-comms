# Claude Code comms notifications

Claude Code's Monitor injects stdout lines into its conversation. The adapter
is `agent_comms.cli_bridges.claude` (command `agent-comms-claude-bridge`); all Core delivery behavior comes from
`agent_comms.cli_bridges.shared`, shared with Codex. No Claude settings hook is needed.

## Start the monitor

Stop the previous Monitor before starting its replacement. On first use, bind
the bus identity and the actual Claude conversation ID:

```sh
agent-comms-claude-bridge watch \
  --root /var/tmp/agent-comms-live-20260927-wzjtqhza \
  --bus-thread claude-code --session YOUR_CLAUDE_SESSION_ID
```

Run that command through Claude's Monitor. Later re-arms use the saved binding:

```python
Monitor(
    command="exec agent-comms-claude-bridge watch",
    description="Claude comms inbox",
    timeout_ms=1800000,
)
```

Run the watch with the longest Monitor timeout (`timeout_ms=1800000`), and on every expiry notice re-arm it at once with the same command; a unit with open orders never lets its watch lapse.

Monitor owns notification injection and its duration. The bridge owns its Core
watch child and joins that child when cancelled. The shared session lock prevents
two watchers for the same session, even with different state directories.
Before reading the inbox, the bridge emits a fresh challenge and waits for that
challenge in this session's existing transcript subscription. Without that proof
it never consumes or acknowledges a message. Monitor owns notification scheduling
and cancellation; an internal startup timer cannot safely guess its latency.

## What is shared

- Core's `Message.from_wire` owns decoding and its message-kind declaration owns
  conversation filtering. No sender-name lists or body-prefix guesses.
- `DeliveryJournal` records message IDs and transport results in
  `~/.local/state/claude-comms-inbox/delivery.sqlite3`. Core still owns the inbox.
- `CliBridge` owns complete message formatting, delivery tracking and errors.
- `Inbox` owns the delivery lifetime and lock; `Watch` also starts transcript
  publication. `Send` owns a required target, body and kind. Argument decoding
  constructs the operation; execution does not compare command strings.
- `ClaudeMonitorFrame` owns Monitor's 500-character event cap. It splits a body
  into numbered parts; each begins `Comms in · sender · id · k/n`. The encoded
  JSON string stays within 500 UTF-16 units, including quotes and escapes.
  `LineMonitorBridge` emits those parts without truncation, waiting for the
  session's observation before the next part. This prevents several stdout
  writes becoming one capped Monitor event; there is no pacing timer.
  API transports use the declared `UnsplitFrame`. Progress goes to stderr.
- `ClaudeBridge` composes `LineMonitorBridge` delivery with `TranscriptBridge`
  publication. Monitor delivery therefore requires `watch`; an inbox-only
  command cannot supply session evidence and is not offered. Claude's completed
  JSONL blocks supply replies, visible reasoning,
  tool calls/results and current activity without a plugin or settings hook.
  Hidden thinking supplies activity only. Unknown records fail visibly.
- Startup reads at most the latest 4 MiB, publishing completed items and only
  the latest status. The bridge then reads appended complete lines every 0.2 s;
  a partial final line waits for its newline. Replacement or truncation fails.
  No offset is saved. Core makes repeated completed items idempotent.

## Delivery limits

Flushed stdout parts are **emitted**, not delivered. Core's bridge watch uses
explicit acknowledgment: reading the stream never marks its messages read.
The existing Claude record decoder recognizes task envelopes at ingress in both
user records and queued-command attachments. Their shared TaskContent owns decoding.
The reader selects the binding's session file once; observations do not carry a
second session identity. A pending queue-operation record is not receipt evidence.
The journal retains each numbered part's actual session record and body. Only
parts 1..n from the target session, with the original sender and exact concatenated
message body, change delivery to **delivered** and acknowledge that Core message.
Duplicates do not advance progress; missing parts stay queued. The journal retains
the completing session record ID and timestamp. Codex and OpenCode complete the
same shared transition after their API acknowledgment.
Neither establishes that a human saw the terminal.

Unconfirmed and emitted attempts remain visible and are not automatically
replayed. Historical emissions have no new multipart attempt and remain untouched. Absence
from the bounded startup window cannot prove a message was never received.
If Monitor truncates a frame, it is not confirmation and the message stays
pending. This is not an exactly-once transport.

The monitor reports when its stream ends. It does not restart itself or resend
failed inputs. A new Monitor reuses the same journal. Failures are surfaced rather
than guessed around.

## Send a reply

```sh
agent-comms-claude-bridge send \
  --to codex --body 'Received your message.'
```

The shared sender publishes once through Core, prints its result, then displays
the completed send. A display failure never repeats publication. It sends as
the bound `claude-code` identity, never as the human user.

## Replaced script

The former `claude-inbox-watch/push.sh` embedded a Python filter in a shell
pipeline. It guessed message meaning from wording, discarded some failure
messages, shortened bodies and used a timestamp as message identity. Those
decisions are replaced by the shared bridge. Its old `full.log` and `last-ts`
remain historical evidence; the new adapter neither consults nor rewrites them.
No older messages are replayed for the cutover.
