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

## Published code checkpoint

Deleted **73 production JavaScript lines**: 66 actual compiled compaction lines,
six policy lines and one source line. This is the algorithm delta against the
reviewed614 native artifact, not patch-text deletion or test-line accounting.
Added six declaration lines for the existing source contract. Replaced the
serial turn-prefix helper with a HistorySummarySource member whose instructions
and phase are declaration-owned.

CompactionPlan admits both original sources; every model-sized leaf request uses
the same plan's provider slots. Source orchestration does not occupy a provider
slot while waiting for nested reductions. The plan retains the original source
scope's AbortController reference; a rejected leaf aborts that scope before a
queued request can start. The old local failure copy is removed. Chronological
result ordering and usage aggregation remain unchanged. Branch-summary behavior
is untouched; compaction validates the whole leaf before releasing its slot.

Original-source controlled acceptance uses the actual 43,194,077-byte saved
session and its 600,500-byte selected source, actual SDK event streams and compiled
native compact function. The same configured output4096/thinking-high contract
is checked on all seven requests. Four global slots are observed; serial policy
observes one. Original source bytes are unchanged, progress is monotone, streamed
summary text arrives, and all owned provider operations join on cancellation and
failure. Temporary ReducedSummarySource directories close normally.

The controlled provider span is893.717ms at the reviewed614 baseline and621.071ms
with shared scheduling: **30.5% shorter in this controlled fixture**. This is not
a measured real-model speedup. Preparation is separately measured at190.524ms
and184.168ms. Current-turn work starts before history synthesis completes; final
output still puts history before current turn and counts every usage once.

The standard pinned preparation recipe now reproduces the complete candidate
with zero fuzz and validates its full-tree commitment. Missing npm archive data
was obtained into the owned scratch npm cache from the existing lock, without
installing or changing default packages. Prepared manifest:
`e36a1dde326b70179fa1c854a73fcad61948c90f5a93465a55ddd07d72936f07`;
tree:`5ea25e3f9e88d073e5506ce97ceeca4c763b6da19f986ab032880d45f020f35c`.

[Controlled receipt](../../evidence/shared-compaction-scheduling/controlled-native.json)
records the source checkpoint and installed acceptance. A fresh noneditable
candidate verifies the exact native full tree. Two original-large-source native
RPC/accounting cases pass in15.93s: successful journal/once-only original custody,
and retained-output overrun preserving UNKNOWN/source with no original binding.
Continuous actual ACP journeys for ordinary and private sessions both pass in
28.30s, with two compactions and two distinct originals started once per journey;
publication precedes native binding. Fourteen unrelated parametrizations were
deselected. No repeated paid437 gate or default installation is performed.

The nested-source experiment uses a fresh SDK-created1,403,136-byte native
session with both history and current turn requiring map/reduction. Twelve leaf
requests share four slots, preserve ordered output and monotone source progress,
then join and clean up. This generated-source case complements the original43MB
case and establishes no independent real-model speed claim.

Deleted the273-line obsolete `patch-native-compaction.py` builder and its two-line
shell call. It injected the old batch scheduler, offsets and clock before a later
patch removed them; those competing authored implementations are gone. The
existing native storage patch now changes pinned stock compaction directly into
the sole final implementation. Fresh standard preparation from the changed
recipe passes zero-fuzz/full-tree verification and produces the **same** e36
artifact already tested above. No repeated native journey for byte-identical
code. These273 Python recipe lines and two shell lines are reported separately
from the73 actual runtime JavaScript lines deleted.

Remaining ownership limit: real03 source events identify provider-containing
phase intervals, not isolated network/queue/reasoning durations. Completed native
entries record aggregate input/output usage (automatic160385/15268,
manual166312/15996); their reasoning field is0, which does not establish absence
of reasoning on the selected high route. Provider/model/thinking, output/context
budgets, task-aware triggers and retained-memory policy are unchanged. Real speed
after activation must be measured from a new ordinary operation, without replaying
these completed inputs. No Toad/ACP protocol change or consumer pin is required.

Owned scratch: `/home/ts/.cache/agent-scratch/comms-shared-compaction-scheduling-20260930`
contains provider-free receipt/index data and the disposable locked npm cache.
The temporary copied-native stage and npm-prime build tree have been retired;
the prepared `stack/.pi-native-e36a1dde326b7017` artifact is protected review input.
Resource preflight: home29.9GiB/RAM16.0GiB, accumulatedswap15.9GiB warning only;
bounded source-only tests, no new native-owner fleet.
