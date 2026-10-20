---
name: home-storage-growth
description: "/home filling up under my fleet is my failure; retire worktrees/scratch per run and use a Claude cleanup agent when it bites"
type: feedback
---

2026-10-17 16:47: /home hit 1.2 GB free (786G disk) and blocked every build. Agent waste: 122 worktrees in /home/ts/wt (19G), 11G agent-scratch, old runtimes. Tristan: "home has grown under your command, get subagent to clean shit up. find all the stupid waste of storage."

**Why:** the fleet creates a worktree and a scratch dir per order and nobody retires them; a full disk stalls the whole install pipeline.

**How to apply:** every order that creates a worktree or scratch dir names its removal in the edits ("remove the worktree after push"); check `df /home` each tick alongside unjudged count; when free space drops under ~30G, put a Claude fork on cleanup immediately (not a Codex unit — it under-deleted and stopped at 1.2G). Related: [[test-cleanup-and-cheap-agents]], [[judge-backlog-every-tick]].

Every cleanup order must also check `git worktree list` of every repo and each checkout's `.git` file target (a path being a worktree's gitdir is a borrower), and never delete a dir containing `.git/worktrees/`.

2026-10-19 10:45: dropped from 30 to 17 GB free in a day with nobody on it. Tristan: "16gb is too low, we were at 30 yesterday, you let codex waste space." Every unit's check hand-copies whole 400 MB saved sessions, bus files and sqlite stores (28 big session copies), and the 3 GB per-unit cap never bounds the total across 12 units. Act on it the tick it appears: start a cleanup fork and order the structural fix, a Core check-root copy that holds only what the check needs and is removed when the check exits (P to e2e, 2026-10-19).
