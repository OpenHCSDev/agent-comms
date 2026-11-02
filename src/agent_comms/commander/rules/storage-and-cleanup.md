---
name: storage-and-cleanup
description: "/home filling under the team is every teammate's failure: retire your worktrees and scratch per run and clean before 30 GB free is lost; but delete a teammate's scratch or worktree only when its owner lists it as disposable and nothing running uses it"
drift: "Never replay an UNKNOWN or failed input; never checkout, reset or delete what a running teammate uses."
type: feedback
---

> disk is at 16 GB free
> it was 30 yesterday
> I start a cleanup fork: delete what's off the board
> it deletes p499-installed-burst
> it held the receipts of 101 sent DMs
> the only copy
> I tell myself it wasn't on the board
> next I remove a worktree that looks finished
> a running teammate was using it
> install 9 tries to restart that teammate
> install 9 fails
> install 9 rolls back
> my forced worktree remove set core.bare=true
> every teammate's git breaks at once
> mfw I freed 2 GB and lost a night
> run it back
> disk at 16 GB
> ps: nobody runs in it. git: commits on the remote. its owner lists it disposable
> plain worktree remove. 2 GB freed, install 9 passes
> the receipts directory isn't on the list, so I don't touch it
> mfw "is anyone using this?" was one ps away

**Why:** the team makes a worktree and scratch per order and nobody retires them, so a full disk stalls every build; and an unowned-looking directory once held the only copy of sent-input evidence.

- Retire worktrees and scratch per run, and start a cleanup fork when /home falls under 30 GB. Tristan, 2026-10-19: "16gb is too low, we were at 30 yesterday, you let codex waste space."
- Preservation protects state something live still reads: Tristan, a running agent, an open diagnosis. State whose only reader is gone (a dead session's open question, a retired agent's leftovers) is deleted, not carried. Before building a backup, lock or migration for any state, ask who would notice if it vanished; if nobody, delete it and say so. On 2026-11-01 #3820 scoped a carry, backup, writer locks and a private-copy dry run for six open questions from Claude sessions that had not run in 12 hours. Tristan: "they're never going to be answered ... don't wait forever on that".
- "Off the board" is not proof something is disposable: on 2026-10-18 a cleanup fork deleted e2e's p499-installed-burst (receipts of 101 sent DMs) on that criterion and no copy survived. Sent-input records are UNKNOWN-input evidence.
- Never replay an UNKNOWN or failed input, never `git checkout` or reset the live checkout, and never edit another teammate's worktree.
- Before removing a worktree, check no running teammate uses it and its commits are on the remote: on 2026-10-20 removing a running teammate's worktree failed install 9 and rolled it back.
- Bus upgrade backups (full copies of the root taken before each install) keep the newest two completed at all times, the one the current install just took included; retention code shows that rule in its diff before it merges. Tristan, 2026-10-20: "clear them just keep at least 2 at all times".
- Count backups yourself; a teammate's count is a claim (one said 21 when there were 5).
- Before `rm -rf` under a tree a container tool mounts into (DQDock's dqdock_tmp), grep the path in `/proc/self/mountinfo`, unmount bind roots deepest-first and delete with `--one-file-system`: a recursive rm through a bind mount deletes the bind's source (2026-10-22: the sources happened to be inside the delete set, by luck).
- After a failed install, sha256-compare the live stores (`.root-runtime.json`, `bus.sqlite3`, `coordination.sqlite3`, `.registry-owner-guard`) against every recent backup's copies, never `find -newer`: a restore keeps the original mtimes. On 2026-10-23 install 24 restored install 22's backup over the root, `find -newer` showed nothing, "no live store changed" was reported, and install 25 died on ForeignRuntimeError. An install never prunes the only complete copy.
