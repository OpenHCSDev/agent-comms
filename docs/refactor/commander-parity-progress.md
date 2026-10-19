# Commander parity checkpoint (2026-10-19)

## Working source application paths

`CheckRoot._session` incorrectly called `NativeRecord.project` on an already
normalized `ExternalRecord`. The projection boundary already yields events.
`ImportedSessionWriter.import_events` now consumes those events, retaining each
original timestamp; the single caller no longer assumes a native record.
The replaced `import_record` method and its ImportBuffer/ImportLimits dependency
are deleted, not kept as an alternate path. Full session import still uses
`writer.append` and is unchanged.

Actual source CLI over the live saved `pi-plan-claims` native history:
`check-root --threads pi-plan-claims --into <owned scratch>/native-history-live-01
-- python -c <read and compare original/copy clocks> {root}` returned 0.
177 conversation events retained timestamps present in the original native
source. The disposable root was removed. No input was submitted or replayed;
original sessions, custody and uncertain inputs were untouched.

Actual saved `claude-code` external startup now refuses precisely:
`Copied conversation event has no original timestamp`. Read-only inspection of
its latest 200 source records found 164 ToolStartTranscript and 164
ToolEndTranscript events with no recorded time, plus 35 dated AssistantTranscript
events. No original time exists to preserve for those tool events. They are not
retimed to hide the mismatch. Full saved-external startup remains unaccepted.

## Partial parity, never full acceptance

Core declares all 33 matrix rows as classes, with native owning phases. Identity
comes from each declaration, not the prose title. The class registry drives the
runner and computed completion; the command decodes the Markdown matrix and
checks its row identities against the declarations. A class-owned checked task
must be accepted through the existing Todo order before that row is done.
Unspecified tasks and partial coverage cannot complete a row. No parity state
column, parity_done boolean or parallel evidence store was added.

Real isolated CLI worker compared native FieldCodec decoding with current
`commander.order.refuse`: 11 text cases across rows 9, 10 and 25 agree with their
expected outcomes. These are explicitly PARTIAL comparisons, not three finished
rows. The other 30 declarations have no observations. All 33 rows remain
unverified; no observed case failed. Report exit is 1. Each row reports its
class identity, number, native owning phases and coverage. Native owning phases
are attribution, not prerequisites on view completion.

`CommanderParityCheck.all_done` consumes accepted existing checked tasks; it
returns false for the current declarations. No script deletion is authorized.
The actual Cutover/ParityPhase source edit was denied by stale claims:
`Resource already claimed by 'pi-plan-claims'`. `parity-gate.patch` contains the
exact requested dependency and computed phase; it passes `git apply --check`
but has NOT been applied. The source after-line ratchet still returns 0.

## Confirmation and staging

After the coherent source changes: 13 focused tests passed serially
(test_check_root.py, test_commander_parity.py, test_plan_phases.py). These include
native clock preservation, dated external projection without text truncation,
refusal of undated source, actual subprocess worker, codec round trip and all
matrix declarations. Pytest reports its existing unknown `timeout` config warning.
No merged or installed build is claimed.

Before edits, refactor-audit Package.load parsed 380 production / 477 test / 34
tool modules with no omissions. AST evidence found one import_record declaration
and one caller; there are now zero of either, one import_events declaration in
native_entries.py and one caller in check_root.py. Runtime source dispatch is
polymorphic; actual native and external paths above corroborate its resolution.

Pending patches are source under this persistent checkout, never shell-applied
to denied files:
- parity-gate.patch: computed ParityPhase and exact four-part Cutover dependency.
- phase7-task-declarations.patch: peer-authored Record/Control task() factories.
- work-dispatch-followup.patch: finite positive deadlines and exact task identity.
- phase-task-consumers.patch: PlanIdle/proof consumers, against peer #1932.
- phase-task-tests.patch: fixture factory migration; state-only commander fixtures
  explicitly disable production starts. Do not run them with live factories.

P900003/P900007 were absent in the real global Todo store at this checkpoint;
this is not a reservation or permission to overwrite a later collision. No live
board record was created. Consumer staging depends on pi-cn-phase7 #1932; its
native dispatch proof is not whole-phase acceptance.

Scratch owner: pi-plan-claims. Only scratch root:
/home/ts/.cache/agent-scratch/pi-plan-claims (13 MiB at checkpoint); source checkout
27 MiB; free filesystem 25 GiB. Test subdirectories parity-worker-check,
parity-final-check and parity-confirm are disposable after active borrower
checks. Real-copy directories were already retired by CheckRoot. Patch handoffs
stay in /home/ts/wt/pi-plan-claims--P1. B shared GitHub claims remains unstarted.
