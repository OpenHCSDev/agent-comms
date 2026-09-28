# Identity and queued-compaction integration

Combined S5 39bf8db with merged PR154/155. No conflict: owner_compaction_adaptive retains both explicit owner-generation calls and FutureInputQueue plumbing.

Local acceptance:75 cases passed (actual old-core subprocess coexistence, identity, queue admission, private delivery, coding tools, local restart);2 actual native queue/compaction cases passed using the pinned package and synthetic provider. Old-core source archive9988f6b is used by subprocess coexistence tests. No CI wait.

The prior actual configured-provider queue run remains in evidence/input-drain. This S5 integration changes ownership counters and requires installed channel confirmation. No native bundle or settings change.
