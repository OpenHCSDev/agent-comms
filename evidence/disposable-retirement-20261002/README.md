# Disposable retirement — 2026-10-02

Completed 17 obsolete generated environment retirements, 79 recomputable cache leaf removals, 12 flat fully merged worktree removals, and 10 inactive native dependency leaf removals. Exact paths and byte readings are in `receipt.json`.

The four deletion windows increased available home space by 2,768,056,320 bytes (2.578 GiB), subject to concurrent host writes and the other cleanup streams. The native dependency group had 1,421,737,984 exclusive allocated bytes before deletion. Current filesystem headroom is recorded separately. No prior cleanup is included.

Native `dist`, extensions, package metadata and locks remain in place with before/after SHA-256 manifests. Removed native dependency leaves make these historical packages intentionally non-executable; they had no current process, launcher, import, route/config or declared donor consumers. Active or externally referenced native packages remain intact.

Worktrees were clean, had no initialized submodules, were ancestors of their repository's `origin/main`, and passed reverse Git metadata and process/import consumer checks. Removal used ordinary `git worktree remove`; no force was used. Branches/common object stores survive. All ignored original logs/reports were copied and hash verified under the persistent raw receipt directory before removal.

Raw screens, preservation manifests and receipts remain under `.artifacts/cleanup-aggressive-20261002` in this worktree. Source/unreviewed work, saved sessions, UNKNOWN dispositions, active buses and public entrypoints were not modified. UV/scientific caches and other agents' active/claimed paths were excluded. No tests, builds, providers or additional agents were launched.
