# Retained route cuts

Owner: core-retry. Review: codex-shepherd. Integration: codex.

The existing route index must answer a retained original wire cut independently
of arbitrarily many later appends, including later routes, nonmonotone message
timestamps and sparse painted membership. Index totals minus a later suffix do
not meet this requirement. Timestamp eligibility must not be replaced by a
sequence approximation. A retained SQLite snapshot alone cannot repair a cut
whose route index was behind at acquisition.

Keep the original BusRouteCounts/OpenedRouteCounts resource and source authority.
Replace mutable latest-only totals with per-route sequence-addressed immutable
timestamp-count roots in that same index. A compressed binary timestamp trie
bounds a threshold count by the finite IEEE-754 key width; source sequence seeks
select the original root. Route discovery must exclude routes born after the cut.
Listing must prune future and ineligible timestamp subtrees, not scan them.
Delete suffix subtraction and historical count scans, not retain another path.

Before source census:607 Python/dependency modules, zero parse omissions,
135 syntax/name sites; dynamic bindings remain manually resolved. Evidence:
`core-send-row-latency-7959/route-cut-8373-before.json`.
Scratch owner/purpose: core-retry, route-cut scaling/source verification,
`/home/ts/.cache/agent-scratch/core-route-cut-8373`.

Current state: implementation pending. No tests or installed acceptance claimed.
The full October15 paper was read; finite supply counts do not establish runtime
bounds or behavioral equivalence. Preserve distinct source/projection lifetimes.
