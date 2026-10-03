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
