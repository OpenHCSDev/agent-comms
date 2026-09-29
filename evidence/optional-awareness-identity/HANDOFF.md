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
Parent can integrate disjoint351/350. Confirmation of Boyle's scope update is
still pending as of this receipt; no competing source has been integrated here.

Source implementation complete and reviewable in draft351. No installed or
painted live-readiness claim; final integration/activation remains parent-owned.
