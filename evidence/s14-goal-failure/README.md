# S14 passive goal failure

**95 production lines deleted; 160 added.** Production checkpoint includes `e8df0fdd` plus the three caller migrations based on main
`95a792db`, including350/351. Boyle owns353; parent owns integration/live install.

## Canonical owners and deletion

Delete the23-term FailedTurnObservation identity/type/state conjunction,
reader identity tuples and external goal/generation/attempt type dispatch.
FailedTurnEvidence owns its existing durable constraints and projects the actual
OwnerIdentity/ThreadIncarnation/TurnFence. Thread observes its active or last
finished turn (never a begin-turn grant) and owns absent-goal handling; Goal owns
same-goal revision acceptance. TurnFence matches its complete turn witness.
Reservation/Generation/evidence share GoalAttemptIdentity, with no token/grant or
new store. Existing lifecycle declarations own failure eligibility and backend/
pause presentation. Full reservation equality still protects observation binding;
identity alone grants nothing. Stored SQL/wire fields and public projection stay
unchanged. Terminal caller already supplies these exact nominal owners; no old
constructor adapter or compatibility path is introduced.

Exact process identity, incarnation/worktree, admission/turn, goal/revision,
reservation/generation and failure-phase fences remain. FieldCodec/TypedTable
remain the sole stored decoder. Observation insertion stays within its existing
savepoint: failure can omit the observation but cannot turn the attempt into
success or undo failure settlement. Reader still opens query-only, never
creates/repairs/migrates and never grants retry. UNKNOWN inputs remain untouched.

Latest NRA + exact refactor-audit ownership: IDEN-1/3/8, IMPL-10, BOUND-1/2,
TIME-9. No per-predicate rule family, registry duplicate, codec subclass, native
producer change or startup/scheduler overlap. Existing Thread/goal/turn owners
are below500 and can absorb their own behavior under principle13. The large
GoalAttemptStore shrinks: human abandon/retry/retire now compare the same actual
reservation identity, retaining their existing phase and explicit decision fences.

## Evidence

- First setup RED is retained compressed: scratch parent was absent before any
  product test. Corrected setup run:37passed,8failed46.42s. All8 failures were the
  fixture's missing PI_COMPACTION_TEST_PACKAGE setting, before changed behavior.
- [Corrected terminal family](terminal-corrected.log): **8passed47.51s**, actual
  ledger/ACP settlement with controlled event streams: failed result/EOF,
  savepoint ABORT/ROLLBACK, owner pause, preserved UNKNOWN/started inputs and no
  schedule. Together with37 real-ledger tests above, all45 cases are covered.
- [Actual native journey](native-final.log): source wheel installed noneditable;
  actual pinned package d396, retained saved history, ACP new/load/prompt, normal
  autonomous goal entrypoint, loopback503 provider, persisted failure projection,
  resume refusal, original UNKNOWN preserved and exactly3 total native inputs
  (seed, explicit user message, one failed goal turn). No native/UI/protocol mock
  and no paid provider. **1passed11.32s**.
- Initial native test RED preserved: it incorrectly expected a direct user
  message to spend a goal grant. The actual path correctly left that generation
  READY. Corrected journey uses the existing autonomous owner entrypoint; no
  production behavior was changed to satisfy the bad expectation.
- [Changed-path measures](changed-measures.json): -40chain terms/-5long chains,
  -3foreign absence/-3god-class excess; no positive measure/god excess/codec growth. Bounded source
  comparison only; no optional census/global NRA rerun. RuffF/E9/I +diffcheck pass.

- Final shared-identity caller checks: [attempt lifecycle](attempt-callers.log)
  **3passed1.58s**, [explicit abandon/retry/retire](human-callers.log)
  **4passed0.71s** against the installed final caller changes. No optional matrix.

## Remaining independent item

[Literal reproducer](literal-reproduction.json) confirms BOTH packaged
StringSubscript and exact archive StringKeySubscript count a one-string Literal
annotation as a runtime dictionary operation. Boyle owns correction in a separate
scoped ratchet PR; Noether158 is notified, with no annotation hiding/waiver. This
measurement defect does not affect353's unchanged StringSubscript delta.

Wegener owns selected scheduler/coordinated_runtime; Carver owns startup;353
edits neither. Whole S14/T4 remains incomplete. This is source plus isolated
installed acceptance, not a claim of parent live cutover. Own scratch/venv remains
temporarily for the immediately following ratchet correction; no live root,
provider settings or predecessor work was touched.

The existing passive projection is a documented pilot API, not an existing UI/RPC endpoint (docs/audits/passive_failed_turn_observation_20260926.md). Its output is not claimed painted/live. The production terminal failure callback is exercised through the actual native goal journey.

## Parent deployment checkpoint

Per parent: core350/351/352 at95a792 and Toad158/159/161 at510716 are merged. Paired installed continuous native/MCP journeys passed; nine idle owners activated on identity-mcp and nine fresh ACP loads passed. Actual saved UI paint is still being checked. Current remaining ownership is updated in the existing plans/remaining-plan-closure-20260929.md; no standalone census rerun.353 itself is not claimed installed.
