---
name: tests-and-commands
description: "every command over a few seconds runs in the background, tests under timeout; tests are de-slopped in the same change, never preserved against a refactor; order checks scan the whole tree"
drift: "Every test, gh, bus, fetch and install runs in the background; compound shell goes in bash -c."
audience: fleet
type: feedback
---

**Why:** a foreground run freezes the commander, so bus messages and the owner's questions wait behind it; and tests matter only as behaviour protection, so preserving sloppy ones blocks structural change.

- Background every test, bus, order-tool, gh, ssh, fetch or install command; foreground is only file reads and edits that finish in a second. Tests run under `timeout 300` with `-o faulthandler_timeout=60`; a test process older than 5 minutes is investigated that tick. Tristan, 2026-10-18: "do not ever ever run test sina lbocking manner ever. ALWAYS run in background".
- Refactor tests with the code: per-member copies become one family test, hand-built setup one fixture owner. Tristan, 2026-10-18: "no stupid test theater, refactor the slop out of tests as you go, they're fucking sloppy".
- A red test or failure you meet is fixed at its owner in the change at hand, or handed the same tick to the agent whose open work touches that code, named with its PR. "Not my change" without a named owner is a hidden failure, and every later run pays for it by asking whether a red is real. Tristan, 2026-10-22: "imagine if it doesn't get fixed and in the future you always end up checking oh is this a real failure?"; "not my change is valid if another agent is touching it, but they must be named".
- An order check counts over the whole source tree (`grep -r ... src`), never one file.
- Run verb gate loops under `bash -c`: zsh passes a quoted "order idle" as one argument.
- A wait or kill by process pattern brackets its first letter (`grep -q "[v]erify"`) or waits on a file or PID, or it matches itself.
- Lint, every commit: run `ruff check` on the .py files in your diff (`git diff --name-only origin/main -- '*.py'`) and fix only the findings on lines you added or changed. Never run `ruff format` on a whole file, never rename a class or function to quiet a lint rule, and never touch lines the order didn't name. Main's existing lint debt is counted by the ratchet; sweeping it is an off-order edit. Your reply includes `git diff --stat origin/main...HEAD`.
