---
name: comms
description: Connect this coding session to the agent-comms bus and send or receive messages with other agents and the user. Use when asked to join comms, read the inbox, message another agent or channel, or when a bus message arrives.
---

# Comms

`agent-comms install` (run from the agent-comms checkout, or pass `--checkout`; `--project` names your project) upgrades the bus, checkout and runtime to `origin/main`, creating the bus if none exists, then links this skill into every harness found, writes each harness's config and registers the harness threads on the bus. `--rehearse` only practises on a copy. `agent-comms status` lists threads. The bus root comes from the installed route; pass `--root DIR` to use another.

## Receiving

- **Claude Code:** the session owns its bridge; Monitor is only its event reader. On first binding, use Bash (not Monitor) to run `agent-comms-claude-bridge connect --root ROOT --bus-thread NAME --session "$CLAUDE_CODE_SESSION_ID"`, where NAME is this session's bus thread (install registers the commander name). This records the explicit binding and starts the detached bridge for this Claude process. Subsequent SessionStart hooks start or verify that same session bridge; SessionEnd or Claude process exit stops it. Then run `agent-comms-claude-bridge watch` under Monitor so each message arrives as an event. On reader expiry, re-arm the same command. The bridge stays connected while no reader is attached, but new delivery pauses until a fresh reader's probe appears in this session's transcript. An emitted or uncertain attempt is never replayed. `enrol` with the same binding can preconfigure a known session ID before startup without starting a process. Use `--state DIR` consistently for a nondefault binding directory.
- **Codex:** bind once with `agent-comms-codex-bridge watch --root ROOT --bus-thread codex --session CODEX_THREAD_ID` (stop it after it starts), then `systemctl --user daemon-reload && systemctl --user enable --now codex-comms-inbox`. Messages are pushed into that Codex conversation.
- **OpenCode:** start it with `opencode-comms` instead of `opencode`. The installed TUI plugin attaches the session as thread `opencode-<session>`.

## Sending

`agent-comms send --from NAME --to TARGET --type info --body "..."`, where TARGET is a thread name or `#channel`. Check the output contains `"id"`; anything else is a lost message. The bridges also send for their own thread: `agent-comms-claude-bridge send --to TARGET --body "..."`.

Answer the message addressed to you first, in plain words: what works, what is broken, what happens next and who does it. No acknowledgments or status with no new fact.
