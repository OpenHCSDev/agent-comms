# P167 — change-driven drain recovery

PR1165; owner core-read-locks. Base main d8b72586c. P107 is frozen/reassigned;
this branch changes no P107 source, tools or evidence.

## Owner and consumers

- `AgentActivity.recover_drain` owns the original observed diagnostic check.
  It now checks that immutable observation BEFORE scheduling its existing
  fenced store operation. `_recover_drain` retains the owner and exact
  diagnostic/timestamp checks under the original lock.
- `drain_failures` owns the declared availability exception tuple, records the
  diagnostic, and propagates failure. Plain ValueError/TypeError are not
  availability; the observer stops and records StoppedDrainDiagnostic.
  Explicit RelationViolationError remains a typed unavailable source/identity
  proof, including a missing private checkpoint; it is not broad ValueError.
- `InputObservation.observe` catches only the declared deferred family and
  waits for WireWatch's next dependency/process-local signal. retry_observer
  and its 50 ms timer are deleted. The optional index shortcut likewise no
  longer hides plain ValueError/TypeError.
- `InputObservation.register_wake` owns task retirement. Failed/cancelled
  selected work does not manufacture another wake. Successful completion
  frees the slot and notifies normally. The duplicate selected-run cleanup
  and unconditional rescheduling are gone. Maintenance-held work does not
  count as a successful drain/recovery.

## Clean-path cost, before and after

Failure-wrapper store-worker dispatches per successful clean drain: **2 -> 1**.
Before: activity_of worker + unconditional recover_drain worker. After:
activity_of worker only; recovery returns before worker scheduling when the
observed value has no diagnostic. Actual clean-path SQL recovery writes were
already **0 -> 0**: the old recovery method already had the guard, but it ran
behind an unnecessary worker round trip. These counts describe this wrapper,
not all unrelated inbox/queue work.

## Source and verification

- Core src: four files, +74/-85, net -11. Final ratchet 0; no introduced
  findings. Whitespace and functional/import lint pass. Existing E731 in
  TickRevisionPaths.capture is unchanged.
- 40 existing checks pass, two existing input-drain cases skipped. Existing
  cases were migrated to the changed exception/callback contract; none added.
  The old timer-retry expectation now checks no retry until auth.json changes.
  The existing unexpected-error case exercises ValueError and Stopped status.
- Actual backend observation, native file watching, persistent SQLite schema
  failure/recovery, retained/concurrent diagnostics, active-turn recovery and
  owner replacement are exercised by that batch. Earlier retained batches
  exposed the deliberately changed retry expectation and the need to retain
  typed missing-source errors; neither is represented as a pass.
- Source/AST evidence: `.artifacts/p167/before-family.json`; final census and
  checks: `/home/ts/.cache/agent-scratch/p167-core-read-locks`. The existing
  NRA parser was reused; native TypeScript dispatch was not changed.
- Two separate manual fresh-root probes stopped at missing explicit package
  and missing participant setup. Both roots/diagnostics remain retained; no
  failed attempt was replayed and no provider input was submitted. They are
  NOT native/installed acceptance or measured cost evidence.
- Installed native/ACP/UI acceptance remains Codex's build handoff, not a
  claim from these source checks. No runtime publication was performed here.

No new timers, retry counters, diagnostic caches, tests or uncertain-input replay.
Only deliberately edited source/test/doc paths are staged in this branch.
