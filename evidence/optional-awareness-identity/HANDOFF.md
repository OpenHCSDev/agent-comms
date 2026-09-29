# S14 optional awareness: canonical proof owners

194 production lines deleted, 277 added; tests 2 deleted,119 added. Base77c2ac67 after346. Scope is optional_awareness_projection.py and its direct wire tests only. Boyle retains admission/response/shared identities. Coordinated directly on347 comments5884463849/5884475744/5884497081; consumed RegistryOwner/OwnerGenerations/FrozenRecipient and canonical receipt tables, no parallel identity family or FieldCodec subclass.

IDEN-1/3, BOUND-1/2, MEMB-5: delete nullable flattened _SelectedDecision/_OpenObligation SQL projection schemas and their anonymous proof chains. Canonical WakeAssignment, ClaimBatchReceipts, ClaimBatchMembers, CohortDeliveryReceipts, AwarenessClaimGenerations, ResponseObligation, ExecutionRecord/ExecutionAssignmentLink decode once through existing TypedTable/FieldCodec. Bounded batch reads retain assignments as completeness anchor: any missing proof omits ALL optional context. Full typed record comparisons own each selected proof; inherited generation provenance classifies historical/current/future using existing OwnerGenerations, independently of wake authority. Added source lines express full canonical relationships instead of a nullable parallel row format; no net-deletion claim.

Deleted redundant type revalidation of trusted internal source-window inputs, retained value bounds. RegistryOwner now owns final live-inclusion check including goal and full ProcessIdentity. No SQL/schema/format/cutover change; existing read transaction and nonblocking registry lock order preserved. No wake/admission effect or cursor advancement introduced. Passive/no-wake behavior must remain separate from selected authority.

First actual disposable wire + SQLite lifecycle check:15passed6deselected9.29s. Covers complete decisions/obligations, old/new generations, stale snapshot, schema-transaction faults, generation race and receipt corruption. No fake provider/native results claimed. Resource warning: no newagents/globalNRA/large matrix/provider calls. Additional continuous wire case and bounded100-row timing pass below; changed-file ratchet has no positive delta. Source draft; no installed/live claim. Parent owns integration/live.


## Completed source acceptance

- Saved real wire/SQLite journey: **1 passed0.53s**, complete projection9.839ms.
  Selected channel message has one wake recipient and one delivery-only observer;
  subsequent passive notice remains passive. Only the selected execution has an
  open reply obligation. Projection preserves all assignments, obligations and
  original wire bytes. Reused numeric PID with different process birth is
  refused. Missing passive provenance omits the whole supplement.
- Full bounded100-row window: **1 passed10.75s**, projection29.195ms against the
  existing250ms foreground gate. Full content correctly exceeds optional16KiB
  budget and omits; a newest-only window from the same real history completes.
  This checks actual canonical batch read cost without providers/native agents.
- Initial new journey failed at a TEST lifecycle expectation: re-registering an
  owner advances admission, so the previous capture remains stale even after
  the process record is restored. Corrected the test to compare a forged capture
  against untouched canonical registry state; it still rejects identical PID
  with wrong process birth and proves original capture succeeds. Failed receipt
  retained in wire-journey.log; its separate generation-race case passed.
- Exact changed-file ratchet: TypeIdentity−10, LongBooleanChain−5,
  BooleanChainTerms−46, ForeignAbsenceProbe−4; no positive deltas, no codec
  subclass or god-class excess growth. Raw evidence compressed, summary retained.
  Ruff F/I and diff check pass. No full NRA/global correctness claim.
- Removed only completed own test roots after user-process reference check;
  source/logs preserved, live/global native packages untouched.

## Shared ownership note

PR350 subsequently advertised overlapping optional-awareness work after PR351
had already been published. Posted explicit correction on both PRs plus347:
Wegener owns this complete implementation; Boyle owns admission/response/shared
identity. No writes to Boyle's files or worktree, no new shared table API needed.
Existing RegistryOwner.require_snapshot API is already on the base and its
ongoing goal-sensitive capability remains compatible. AssignmentBinding only
represents ENGAGED work, so awareness does not use it to admit passive decisions.
Parent can integrate disjoint351/350. Boyle confirmed withdrawal on351#issuecomment-5884778006. No competing
source was integrated. He retains shared identity350, with API use coordinated
on351#issuecomment-5884819114.

Implementation and bounded installed wire acceptance complete in draft351.
No painted live-readiness claim; final integration/activation remains parent-owned.


## Current-main noneditable acceptance

Merged current main d4f09edf (348) into351 as6cc7fd4c; no source/awareness/helper
changes or conflicts in that incoming integration. Built the complete wheel at
6cc7fd4c offline and installed into an isolated owned environment using staged
runtime dependency versions. Python isolated mode, no source PYTHONPATH.

The SAME persisted-wire/SQLite passive/captured-owner/missing-proof journey
passed on the first installed run: **1 passed0.50s**, projection9.152ms.
The changed module matches the source commit, and every loaded agent_comms
module resolved within the noneditable candidate. Receipts: installed-imports.json,
installed-wire.log, installed-build.log, installed-setup.log; exact runner retained.
No new matrix/provider/native process/UI assertion; parent retains live gate.

After the test, no user-process command/cwd/executable/open-file references were
found to the candidate or test root. Those two owned disposable paths were
removed; source, wheel, logs and global native package remain preserved.
Protected OS process details that could not be read are recorded in
installed-cleanup.json rather than described as inspected.

Shared350 API integration is explicit: use RegistryOwner.require_active_turn
and WakeAssignment.source/Message.reference once350 lands. Those replace the
remaining local active-turn/value-pair reads without parallel helpers. This
checkpoint remains independently mergeable on current main with its existing
RegistryOwner.require_snapshot contract.
