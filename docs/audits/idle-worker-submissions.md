# Idle executor submissions and installed awareness

Owner: core-awareness-index. Investigation checkpoint, not a new implementation.
Source: 5d025464cf44804b23e690bcf71b5520ade700e1.
Installed: toad-6e05712f2-core-948fec866.
Observed on 2026-10-13 after the second restart. No original or UNKNOWN input
was replayed; the live bus was read only.

## Executor submitter (cause c)

`InputDrain.observe` submits `_tick_revision` through `Coordination.run_worker`
at `input_drain.py:329`. `Coordination.run_worker` submits to asyncio's default
executor at `coordinator.py:33`, and joins the operation before returning.
Changed ticks also submit the registry, certified source and coordination
reads shown by their call sites in `input_drain.py:301-306, 435-482, 526-554`.
The unchanged settled tick executes only its revision stat operation; it does
not decode rows, drain, synchronize configuration or prepare a goal.

The installed observer was exercised on a fresh disposable scratch bus with
its actual configured native launcher/package and real WireWatch, registry,
checkpoint and coordination stores. An instrumented ThreadPoolExecutor in
that isolated process recorded submitted callables and concurrent callbacks;
no production scheduler was patched and no provider input was sent.

- Startup grew the default pool from one to two threads at `Registration.entry`.
  Maximum concurrently executing callbacks was **one**.
- After the first settled marker read, five quiet seconds submitted exactly
  five revision checks. No new threads, drain or configuration work.
- Three messages to another recipient, spaced 1.1 seconds apart, submitted six
  revision checks, three sealed-address reads and one coordination read. No
  full drain/configuration/goal work and no new threads.

CPython 3.14's `concurrent/futures/thread.py:100-116` marks a worker idle only
when its work queue is empty; completion can wake the loop before that idle
mark. `submit` queues the next operation and `_adjust_thread_count` starts a
thread if the idle semaphore has no token (`thread.py:215-240`). Workers then
wait for more work until executor/interpreter shutdown (`thread.py:121-136`).
The reproduced count is retained pool high-water, not evidence of an unbounded
thread leak. Which sequential callback grows the pool is timing dependent:
earlier runs grew at coordination reads or goal preparation instead.
No thread cap or replacement executor is justified by this observation.

Read-only py-spy showed four sleeping default-executor threads in installed
worker 1806271, plus the separate asyncio child-wait thread. A 20-second
/proc sample showed unchanged total thread counts (4, 4, 5, 4) in workers
1783977, 1783997, 1806147, 1806202. CPU during that interval was respectively
3.40%, 3.40%, 3.50%, 7.05% of one core. The first, second and fourth owners
were idle at both endpoints; the third was executing and is not idle evidence.
These are current-load observations, **not** a controlled before/after result
for #761/#764: traffic differs from the earlier baseline interval.

## Omission is installed and seen; awareness is still unavailable

This actual delivered request contains:

> Optional awareness omitted (ProjectionUnavailableError); the original is delivered alone and no read is implied.

The same wording is present in the current owner log
`diagnostics/owner-716545e8d63f1e097ecd18afb0cd0083-0aee67ffc092439690dd34f6fc2ef7fe.log:2`
and in retained native session messages containing source sequence 3181.
This establishes installed delivery visibility, not successful awareness.

The live candidate DB had an empty WAL and a stable main-file revision during
copying. Its checkpoint was last_seq **3168**, byte_offset **3875793171**.
The separately read bus marker was already at **3332**. No WAL or live index
was written. Calling the installed `WakeCandidateIndex.page` on the disposable
DB copy with required_through_seq=3181 reproduced:

> ProjectionUnavailableError('candidate projection is stale or unrelated')

`wake_candidate_index.py:510-514` refuses this lag before reading source rows.
The optional reader asks that index for the captured high-water at
`optional_awareness_projection.py:230-237`. This proves the stale-page refusal
for that snapshot; the delivered generic error does not identify which of
all optional-reader guards fired in the original turn.

The first unindexed original record at that offset was sequence 3169 (1301
bytes), so this remaining lag is not the old first-oversized-record refusal.

## Remaining repair: publication notification coverage

There is a demonstrated notification gap:

- `Messaging.send_message`, `send_initial_cohort` and `_notify_user_commit`
  schedule catch-up after publication locks exit (`messaging.py:94,118,203`).
- `LiveResponseOwner._publish_owned` publishes fenced responses directly
  (`coordination_response.py:278-281`). The append occurs in
  `_settle_fenced_response` (`coordination_response.py:554-556`) through
  `Publisher._publish_keyed_response_unlocked`, whose end appends then returns
  (`publisher.py:562-564`), without that notification.

core-awareness-index remains the implementation owner of this gap. Before
editing, enumerate the complete append/lock-release consumer family with the
existing AST tooling and place maintenance notification at the existing owner
that can cover every successful append **after** the locks exit. Delete the
caller-owned notification copies in the same change; never add a foreground
index rebuild, replay an uncertain response or manually repair the live DB.
The gap explains a way for the index to remain stale, but this checkpoint does
not claim to have traced every intervening live publication to that path.

## Verification limits and cleanup

No production code changed, no new runtime was installed and no merge was
performed. The observer experiment exercised the installed application code,
not a provider turn. Initial harness runs with the system Python correctly
failed native launcher validation; their full ticks are not healthy-idle
measurements. The final runs used the installed runtime Python.

Disposable observer buses and the retained-index copy were removed in finally
blocks. The one-shot instrumentation script was removed after recording these
results. The processes exited and asyncio shut down the measured executors.
