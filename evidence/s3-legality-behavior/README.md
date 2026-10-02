# Original S3 acceptance5: executed predicate/coupling closure

Scope: tests only. No MutationStore, table-owner, production, live or native edits.
Boyle retains MutationStore refactor; this branch adds a separate test module and
removes12 overlapping assertions from test_coordination.py, preserving immutability,
failed completion evidence and every SQL commit/reopen test. No compatibility.

Original47 predicates recovered from f9854ab AST, not diagnostic-name matching.
predicate-behavior.json maps each exact original condition and description to an
executed family-level test and states its proof kind. This is an immutable audit
receipt; it never registers cases or drives test/production family membership.

## Actual owners and coverage

- Assignment policy/decision/binding derives from WakePolicy, AssignmentState and
  BoundAssignment declarations; test every maintained production policy and bound
  leaf. Check positive sequence/revision, chronology, wrong verdict, prebinding
  target rejection, missing execution/target and a valid exact engagement.
- Execution references/budget derives from ExecutionState declarations, including
  unstarted/no-ordinal, required ordinal, post-attempt retry headroom, numeric/time
  bounds and every actual ExecutionOrigin route requirement.
- Replay assessments exercise revision, unknown bit mask and every declared
  ReplayFact against replay-safe rejection plus accepted unsafe controls.
- ResponseState derives all current leaves. Test complete/partial/absent/nonpositive
  receipts and the four intent/receipt presence combinations for each leaf;
  declaration capabilities determine their permitted relations. No state roster.
- Actual MutationStore-created pending/active snapshots anchor constructor/codec
  behavior. Negative mutations cover record/link membership/order/owner/disposition,
  attempt identity/phase family, exact active pointer/version/route and target.
- Attempt terminal and response families exhaust current retry/success/failure
  partitions. Deferred authorized retry stays possible, unauthorized retry fails,
  failed cannot be authorized, completed wire requires terminal successful response,
  and failed cannot erase a publication receipt.
- Real message authority constructs a valid intent+published receipt snapshot.
  Receipt envelope fields derive from the receipt dataclass; mismatch checks cover
  all frozen components. Canonical publication key rejects at its own constructor.
  Earlier receipt/parent and exact-route checks make the old late parentless-receipt
  guard unreachable; execute both current refusal paths rather than fabricate an
  invalid object with object.__new__ or bypass the boundary.

## Executed evidence

installed-final.log:111 passed in12.56s against the noneditable combined-core wheel.
24 family-level cases plus78 existing actual SQLite authority/coupling checks and
9 nominal/new-declaration/codec cases. Real SQLite includes transactions, rejected
commits, rollback/reopen, cross-process/concurrent initialization and publication
validation. No mock store or live root used. CI deferred; full suite not claimed.

installed-boundary.log retains an earlier red receipt (101passed,1failed): the
fixture tried corrupting publication_key at the envelope layer although its typed
receipt constructor correctly rejected earlier. Fixed test now asserts that owner
and continues deriving remaining envelope fields. family-fixed.log records24passes.
No production defect was hidden or reinterpreted; no weakened behavior assertion.

This closes original S3 acceptance5 legality behavior, including the three
nullable/shape checks now impossible under current variant owners. It does not
prove old transition-table equivalence, historical stored-data compatibility,
complete NRA detector coverage, universal size limits or a full-suite gate.

No runtime format reset or installation needed for test-only changes. Parent owns
production integration/install. Source branch test/s3-rule-behavior-20260928.
