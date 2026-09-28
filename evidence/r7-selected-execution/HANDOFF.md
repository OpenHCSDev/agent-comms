# R7 selected execution + S5 vocabulary closure

## State / ownership

Published draft: https://github.com/OpenHCSDev/agent-comms/pull/201
Implementation `8cc8566`; current main198 reconciled at `e1a84f0`.

Subsequent original-plan audit: see
`../original-plan-completion/ORIGINAL-PLAN-COMPLETION-AUDIT.md`. The combined
R6/R7 candidate has the major replacements, but the audit found remaining
assignment/lease locals named claim/turn_claim (O1). This qualifies the earlier
vocabulary-complete wording below; current APIs are deleted, those names remain.

Complete source candidate on `refactor/r7-selected-execution-20260928`, own tree
`/home/ts/wt/comms-refactor-r7-selected-execution-20260928`, based main199 `065a4da`.
Parent owns installed acceptance, paired Toad and activation; Pascal owns R6.
No live state or native package was changed, no paid providers/extra agents/models.

## Actual replacement and deletion

`SelectedExecution` replaces the old 624-line `run_one_sealed_claim` transaction
and its procedure-local state. Its single-use instance owns selected assignment,
canonical turn lease, sealed source, native session, prompt/binding, reserved
input, TRIAGE promotion/IGNORE, FULL result, owner tools, response and cleanup.
Its run permit rejects both concurrent reuse and a second run after completion.
`DurableTurn` remains the one evolving attempt-fence owner; the former copied
fence and nonlocal observation closure are deleted. The start result stays local;
the selected assignment and obligation become the actual component state.

Moved source selection, reservation, verification, cursor, triage/full receipts
and native-send admission onto that owner. Their repeated store/bus/owner/source/
session parameter bundles are removed. Native raw admission still opens a fresh
SQLite connection on its isolated writer and retains wire/bus/registry/SQL locks
and its one-use token; it never borrows the event loop's connection. Owner effect
admission is constructed in one method for both normal tools and the explicit
operator plan. No second executor or shared-self facade exists.

ACP and both foreground paths construct the component directly. The obsolete
ForegroundOwner injectable-coroutine executor argument is deleted. Native tool
policy, MutationStore transitions, lease fences and UNKNOWN/no-retry rules stay
with their existing owners. Exact participant/admission checks still guard send,
owner side effects and publication; cleanup releases only the captured lease.

`assignment_states.py` replaces deleted `claim_states.py`. WakeAssignment,
AssignmentState and their cases, execution links, store/cohort/source/prompt
records and all attention APIs use assignment vocabulary. Resource ownership
still uses claims. Registry lease methods replace turn-claim names. All internal
Python identifiers containing epoch are migrated to generation; counter meanings
are unchanged. ParticipantSnapshot's duplicate generation/participant_generation
accessor is removed: participant_generation is the actual declaration field.

Removed ProjectionRecord and its to_primitive forwarding; gateway/current tests
use FieldCodec.encode. Removed RecoverySnapshot.to_primitive; snapshot callers
use FieldCodec.project(snapshot, "snapshot"). Actual FailedTurnProjection has a
distinct redacted schema and is outside these removed forwarding interfaces.

## Boundary preservation

Existing SQL schema/digests, native receipts and on-disk field names remain.
FieldCodec metadata declares the retained external claim_id/claims/wake_claim_id,
generation/owner_epoch/owner_admission_epoch spellings on renamed fields.
Compaction attestation/source receipts use this codec, rather than exposing new
internal names through asdict. WakeAdmission's old duplicate key roster and
constructor decoder are deleted in favor of FieldCodec; its required version
remains required. No live migration, historical replay, ID reallocation or
old-client alias is introduced.

`CALLER-CONTRACT.md` lists the current API/deletion map. Pascal's only observed
R6 production overlap is three `person.participant_generation` accesses in
ThreadManagement rename; exact patch is `thread-management.patch`. Transcript,
native-entry and replay files are untouched. Parent Toad test migration:
`channel_history_reader_pilot.py:58`, claim_local_turn -> lease_local_turn.
Existing Toad wire cursor spellings remain unchanged.

## Executed evidence (counts are batches, not additive distinct totals)

- `owner-closure.txt`:95 passed, selected transaction, stale owner/failure,
  triage/full/publication, projections, saved claim admission and verifier.
- `declared-boundary-final.txt`:158 passed, coordination/stores/cohort/response,
  tool policy/broker and concurrent/completed execution reuse refusal.
- `generation-boundary.txt`:102 passed/21 skipped. Compaction/admission/queue,
  registry/identity/lease consumers. Skips require the opt-in native package.
- `source-current.txt`:49 passed, optional awareness, native cursor scale,
  ACP selected plan and real foreground subprocess consumers.
- `admission-tools.txt`:67 passed/1 skipped with one earlier fixture failure
  (asdict leaked the renamed admission field). That fixture is fixed and passes
  in owner-closure; this file is not represented as an entirely green batch.
- `caller-closure-fixed.txt`:156 passed/1 skipped with one stale maintenance
  process fixture. `maintenance-current.txt`:that repaired case passed. The
  noncoroutine ForegroundOwner caller and retired Comms fixture receivers were
  migrated; no deleted API was restored.
- `native-four-tools-canonical.txt`:1 passed, integrated SelectedExecution with
  actual pinned Pi and localhost deterministic provider. All four normal tools
  read/edit/write/bash execute, files agree, one response is published, resource
  claims and exact turn lease are released, and a second run cannot send.
- `native-compaction-seam.txt`:3 passed, actual native local-fake original/queued
  summary variants and native commit after generation/attestation migration.
- `nra-final.json`:79 detectors/no omissions, full package dependency context,
  zero selected-file findings. This is an architectural scan, not a claim of
  native equivalence for the authored component transformation.

## Earlier attempts and actual limits

Two combined source/cursor attempts exceeded60seconds during large-scale fixture
work; partial dots are not passes. The later49-case source-current batch is green.
The very large1001/1002-row checkpoint acceptance was not rerun to completion;
R7 changes its current APIs, not checkpoint storage. Existing parent migration
and checkpoint receipts remain separate evidence. Initial native four-tool run
selected the /home package and was correctly refused before send; using the
existing canonical verified /var/tmp package passed. Neither was rebuilt.

No remaining identified product failure. Installed configured-provider, paired
Toad and rollout acceptance are deliberately parent-owned and not claimed here.
CI is deferred per owner. No source hold is imposed by this handoff.

## Integration / rollback

Merge this full branch into parent's current R6 integration. Preserve R6's three
rename-method hunks while applying the narrow participant field changes; no
transcript schema edit is present in R7. Apply the one Toad test lease rename,
build isolated candidate and run parent's usual installed checks. Normal serial
activation retires old owners before new owners start. R7 adds no data migration;
rollback is package selection with the same persisted/native formats. Do not
replay UNKNOWN. Preserve the published source/evidence; discard only owned test
caches and temporary transformation scripts once local processes finish.

## Final cleanup and deletion check

Owned `.audit-work`, temporary transformation scripts and census scratch are
removed. Persistent source, runnable tests and all receipts are retained.
`deletion-check.txt` reports zero obsolete selected/attention/lease APIs and zero
Python identifiers containing epoch in production (external string spellings
remain by design). No native bundle, live root or other worktree was removed.
