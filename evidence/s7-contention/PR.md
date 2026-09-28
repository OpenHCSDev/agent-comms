Replace the broken headless benchmark (deleted facade calls and retired transcript replay) with an executable S7 contention workload on current owning APIs.

Three spawned poller processes run `Comms.views.list_threads` while the parent publishes through `Comms.messaging.send` on a fresh owner-only private root. 50/100/150 registered threads, 200 measured sends each. Compare current shared document reads with forced real exclusive acquisitions, keeping canonical wrappers, fsync, bus/registry guards and candidate maintenance intact.

Separate raw flock wait from acquisition including the durability barrier and hold including that barrier. Lock distributions are per process/store/requested and effective mode; worker polling failures are counted explicitly. Unexpected worker failures and publication failures abort rather than fabricate success.

Persistent scratch below ~/wt; three poller workers; per-process address-space, per-case disk, time and event budgets; workers joined/terminated and disposable roots deleted in finally. No production files changed.

Execution underway; the first 50-thread shared case completed at send p50 71ms / p99 114ms with 200/200 sends durable. Initial exclusive run exposed a real fail-closed registry marker read, preserved in first-attempt.log. Complete measurements and bounded historical-baseline recovery will be attached before handoff. CI deferred.
