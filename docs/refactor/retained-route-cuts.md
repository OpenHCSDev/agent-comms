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

## Implemented checkpoint

Deleted `RouteTotalRow`, later-suffix subtraction, historical timestamp count
scans and future timestamp-range listings. `RouteIdentityRow.first_seq` bounds
route discovery; `RouteEntryRow.time_root` selects the immutable original rank.
`RouteTimeNode.add` supplies shared prefix insertion through inheritance;
`LeafRouteTimeNode.grow` owns duplicate-time sequence chains and
`ForkRouteTimeNode.grow` owns child replacement. Both queries consume one
`OpenedRouteCounts._eligible` frontier. No kind/shape switch or second cache.
An original empty cut also cannot clear a later committed index. SQL references
are deferred by their ForeignKey declaration, not a hand-written deletion roster.
Deleted the stored `_targets` restatement; each query derives its requested actors
from the original DeliveryScopes. Test index paths derive from BusRouteCounts.

The full October15 paper was read, including theorem assumptions. Provider
reachability alone does not establish state sharing, lookup correctness or runtime
bounds. Leaf/fork behavior stays distinct; original wire and projection lifetimes
remain separate. No claim about arbitrary future requirements or a measured gap.

Verification so far:12 focused retained-cut checks,44 supplementary routing/read
checks. The large-advance check uses actual publication, an independent reader,
512 later messages and16 later routes; counts/listings remain exact with no new
eligibility calls and at most32 additional SQLite VM operations. Includes
nonmonotone and duplicate timestamps, sparse seen IDs, empty cuts, replacement,
truncation and declaration-derived table creation/reset.

Fresh application flow:3 retained eligible messages,15 in a fresh cut after12
later publications, retained cut still3; no model input. Actual saved application
`MessageBus.pending_counts_opened` on4,253,207,412 original bytes/through8513
matched the canonical decoder + DeliveryScope oracle. Six complete calls:
8.853,9.458,10.004,8.896,9.312,9.136ms. This is source application acceptance,
not an installed UI or physical-paint claim and not the universal50ms target.

The disposable schema is version2 with one active reader format; no legacy reader
or fallback was added. The existing live v1 index remains untouched for installed
borrowers. First preparation of the4.25GB source took approximately130seconds,
in bounded source-owner units outside the measured page path. It is not a cold
latency improvement. Preserve the prepared v2 file for integration to avoid
repeating this rebuild; owner core-retry, purpose saved-state/integration evidence:
`/var/tmp/agent-comms-live-20260927-wzjtqhza/bus_route_counts.v2.sqlite3`.
Evidence/receipts: `core-route-cut-8373/{application,saved,saved-complete}.json`.
Resource check warned about swap/disk headroom; preparation ran serially/niced,
without a native/model launch, publication or read acknowledgement. The new
projection is the only intentional application-data write; original sessions,
authentication and uncertain inputs were not edited or replayed.

The current discovery bound is routes present at the original cut, not all
possible incarnation history already inside that cut. Core-retry owns the next
refinement: seek through DeliveryScope's current sender identities, then remove
the intermediate route-birth catalog. Acceptance holds the current roster fixed
while increasing retired sender generations; no history-proportional query work.

No installation or physical UI acceptance claimed. Integration owner codex must
retain off-page preparation and the actual prepared resource at cutover. Native
ancestry remains on pushed #908; this repair does not finish its history walks.
