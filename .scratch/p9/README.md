# P9 startup verification

Owner: doctrine-segment. Fresh direct task from claude-code, 2026-10-16.

No production edits: textual-image fix `1f1d8b8ea3c30b39da3b14ea0ac0786ce9c49a80` was already published and installed. Its `_terminal.py` bytes match source. Earlier producer/consumer checkpoints were also already pushed: python-introspect `2518631`, ObjectState `8642469`, OpenHCS `81849573a`.

`run.py` launched five separate installed Toad processes. Each started on a real PTY whose ioctl reported zero columns. The controlled terminal answered the actual cell-size query and primary DA; the installed application constructor, image selector, Linux driver and first-frame renderer ran unchanged. All five exited successfully, with the reported non-default cell sizes and Sixel support. This is protocol/PTY acceptance, not a physical st/Xvfb recording. No provider input or package installation was needed. P44 remains BLOCKED and its failed input was not replayed.

Result: `/home/ts/.cache/agent-scratch/rts-results/p9.json` (five passes, zero division errors, exact library/Toad/Core SHAs).

Retained output: `run-1` through `run-5`, approximately 0.5 MiB total including private test roots, first frames and terminal output. All five app processes exited and were reaped. Retain until owner acceptance, then retire disposable test output; preserve the concise result and reproduction source. The runner refuses existing run directories rather than overwriting evidence.
