# Original activity observations

Parent owns the existing ActivityLog/AgentActivity/HistoryViews roster family;
592 indexed-reader source80dfd0d0 stays frozen for its installed gate. Reuse this
checkout and branch; no new worktree, environment, observer store or framework.

Source facts: ActivityLog.current creates an idle Activity with the constructor's
actual-event timestamp default when the log contains no event. Activity.from_wire
already owns unknown historical timestamp interpretation. Current/all_current
repeat stale-event projection, and AgentActivity.all_activity calls the original
log once per thread. Channel and thread rosters also acquire separate activity
observations and one channel caller reacquires registry ownership.

Fix at the existing event/log owners, retaining actual event clocks. Batch
observation uses one original event map and captured registry; idle unknown
observations must not claim activity now. All existing current/all-current,
individual/whole-roster and channel/thread consumers migrate together. The
known activity-clock failures and actual installed roster/order checks come last.
No native lifecycle,592 page-reader, Heis viewport or Einstein settings edits.

## Published implementation

Source7228b5bb changes four existing production modules,27 lines deleted and55
added. No new class, semantic store, cache, flag or alternate event decoder.
ActivityLog._observed is the one current/all-current expiry decision. Unknown
observations use Activity.from_wire's existing unknown timestamp; actual event
creation still owns its current-time default. AgentActivity._observed owns the
original owner/readiness and turn-phase projection used by both individual and
batch reads. Batch observation reads the original event map once, including
registered threads without events.

HistoryViews supplies one original registry and activity observation to channel
and thread rosters. ThreadView no longer reacquires it. Coordination unread
checks retain captured RegistrySnapshot alias semantics, without a fresh registry
read. Existing alias resolution and source timestamp semantics remain intact.

Before/after source census reuses refactor-audit Package/ParsedModule:311 Core
modules, zero parse omissions. Selected lexical declarations/imports/calls and
consumer sites are retained in source-before.json and source-after.json. Generic
method names can match unrelated receivers; dynamic alias/MRO resolution is not
proved by syntax. IMPL-12 duplicated implementation is removed at the existing
log/projection owners; this is a semantic source pass, not a test-led design.

Final batched checks:40 passed, one original race harness failed because it
started a second writer when last-sent timestamps opened the log again. The
harness now starts the requested concurrent mode change only at the first
opened boundary; every original assertion stays unchanged. That same race check
then passed0.53s. Raw failure and final result remain here. The other previously
failing alias/retag activity-clock check passed unchanged in the initial batch.
The full batch is not reported as an uninterrupted all-pass run.

Next: actual installed roster/order and channel/thread App checks on a released
existing holder. This source is not in live395 and does not hold its delivery.
