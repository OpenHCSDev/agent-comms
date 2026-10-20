---
name: ship-fixes-same-hour
description: "Merged fixes get installed and live the same hour; when something significant is broken, do not hold for caution or his decision"
type: feedback
---

Tristan, 2026-10-13: "getting fixes in is always a priority, when something significant is broken, we don't need to be cautious about breaking things, it's already broken that's more important", after I held an install eight hours waiting on his decision about a schema difference that took minutes to carry.

**Why:** a merged fix that is not installed does nothing for him; the app was already unusable, so the hold protected nothing.

**How to apply:** after a merge, build the runtime and relink the same hour. If the live bus refuses the build (schema, native package pin, route file), back up, carry it and continue rather than asking. Still back up first and stop agents with the owner's `stop`. Cutover checklist learned that day: declared-DDL carry for coordination.sqlite3, native_prompt_bindings.sqlite3, wake_candidates.sqlite3; native package must match `stack/pi-native.sha256`; `~/.local/state/agent-comms/active-route.json` names the native package Toad uses; then open Toad on an isolated display before saying chats work. See [[session-state-20261011]].

**How to apply:** when a unit reports a build qualified and waiting for the go, I check its contents and send the go myself in the same tick. Never delegate the go, and never let "waiting for install go" sit past one tick.
