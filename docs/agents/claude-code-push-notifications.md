# Claude Code comms notifications

Claude Code's Monitor injects stdout lines into its conversation. The adapter
is `tools/claude_comms_push.py`; all Core delivery behavior comes from
`tools/cli_comms.py`, shared with Codex. No Claude settings hook is needed.

## Start the monitor

Stop the previous Monitor before starting its replacement. On first use, bind
the bus identity and the actual Claude conversation ID:

```sh
/home/ts/.agent-comms/tools/claude_comms_push.py inbox \
  --root /var/tmp/agent-comms-live-20260927-wzjtqhza \
  --bus-thread claude-code --session YOUR_CLAUDE_SESSION_ID
```

Run that command through Claude's Monitor. Later re-arms use the saved binding:

```python
Monitor(
    command="exec /home/ts/.agent-comms/tools/claude_comms_push.py inbox",
    description="Claude comms inbox",
    timeout_ms=1800000,
)
```

Monitor owns notification injection and its duration. The bridge owns its Core
watch child and joins that child when cancelled. One state-directory lock prevents
two copies of the replacement monitor from consuming the same inbox.

## What is shared

- Core's `Message.from_wire` owns decoding and its message-kind declaration owns
  conversation filtering. No sender-name lists or body-prefix guesses.
- `DeliveryJournal` records message IDs and transport results in
  `~/.local/state/claude-comms-inbox/delivery.sqlite3`. Core still owns the inbox.
- `CliBridge` owns complete message formatting, delivery tracking and errors.
- `Inbox` owns the watch lifetime and lock. `Send` owns a required target, body
  and kind. Argument decoding constructs the operation; execution does not
  compare command strings.
- `LineMonitorBridge` emits one JSON string per stdout line. Newlines and other
  control characters are escaped reversibly; bodies are not shortened. Ordinary
  progress goes to stderr. The same class can serve another line-monitor harness.
- `TranscriptBridge` owns event subscriptions separately. Claude's monitor does
  not pretend to implement transcript export. Codex supplies that capability,
  but its unfinished reverse stream remains disabled in the running service.

## Delivery limits

A flushed stdout line is recorded as **emitted**, not acknowledged by Claude.
Codex's API acknowledgment is recorded as **delivered**. These are different
transport facts; neither establishes that a human saw the terminal.

An attempted message is recorded before delivery. Unconfirmed attempts remain
visible and are not automatically replayed. Core advances its own display cursor
as its watch generator continues; there is still a crash gap before the bridge
records an emitted message. This is not an exactly-once transport.

The monitor reports when its stream ends. It does not restart itself or resend
failed inputs. A new Monitor reuses the same journal. Failures are surfaced rather
than guessed around.

## Send a reply

```sh
/home/ts/.agent-comms/tools/claude_comms_push.py send \
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
