# Old worktree cleanup

User requested removal of old worktrees to recover home disk space.

- Parent removed 144 clean merged worktrees using `git worktree remove`, without force or deleting branches.
- Heisenberg separately removed four clean merged worktrees; parent observed these as already removed.
- Removed 45 unused generated Python environments; their source and test evidence remain.
- Free home space at start: 25.00 GiB; final observed: 29.88 GiB. Concurrent tests can change this figure.
- Allocated directory estimates overcount actual reclamation when generated package files are hard linked to other environments or UV caches. Filesystem free space is the recovery measure.

Protected open PRs, recorded owner worktrees including stopped owners, process/launch references, editable installations, repository anchors, uncommitted source, ignored evidence, original sessions, native packages and uncertain inputs. The remaining dirty or unmerged worktrees require individual disposition; they were not force removed.

Audit and mutation receipts contain the exact affected paths. No installed package, native owner, live bus or source code was changed by this cleanup.
