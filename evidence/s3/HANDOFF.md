# S3 coordination lifecycle ownership — completed implementation

Branch: `codex/refactor-s3-coordination-20260927`.
Worktree: `/home/ts/wt/comms-refactor-s3-coordination-20260927`.
Based on S2 `cb54e15`; merged parent's PR146 coding head `305bdd8`, then current main through PR148. No conflicts or dropped parent changes. Parent owns integration/deployment; nothing was installed or restarted by this worker.

## Acceptance

- Claim, execution, attempt and response records now store nominal state members with state-specific data. Pending executions cannot carry an ordinal; active executions require one. Only published responses carry receipts. Terminal attempts cannot carry a lease or incomplete finality. Claim decisions distinguish pending policy from bound engagement.
- A1 membership owns names; A3 successors own transitions. The four Python transition views, new-store SQLite vocabularies/edge constraints, and recovery publication vocabulary derive from those declarations. Existing v2 databases are read without schema rewriting. Scalar enums are generated compatibility views for persisted strings and external callers, not independent state rosters.
- State behavior moved into owners: snapshot coupling, admission to pre-engagement/start/retry, recovery audit rules, response intent/receipt rules and wake expectations. Common snapshot invariants now have named membership, attempt identity, current pointer, route and receipt methods.
- A2 owns gateway DTO encoding/decoding on both sides, including required tags, strict Literal values and wire aliases. The client's five replica rosters and field mirrors are gone. Request encoding and gateway request parsing also share one typed declaration. The detailed snapshot derives from owner fields, redaction metadata and declared computed properties; its secrets stay excluded.
- Native FULL now reports events while the process runs. `DurableTurn` reuses S2's `TurnPhase` and A4 dispatch to persist prompt acceptance, model, overlapping tool and compaction progress under current CAS fences. The post-result synthetic phase loop is deleted. Final done/death is recorded only after native cleanup and any explicit owner effect. Native parsing/proof and PR95 exactly-once contracts remain intact.
- Normal `CodingToolMode` and explicit selected proof mode remain separate, as parent implemented. No second executor/tool policy, broker, claim store, model override or helper was introduced.

## Focused verification

Commands use the existing integration venv with `PYTHONPATH=src`, `-o addopts=''`, and owned `.artifacts/s3` basetemp. All shards bounded at 60 seconds. Counts overlap and must not be summed as a unique-suite total.

- Baseline: **200 passed**.
- Merged core/schema/gateway/codec/nominal shard: **230 passed**, 29.41s.
- Merged coordinated/native/coding/selected-tool/private-entrypoint/failure-recovery shard: **159 passed, 6 optional skips**, 41.08s.
- Actual prepared native CLI with loopback-only HTTP fixture: **5 passed**, 13.12s. Success/configured route, 429, output length and launch boundaries. Successful real child runs assert SQLite model progress before return and final done/death afterward. No provider credits or credentials used.
- New-case/data-shape tests: **11 passed**. A test-only execution state and response state each roundtrip real SQLite, declare an accepted SQL transition, and cross a real Unix socket into the existing client without client/schema roster edits.
- Golden graphs pin all original lifecycle names and edges. Replay against pre-S3 source: **68 identical snapshot projections, 324 identical claim/receipt legality cases**, plus an actual old-schema database reopened without schema rewrite. These are deterministic generated fixtures, not production captures.
- Shared request schema: **48 gateway/client/nominal tests passed**, then **11 malformed-request cases passed**, including absent/null/boolean thread fields.
- Final wake-frame narrowing: **2 passed**; real WakeDecision/NoWakeDecision union narrowing retained. Failed narrowing attempt is retained in its log.
- NRA: initial selected-surface scan **13 findings** (6 enum-case, 7 mirror); full-context closure scan **0 findings** across all 20 changed production modules. Success output contains no scan_status/detector-omission counters; no invented coverage counts. See `nra-acceptance.json`.
- Authored state/data/async consumer moves are not claimed as NRA native equivalence proofs. Native execution, local tests, source scan and replay are distinct evidence.

Prepared native package reused: `/var/tmp/agent-comms-pi-native-coding-20260927-x_nsd3jh/node_modules/@earendil-works/pi-coding-agent`. No native installs or duplicate environments.

## Changed production paths

- `coordination.py`, `coordination_store.py`, `coordination_cohort.py`, `coordination_response.py`
- `execution_states.py`, `attempt_states.py`, `claim_states.py`, `obligation_states.py`
- `wake_policy.py`, `recovery_states.py`, `coordination_errors.py`, `state_tags.py`
- `recovery_projection.py`, `recovery_gateway.py`, `recovery_gateway_client.py`, `field_codec.py`
- `coordinated_runtime.py`, `native_pi.py`, `durable_turn.py`, `wake_injection.py`

Tests: `test_coordination_nominal.py`, `test_coordinated_runtime.py`, `test_native_pi.py`, `test_recovery_gateway.py`.
Evidence: this handoff, legacy graph fixture, replay script/result, boundary counts, scan acceptance and final test logs.

## Scope and remaining work

No blocker remains in the assigned implementation. Parent still owns live activation and actual production channel acceptance. CI is deferred; no full-suite or deployment claim.

Historical recovery-only attempt values remain readable and explicitly advanceable. Native's configured no-auto-retry path does not fabricate recovery phases; its live durable projection covers actual acceptance/model/tool/compaction/settlement. This resolves S3 OPEN-1 conservatively without deleting stored values or inventing a new live phase authority.

The deliberately reserved `claim_admission.py`, `selected_write_plan.py`, and source/delivery recovery consumers retain their existing scalar policy comparisons via the derived compatibility tags. Their policy/claims/source internals were not changed. Darwin's ACP/session work and runtime/CLI ownership remain untouched. There is no need for either worker to wait on this branch.

Failed/timed-out/misaddressed earlier commands remain under `.artifacts/s3`; they are not passing evidence. Disposable successful-test directories and finished NRA caches are cleaned only after all child processes exit; compressed raw scan evidence and failure logs are retained.

## Resource cleanup

All task test/scan processes exited before cleanup. Finished NRA caches and successful disposable test directories were removed; raw scan output was compressed, and failed evidence retained. Owned artifacts reduced from 463MB to 42MB. No worktree, live data, or another worker's files were removed.
