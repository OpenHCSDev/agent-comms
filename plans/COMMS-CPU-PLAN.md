# agent-comms: remove repeated work from observation

Requested by Tristan on 2026-11-02 after inspecting the live Python processes.
This document records the measurements, established source paths, remaining
traces and implementation sequence. No performance repair has been implemented
or qualified by this investigation.

## Outcome and core issue

Toad and Core should observe committed changes without repeatedly decoding
unrelated history or reconstructing unchanged facts. Human read markers,
notification decisions, pending input and plan evidence must retain their
existing meanings. A quiet system should settle; active work should pay for the
affected data, rather than multiplying global work by every visible thread,
served teammate or wake check.

The established defect is in notification invalidation. Each applicable thread
window asks whether its read evidence changed. That call independently reads and
nominally decodes the entire shared `ReadDocument`. Checking which thread needs
an update can therefore reconstruct the same global ledger repeatedly before
any presentation is rebuilt. There is already a selective ledger read, but this
path does not use it.

This is not yet a complete explanation of all Core CPU consumption. The main
comms worker consumed more CPU than the observer in the final sample. Its
database, goal and activity work, and the connector's plan-evidence work, remain
part of this plan. A ledger-only improvement cannot be reported as resolution
of the whole process load.

## Evidence and its scope

The busy runtime's `build.json` identifies Core commit
`dcaf1677d05d9670c84f3e38f4251b717da88d09`, Python 3.14.8 and Toad commit
`8986a06c6936841e7edd49cff0de03ebfda302a1`. Source references below were checked
against repository main `1bb6295ea71433757e72bb5df7fc5c962419a63d`.
Reconcile intervening changes before implementing; a profile of the installed
runtime is not a measurement of a later source head.

The original Toad ran as PID `3833269` through Tailscale SSH from the laptop
`100.98.83.32`, on `pts/16`. Its observer, PID `9403`, accumulated approximately
79 CPU minutes over 2 hours 46 minutes. A lifetime `ps %CPU` of about 47% was
followed by a live sample near 14%, then a sample near 0.2%. A 20-second profile
caught only one active sample. This establishes intermittent activity; that
profile does not explain the earlier CPU time.

On the requested recheck, Toad had restarted as PID `1052432` in a new laptop
SSH session on `pts/5`, with observer PID `1052882`. Three-second process CPU
deltas, where 100% means one logical CPU, were:

| Process | PID | Before profiling | After all profilers exited |
|---|---:|---:|---:|
| Toad observer | 1052882 | 72.3% | 93.0% |
| Toad UI | 1052432 | 24.6% | 18.3% |
| Connector | 1043624 | 80.1% | 3.0% |
| Main comms worker | 1044130 | 45.0% | 136.3% |

These are separate short windows, not a steady-state benchmark or a before/after
optimization comparison. PIDs identify the captured runs, not permanent roles.

Read-only `py-spy record` captures requested 100 Hz sampling for 20 seconds.
The profilers fell behind and consumed substantial CPU, so they were interrupted
and their exits confirmed. Their stacks identify work actually reached, but
their sample shares are approximate and cannot establish precise time fractions,
event rates or speedups. The final CPU column was measured without profilers.

| Capture | Samples | Observed inclusive stack membership |
|---|---:|---|
| `toad-worker-new.txt` | 228 | 180 in `nominal/field_codec.py`; 135 in `changed_threads`; 118 in `changed_notifications`; 50 in `viewer_snapshot` |
| `connector-recheck.txt` | 144 | 61 in the `TeamHeartbeat` / `phase_evidence` path |
| `comms-worker-recheck.txt` | 168 | 35 in `field_codec`; 19 in `_private_observation_revision`; 17 in `goal_snapshot`; 14 in `agent_activity` |

Counts overlap and must not be added. The codec is where decoding executes;
these samples do not establish that codec dispatch, rather than the volume of
unnecessary decoding requested by its consumers, is the primary defect.

Raw captures remain on `ts-gaming` in
`/home/ts/.cache/agent-scratch/python-cpu-inspect/`. They are local diagnostic
artifacts, not files committed with this plan. Their SHA-256 digests are:

```text
toad-worker-new.txt
aac04c6355bb62c1275dd35f2ec632dce81f6175f873428c513a6aee74e368b2
connector-recheck.txt
5d009f4f19c6edd77334427c7aa20c20e6975c6d59c68e7663e611f2a1cfdfd4
comms-worker-recheck.txt
c63af87199ce94a39499d3c7289f7893227d6106ffebfd36605be07e946377e9
```

## Established trace: Toad notification invalidation

The captured runtime stacks and the checked source agree on this route:

```text
ObservationService.run                         ui_model/observation.py:1415
  -> ObservationService.observe               ui_model/observation.py:1393
  -> LiveObservationInterest.observe          ui_model/observation.py:174
  -> ReadingObservation.step                  ui_model/observation.py:259
  -> ThreadReading.read / located             ui_model/observation.py:906,928
  -> WireRevision.changed_threads             presentation.py:695
  -> each remaining view.notification_window.changed
  -> CapturedNotificationWindow.changed       message_notification.py:82
  -> ReadLedger.changed_notifications         read_ledger.py:211
  -> self.read()                              read_ledger.py:215
  -> LockedStore.read                         locked_store.py:1207
  -> JsonDocument._decode_capture / _decode   locked_store.py:431,461
  -> FieldCodec.decode_document(ReadDocument)
```

`WireRevision.changed_threads` checks the notification windows of remaining
readable threads at `presentation.py:724-727`. A captured window first checks
`NotificationEvidenceView`, then reads human display evidence when it has read
marks. Consequently this is not an unconditional full decode on every timer
tick: the source revision and window contents determine eligibility.

`WireRevision.reads` declares that ledger changes feed
`NotificationEvidenceView` (`presentation.py:616-622`); notification assignment
sources feed that view too (`presentation.py:636-641`). The investigation has
not measured which source changes trigger the observed bursts or whether UI
publication creates a repeated invalidation cycle. Trace the actual writers
before claiming a feedback loop.

`ReadLedger` owns `read_ledger.json` and `ReadDocument`
(`read_ledger.py:87,115`). The document includes message cursors, bus identity,
transcript state and notices. `changed_notifications` reads that whole document
once per invocation, then passes it to each mark's recipient comparison.
It does not decode once per mark; the repetition shown here is across window
checks. Recipient comparison semantics still need the complete trace below.

The existing `ReadLedger.read_conversation` (`read_ledger.py:123`) selects the
viewer's recorded incarnations and conversation keys through
`FieldCodec.decode_selected`. Reuse or extend the owner capability if it covers
notification marks. It still captures and parses the JSON document through
`JsonDocument._read_using`; selective nominal decoding alone does not prove
bounded file I/O or JSON parsing as history grows.

The source inventory used Python ASTs across 653 Core/hooks source modules,
excluding packaged skill resources, with zero parse omissions. It found one
production call site for `changed_notifications`, in
`CapturedNotificationWindow.changed`. That site is called for multiple windows;
one syntactic caller is not one execution. This inventory is a starting map,
not a complete trace of ledger lifecycle or all read consumers.

## Other captured paths and remaining traces

### Sidebar and unread metrics

Fifty observer samples included `HistoryViews.viewer_snapshot`
(`history_views.py:405`), which opens the presentation basis and calls
`Presentation.display_view_metrics` (`presentation.py:245`). That derives the
unread selection and advances the existing unread owner; one sampled stack
continued through `count_unread` into SQLite execution.

Trace the captured basis, selection, retained unread state and database query
plans. Establish which changes require recomputation and how work scales with
unread history and unrelated channels. A sampled SQL execution is not evidence
that it scans the full history or lacks an index.

### Main comms worker

Nineteen captured samples included:

```text
Coordination._read / _apply
  -> InputObservation._private_observation_revision   input_drain.py:485
  -> next_sealed_assignment                         coordination_cohort.py:245
  -> sealed_cohort_assignments                      coordination_cohort.py:289
  -> SQLite execution                              store_files.py:508
```

`next_sealed_assignment` requests `limit=1` and pending-state selection. The
query joins wake claims, batch membership, sealed batch receipts and delivery
receipts, scoped by recipient and sequence. A one-row result does not prove
one-row work. Inspect the real schema, indexes and `EXPLAIN QUERY PLAN` against
a representative saved database before prescribing an index or query change.

Trace what schedules `_private_observation_revision`, how often unchanged
owners are checked, and whether connection/snapshot lifetimes multiply the
query cost. Continue the other captured paths through `GoalManagement` goal
snapshots, registry snapshots and `AgentActivity` to their declarations,
writers and consumers. The current evidence identifies several paths; it
does not yet identify the dominant worker term or establish a busy loop.

### Connector and plan evidence

Sixty-one connector samples included this route:

```text
TeamHeartbeat.observe                          team_heartbeat.py:98
  -> TeamObservation.read                      team_heartbeat.py:40
  -> HeartbeatFrame.capture
  -> PlanReadings.observe_reading              plan_reading.py:83
  -> PhaseEvidence.recorded / MainEvidence.holds
  -> DeclarationEvidence.read                 phase_evidence.py:1267
  -> MainTree.module_bindings                  phase_evidence.py:168
  -> ast.parse
```

Other stacks included pattern-evidence Git searches and subprocess creation.
There are already retained owners: `PlanReading.reads`,
`RetainedPlanReadings`, `MainTree.bindings`, and `MainTree.following`.
`module_bindings` parses facts it considers fresh; it does not unconditionally
parse every requested module. Trace reading construction, source-blob identity,
invalidation and retention across served teammates and passes. Determine whether
this capture is first-use work, correctly invalidated work or lost reuse before
changing it. The connector had fallen to 3% in the final sample, so continuous
connector saturation has not been demonstrated.

## Cost model and choice of repair

For the established observer path, let W be eligible windows, L the bytes and
decoded content in the global ledger, and M the marks actually being compared.
The current route can pay roughly W times the global capture/parse/decode cost,
plus comparison of M. Measure these quantities on saved representative state.
The target is one coherent owner read of the required evidence and work bounded
by the affected selection, including its byte acquisition and parse cost.

Use the profile shares only to size the possible payoff: if 52% were a true
observer time fraction, eliminating that entire path would cap the observer
speedup around 2.1x. Eliminating a hypothetical 79% decode fraction would cap it
around 4.8x. Neither figure predicts total application speedup; the samples were
perturbed, decoding remains necessary, and the main worker was at 136% CPU.
Measure total process-tree CPU and interaction latency before selecting the
dominant repair. A route unable to close the measured gap is rejected unless
it is a necessary dependency of that repair.

Correctness, source structure, empirical work reduction and runtime improvement
are separate evidence. A shared whole-document read may remove W-fold nominal
decoding while leaving an L-sized parse on every change. Treat that as an
intermediate result unless measurements establish that it satisfies the full
workload target. Do not spend the work cycle tuning a codec leaf while its
consumer still requests unrelated global data repeatedly.

## Implementation sequence

### 1. Complete the owner and change-propagation map

Extend the AST inventory through ledger producers, recipient/read-mark
comparisons, human display acknowledgement, registry aliases and incarnations,
archive readers and bus replacement. Trace source capture, revision checks,
selection lifetime and publication back to the actual writer. Record each
required fact once with its owner and all production consumers.

Complete the main-worker and connector traces above. Preserve the existing
revision-source declarations, typed table/schema declarations and retained
plan-reading owners. If this plan is activated for fleet execution, declare its
phases through the existing `Plan` / `PlanPhase` family in `plan_phases.py` and
record work through Core's existing orders. This document does not claim that
those declarations or assignments have already been made.

Done: exact source paths identify which unrelated changes cause repeated work,
which owner can answer once, and where any incomplete trace remains.

### 2. Replay the workload and choose the dominant structural repair

Use a disposable copy of representative state under
`~/.cache/agent-scratch/`, preserving the saved source/runtime identity. Measure
quiet observation, one human read advance, one notification assignment, channel
traffic, changing thread selection, sidebar refresh and transcript navigation.
Vary unrelated history and visible windows independently. For the main worker,
vary settled history while holding pending work and the active owner fixed.

Record CPU seconds, event/pass counts, bytes parsed, nominal rows decoded,
queries, database plans and interaction latency. Distinguish cold setup from
repeated unchanged passes. Start long commands in the harness background mode.
Use short, sequential, low-rate read-only profiles only when a remaining trace
requires them; quantify profiler overhead before treating samples as timing.
Do not repeat the three simultaneous 100 Hz profiles that disturbed this run.

Done: the dominant end-to-end term and expected maximum payoff are explicit.
Prototype the proposed removal on the saved workload before full qualification.
If it does not materially reduce repeated work, stop and revise the architecture.

### 3. Repair the existing owners and remove competing paths

Put evidence capture and selection on the existing owning family so every
window inherits or consumes the same coherent answer; consumers must not each
assemble their own registry/document/revision combination. Preserve correlations
between the registry incarnation, bus identity, ledger cut and recipient marks.
An update during capture must retain the current stale/busy retry behavior.

First evaluate the existing selective read and snapshot mechanisms. If the
global JSON layout prevents bounded acquisition, inventory the existing typed
store and index capabilities and move the required facts to one indexed owner.
Preserve records with actual live readers through that owner's migration, then
retire the old authority and callers together. Do not create two writable read
ledgers, a hand-maintained notification index or a new revision roster.

For the worker, fix the scheduling, source lifetime or query owner established
by phase 2. For plan evidence, repair the existing retention path if the trace
shows reuse is lost. Share common behavior through the existing bases or
overlapping capabilities; keep genuinely different behavior in its members.
Do not mask repeated work with sleep intervals, debouncing, skipped evidence,
disabled observations or a parallel cache with its own freshness rules.

Done: the original consumer paths use the owner repair, obsolete global walks
and duplicate decisions are deleted, and local replay demonstrates the claimed
work reduction. Report any residual history-sized work explicitly.

### 4. Establish behavior, land and verify the installed application

Use the affected existing behavioral cases, including
`test_notification_display_viewer.py`, `test_notification_read_state.py`,
`test_thread_read_results.py`, `test_read_ledger.py` and
`test_recipient_activity_index.py`, selecting actual cases from the completed
trace. Human display state must remain distinct from delivery or execution.
Cover alias/incarnation replacement, bus replacement, archive boundaries,
concurrent source changes, malformed evidence and selection changes. Extend
family-level behavioral checks where the trace exposes a missing case; source
absence assertions and tests of removed helpers do not establish preservation.

Compare repeated replay with growing unrelated history and fixed affected
selection, then measure total process-tree CPU and UI latency without profiling.
The bounded-work claim must survive both quiet and active cases. For SQL work,
one returned row is insufficient evidence; retain the query plan and actual
work measurement. Reassess the dominant term after each decisive measurement.

Keep one coherent software PR for this issue with its evidence and source
judgment; land by the applicable team delivery route. Install useful landings
through the existing installer. Verify the installed build with a real live-agent
message and reply, notification/read updates, navigation and a fresh quiet/busy
CPU comparison. Merged, installed and seen working are separate outcomes.

Done: required behavior survives, affected work no longer grows with unrelated
history, aggregate CPU and interaction measurements demonstrate the improvement,
and the installed workflow works. If a captured process still burns CPU, record
its remaining trace and keep this plan open rather than declaring the ledger
change sufficient.

## Assumptions and boundaries

- The source-backed notification defect transfers to other windows that reach
  this same owner path. Its frequency and total payoff still require measurement.
- The recorded PIDs and short samples describe 2026-11-02, not current process
  identity or a performance target approved by Tristan.
- Raw profile shares overlap and are approximate. No measured speedup, causal
  invalidation loop, missing index or steady-state connector load is claimed.
- The existing papers and team rules govern implementation: paper1 Theorems
  4.6-4.8 and Corollary 4.10 for ownership and preserved required facts; paper4
  Section 4.4 for preservation evidence. Passing selected calls does not certify
  deleting a distinction used elsewhere in the admitted workflow.
