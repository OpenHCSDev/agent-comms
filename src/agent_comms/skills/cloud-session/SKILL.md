---
name: cloud-session
description: Start a new cloud Claude Code session on this repository that connects itself to the bus. Use when Tristan or a teammate wants another session, a remote session, or someone to take over an inbox.
---

# Start a cloud session

```
agent-comms-cloud-session NAME [TASK]
```

Run it from a checkout of the repository the session should work on; the session works on that checkout's pushed branch. `NAME` becomes both its inbox on the bus and its title in Tristan's app. `TASK` is what it does once connected. It prints the session's link.

The session starts in auto mode, connects itself, and wakes on each message sent to `NAME`. Give it work with `comms_send`.

Every session costs Tristan's usage, so say when you start one, and why.

Use this and not a routine for anything a person should see: a routine's run is not listed in their Sessions.
