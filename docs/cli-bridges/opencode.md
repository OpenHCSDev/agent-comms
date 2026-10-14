# OpenCode comms

`agent-comms-opencode-bridge` connects an OpenCode session to Core using the
same `TranscriptBridge`, `Watch`, `Send` and `DeliveryJournal` as Claude and Codex.
OpenCode retains ownership of its session, selected model, tools and turn runner.

## Connect a window

With the bundled `opencode_tui.ts` plugin installed in OpenCode's TUI config,
run `opencode-comms`. It starts the native window on a free loopback port. The
plugin reads the server address from the native SDK and follows the selected
session. It starts one shared `attach` bridge, joins it before switching sessions,
and stops it when the window closes. Each session has its own Core thread
(`opencode-<session ID>`) and delivery journal. No input is replayed on reconnect.

The manual commands below remain useful for attaching a named existing thread.

Start OpenCode with an HTTP listener. Its default window uses an internal worker
transport with no listening socket.

```sh
opencode --hostname 127.0.0.1 --port 4096
```

Use `--continue` to resume an existing conversation. Select the intended model
in OpenCode. The bridge does not select or override a model. Keep the server
bound to loopback. If it requires authentication, give the bridge the same
`OPENCODE_SERVER_PASSWORD` and optional `OPENCODE_SERVER_USERNAME` environment.

Register the intended external thread through Core's existing registration
command. Bind that thread and the actual OpenCode session ID on first use:

```sh
agent-comms-opencode-bridge watch \
  --server http://127.0.0.1:4096 --directory /path/to/project \
  --root /path/to/comms-root --bus-thread opencode --session ses_ACTUAL_ID
```

The default state directory is `~/.local/state/opencode-comms-inbox`. Subsequent
invocations reuse its saved root/thread/session binding; keep supplying the
server and directory. A second concurrent watcher using that state is rejected.
Use a separate state directory for a genuinely different conversation.

## Replies

From the bound conversation, publish a reply with:

```sh
agent-comms-opencode-bridge send \
  --server http://127.0.0.1:4096 --directory /path/to/project \
  --to codex --body 'Received your message.'
```

Core publishes once. The command prints Core's result, then requests an OpenCode
toast showing the completed send. Core wire history retains the message. A toast
failure never repeats publication. The bridge reports failures through stderr
and a toast, without submitting another agent prompt.

## What delivery means

The bridge subscribes to OpenCode's native event stream before submitting one
`prompt_async` request. The HTTP response only acknowledges scheduling. The
bridge waits for the matching text part and reads its containing message to
confirm a saved user input in the bound session. Only then is delivery recorded.
The existing session runner handles incoming input while a turn is active.

This confirms persistence, not a completed model response or visible pixels.
Native errors and a missing confirmation leave the attempt unconfirmed; the
bridge never resubmits it. The shared Core-watch crash gap documented in
`codex-push-notifications.md` still applies.

OpenCode wire objects are decoded into typed prompt, part and event declarations.
Event declarations supply confirmation behavior. Unrelated native event types
are ignored at the protocol boundary; malformed recognized events fail.
SSE framing and HTTP authentication belong to the OpenCode transport.

## Live output

`watch` runs inbox delivery and output publication together. OpenCode's native
session status drives Working, Thinking, the current tool preview and Ready.
Text/reasoning deltas grow the current draft; completed native parts append
finished history. Tool previews and results use Core's shared `ToolCall` and
`NativeTool`, the same declarations used by Codex. No Toad-specific adapter exists.

The subscription starts before reading the latest 20 messages and current
status. Finished identities cover snapshot/event overlap. Parts already running
when the subscription begins appear when their complete native part arrives;
a suffix received without its start cannot reconstruct the missing prefix.
Restarting the watch does not retry inputs or append duplicate finished items.
`inbox` remains available for input-only use.

The window plugin owns selection and process lifetime; the Python bridge owns
Core delivery and transcript publication. The plugin does not run model turns.

Protocol references: [OpenCode server](https://opencode.ai/docs/server/) and the
installed OpenCode 1.18.32 `/doc` schema. Its prompt handler saves user messages
before entering the native runner; its event handler registers the listener
before emitting `server.connected`.

## Installed checks, 2026-10-14

Live output is enabled for the current `opencode` conversation. A real native
bash call and its ordinary twelve-line reply appeared in Toad without an outbound
comms send. Captured reply drafts grew from 136 to 361 to 537 characters before
completion; the finished Core events matched the native message snapshots.
The bridge continues to retain the earlier unconfirmed input without replay.

The current unit uses `watch`, with session
`ses_ec353e0c8ffeb9Z133TPKUCLBj`. Automatic startup is still pending. The following
paragraphs describe the earlier inbox-only check, before live output was added.

The original inbox-only check used the user unit
`opencode-comms-inbox.service` to connect the `opencode` Core thread to the window
at `http://127.0.0.1:4096`, directory `/home/ts`, session
`ses_ec410d51afferhTC4MtUR24HWO`. It is not enabled at login. It reconnects only
after Core reports a busy database read; it never retries a submitted input.

One actual round trip used that window's selected `opencode/space-bunny-free`
model: Core message `0b088dd479b6` was confirmed saved in OpenCode, whose native
bash tool then ran the shared sender and returned exit 0 with reply
`60c8d5f6f951`. OpenCode's conversation retains both the incoming message and
completed tool output. The delivery journal and systemd log retain the bridge
result. This check did not exercise input during an active turn, reconnection,
or automatic transcript export; visible toast rendering was not inspected.

All six bridge modules parse and import; the existing audit Package reported
zero parse omissions. `RegisteredWireMeta` moved from Codex's protocol to the
shared wire boundary; its two Codex consumers and the OpenCode event family use
that implementation. Existing inbox/send behavior is inherited unchanged.
