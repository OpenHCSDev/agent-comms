---
name: tests-and-commands
description: "every command over a few seconds runs in the background, tests under timeout; tests are de-slopped in the same change, never preserved against a refactor; order checks scan the whole tree"
drift: "Every test, gh, bus, fetch and install runs in the background; compound shell goes in bash -c."
audience: fleet
type: feedback
---

**Why:** a foreground run freezes the commander, so bus messages and the owner's questions wait behind it; and tests matter only as behaviour protection, so preserving sloppy ones blocks structural change. A plain `timeout` sends TERM, which skips pytest finalizers: on 2026-10-23 that left 7 orphaned `agent_comms.worker --comms-root /var/tmp/ac-private-*` bus workers, up to 5 h old, because the private_comms fixture's cleanup (`InstallLogs(root).stop_running()` in its `finally`, tests/conftest.py:383) never ran; INT runs it.

- Background every test, bus, order-tool, gh, ssh, fetch or install command; foreground is only file reads and edits that finish in a second. Tests run under `timeout -s INT 300` with `-o faulthandler_timeout=60`; a test process older than 5 minutes is investigated that tick. Tristan, 2026-10-18: "do not ever ever run test sina lbocking manner ever. ALWAYS run in background".
- Refactor tests with the code: per-member copies become one family test, hand-built setup one fixture owner. Tristan, 2026-10-18: "no stupid test theater, refactor the slop out of tests as you go, they're fucking sloppy".
- A red test or failure you meet is fixed at its owner in the change at hand, or handed the same tick to the agent whose open work touches that code, named with its PR. "Not my change" without a named owner is a hidden failure, and every later run pays for it by asking whether a red is real. Tristan, 2026-10-22: "imagine if it doesn't get fixed and in the future you always end up checking oh is this a real failure?"; "not my change is valid if another agent is touching it, but they must be named".
- An order check counts over the whole source tree (`grep -r ... src`), never one file.
- Run verb gate loops under `bash -c`: zsh passes a quoted "order idle" as one argument.
- A wait or kill by process pattern brackets its first letter (`grep -q "[v]erify"`) or waits on a file or PID, or it matches itself.
- Never cut a check's output with `tail` or `head`; grep the lines that carry its verdict or read it whole. On 2026-10-23 a tail kept the tables and dropped the UNPUSHED alarms above them for 50 minutes. Tristan: "tailing is a terrible reflex of urs".
- Every background wait also watches progress: when its log has not changed for 180 s it prints STALLED and exits, since a wait on a PID alone cannot tell slow from hung. Tristan, 2026-10-25, after a 9-minute install hang he found himself: "I have to send you a message to say that it's hung? Why?".
- Lint warns, never blocks: the debt ratchet reports ruff and Black growth as warnings on the file and gates only restated facts. Read the warnings on lines you added or changed; fix one when it names a real defect, such as an undefined name or dead code. Never run `ruff format` on a whole file, never rename a class or function to quiet a lint rule, and never touch lines the order didn't name. Your reply includes `git diff --stat origin/main...HEAD`.
