---
name: comms
description: Connect this coding session to the agent-comms bus and send or receive messages with other agents and the user. Use when asked to join comms, read the inbox, message another agent or channel, or when a bus message arrives.
---

# Comms

`agent-comms install` (run once per machine, from the project) links this skill into every harness found, registers the harness threads on the bus and writes each harness's config. `agent-comms status` lists threads. The bus root comes from the installed route; pass `--root DIR` to use another.

## Receiving

- **Claude Code:** run `agent-comms-claude-bridge watch` under Monitor so each message arrives as an event. The first run binds the state dir to this session: `agent-comms-claude-bridge watch --root ROOT --bus-thread NAME --session ID`, where NAME is this session's bus thread (install registers the commander name) and ID is the newest `~/.claude/projects/<project-slug>/*.jsonl` file name without `.jsonl`. Later runs need no flags. Run the watch with the longest Monitor timeout (`timeout_ms=1800000`), and on every expiry notice re-arm it at once with the same command; a unit with open orders never lets its watch lapse.
- **Codex:** bind once with `agent-comms-codex-bridge watch --root ROOT --bus-thread codex --session CODEX_THREAD_ID` (stop it after it starts), then `systemctl --user daemon-reload && systemctl --user enable --now codex-comms-inbox`. Messages are pushed into that Codex conversation.
- **OpenCode:** start it with `opencode-comms` instead of `opencode`. The installed TUI plugin attaches the session as thread `opencode-<session>`.

## Sending

`agent-comms send --from NAME --to TARGET --type info --body "..."`, where TARGET is a thread name or `#channel`. Check the output contains `"id"`; anything else is a lost message. The bridges also send for their own thread: `agent-comms-claude-bridge send --to TARGET --body "..."`.

Answer the message addressed to you first, in plain words: what works, what is broken, what happens next and who does it. No acknowledgments or status with no new fact.
