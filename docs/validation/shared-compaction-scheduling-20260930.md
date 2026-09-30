# Shared native compaction scheduling

Owner: Einstein, follow-up to merged PR429 and PR437. Source baseline:
`b71b0ef7ed088b32bc8d19462d2f89c593d86c3d`.

## Completed real source evidence

Use the completed PR437 real03 automatic/manual receipt, not another paid run.
`evidence/native-compaction-progress/real-installed.json` records the original
43,194,077-byte session, selected Sol/high route, native clock and source events.

| Native interval | Automatic seconds | Manual seconds |
| --- | ---: | ---: |
| History traversal/maps through synthesis admission | 165.519 | 156.491 |
| History synthesis until current-turn admission | 125.229 | 125.670 |
| Current-turn summary until completion | 86.038 | 89.049 |
| Native total | 376.786 | 371.210 |
| Whole installed journey | 400.918 | 382.512 |

The approximately 1.6-second initial map-admission interval is observable.
Map, synthesis and current-turn intervals include provider execution and local
processing; the receipt does not isolate network wait, reasoning or generation.
The earlier read-only original-source preparation took 0.728 seconds, measured
separately. Do not subtract it as an isolated span of these provider journeys.

## Concrete dependency and remaining scope

`compact` awaits history map/reduction/synthesis before calling
`generateTurnPrefixSummary`. The turn prefix does not consume the history result:
its source and prompt are independent; final concatenation and usage combine are
the actual join. Serial admission adds 86–89 seconds in these completed runs.

Extend the existing CompactionPolicy/CompactionPlan scheduler to admit independent
source work with one configured provider concurrency bound, one abort/join path,
chronological reduction and unchanged output/context budgets. Nested reduction
must not occupy a worker while waiting for child jobs. Source progress and elapsed
must remain derived from the native source job, never from parallel callback
copies or backend timers. Preserve all summary bytes, usage, original-input
dispositions and once-only native commit.

This is a scheduling opportunity, not a measured candidate speedup. The provider
still determines the duration of each individual synthesis. No new timeout,
cache, policy, semantic store, reasoning override, paid replay or default install.

## Ownership and acceptance

PR429's rolling scheduler was owned by this worker; parent notified before this
follow-up. PR428 belongs to comms428 and owns task-aware/retained-memory planning,
not this production scheduling change. Shared lifecycle/progress contracts remain
coordinated with Arendt; presentation and store transactions remain their owners'
scope. IMPL-8, IMPL-13 and TIME-1 apply: extend the determining native mechanism,
delete replaced serial/callback plumbing and migrate the whole caller graph.

Before readiness: actual prepared native controlled-provider continuous compact
journey verifies bounded concurrency across nested maps and both source branches,
ordered summary/usage, progressive monotone source clock, abort/failure cleanup,
and serial policy. This is local native-path evidence; a subsequent real model
speed measurement needs a fresh authorized input and is not claimed here.
