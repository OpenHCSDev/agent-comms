Replace the broken headless benchmark (deleted facade calls and retired transcript replay) with an executable S7 contention workload on current owning APIs.

Three spawned poller processes run `Comms.views.list_threads` while the parent publishes through `Comms.messaging.send` on a fresh owner-only private root. 50/100/150 registered threads, 200 measured sends each. Compare current shared document reads with forced real exclusive acquisitions, keeping canonical wrappers, fsync, bus/registry guards and candidate maintenance intact.

Separate raw flock wait from acquisition including the durability barrier and hold including that barrier. Lock distributions are per process/store/requested and effective mode; worker polling failures are counted explicitly. Unexpected worker failures and publication failures abort rather than fabricate success.

Persistent scratch below ~/wt; three poller workers; per-process address-space, per-case disk, time and event budgets; workers joined/terminated and disposable roots deleted in finally. No production files changed.

Current matrix is executed: all 1,200 measured sends durable; shared send p50/p99 (ms) **71.22/114.36**, **147.48/229.15**, **178.40/245.60** at 50/100/150 threads. Registry raw flock p99 is approximately 0.006–0.010ms shared vs 1.18–2.15ms forced exclusive. Send improvement is not uniform; no causal A8 speedup claim.

**Concrete product finding:** five failed polls in the full matrix (both policies) raised `RelationViolationError: Private registry guard marker is not owner-only`. Initial fail-fast receipt retained. This benchmark does not repair or bypass it; parent owns production guard investigation.

Full measurements, timing definitions, limits and resource receipts: `evidence/s7-contention/RESULTS.md` and `current.jsonl`. Peak process RSS 422.9MiB, scratch 14.63MiB; case roots cleaned. Recovered isolated pre-A8 9cb6651f completed all three sizes: 600/600 measured sends durable, zero failed polls; send p50/p99 **172.32/312.55**, **306.55/506.98**, **424.48/757.91**ms. Historical lock timings and exact ephemeral-driver translation are retained. This is a full-version comparison, not an A8-only attribution. No benchmark measurement remains unfinished. No production file change; CI deferred.
