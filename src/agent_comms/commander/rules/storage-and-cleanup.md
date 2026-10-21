---
name: storage-and-cleanup
description: "/home filling under the fleet is the commander's failure: retire worktrees and scratch per run and clean before 30 GB free is lost; but delete a unit's scratch or worktree only when its owner lists it as disposable and nothing running uses it"
type: feedback
---

**Why:** the fleet makes a worktree and scratch per order and nobody retires them, so a full disk stalls every build; and an unowned-looking directory once held the only copy of sent-input evidence.

- Retire worktrees and scratch per run, and start a cleanup fork when /home falls under 30 GB. Tristan, 2026-10-19: "16gb is too low, we were at 30 yesterday, you let codex waste space."
- "Off the board" is not proof something is disposable: on 2026-10-18 a cleanup fork deleted e2e's p499-installed-burst (receipts of 101 sent DMs) on that criterion and no copy survived. Sent-input records are UNKNOWN-input evidence.
- Before removing a worktree, check no running unit uses it and its commits are on the remote: on 2026-10-20 removing a running unit's worktree failed install 9 and rolled it back.
