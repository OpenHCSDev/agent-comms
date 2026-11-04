---
name: cloud-session
description: Start a new cloud Claude Code session on this repository that connects itself to the bus. Use when Tristan or a teammate wants another session, a remote session, or someone to take over an inbox.
---

# Start a cloud session

From a cloud session that is already on the bus, call `create_session` (the claude-code-remote tools):

- `title`: the new session's inbox name, `claude-<letter>-<word>`, so Tristan sees the same name in his app and on the bus.
- `permission_mode`: `auto`.
- `source_url` and `source_revision`: the repository and branch it should work on.
- `prompt`: `connect as NAME, then TASK`.

The child gets the comms connector at its first start, in auto mode, with nothing for Tristan to approve. Its comms tools may be named `mcp__<uuid>__comms_*` rather than `mcp__Comms_crt__comms_*`, so it finds them by keyword. Give it further work with `comms_send` to `NAME`.

Every session costs Tristan's usage, so say when you start one, and why.

## When no cloud session is on the bus

```
agent-comms-cloud-session NAME [TASK]
```

Run it from a checkout of the repository the session should work on; the session works on that checkout's pushed branch. It prints the session's link. A session started this way gets no connector until Tristan opens it in the Claude app, so it cannot connect on its own: tell him to open it.

Use either of these, and not a routine, for anything a person should see: a routine's run is not listed in their Sessions.
