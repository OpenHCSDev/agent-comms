---
name: tests-and-commands
description: "every command over a few seconds runs in the background, tests under timeout; tests are de-slopped in the same change, never preserved against a refactor; order checks scan the whole tree"
type: feedback
---

**Why:** a foreground run freezes the commander, so bus messages and the owner's questions wait behind it; and tests matter only as behaviour protection, so preserving sloppy ones blocks structural change.

- Background every test, bus, order-tool, gh, ssh, fetch or install command; foreground is only file reads and edits that finish in a second. Tests run under `timeout 300` with `-o faulthandler_timeout=60`; a test process older than 5 minutes is investigated that tick. Tristan, 2026-10-18: "do not ever ever run test sina lbocking manner ever. ALWAYS run in background"; 2026-10-19: "stop running long ass tool calls in the foreground."; 2026-10-20 01:31: "u got 41 edits pending".
- Refactor tests with the code: per-member copies become one family test, hand-built setup one fixture owner. Tristan, 2026-10-09: refactor aggressively, tests are not production code; 2026-10-18: "no stupid test theater, refactor the slop out of tests as you go, they're fucking sloppy".
- An order check counts over the whole source tree (`grep -r ... src`), never one file.
- Run verb gate loops under `bash -c`: zsh passes a quoted "order board" as one argument.
- A wait or kill by process pattern brackets its first letter (`grep -q "[v]erify"`) or waits on a file or PID, or it matches itself.
