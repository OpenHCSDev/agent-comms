# PR151/S3 complete nominal state caller and deletion closure

Owner: Pascal. Branch `codex/refactor-s3-deletion-20260928`, persistent `/home/ts/wt/comms-refactor-s3-deletion-20260928`. Parent owns integration/activation. No live data, processes, provider calls, models, or helpers changed.

## Source and state

Based on main `326a04b` (PR163), including `829ce55` TurnRunner/OwnedTurn. PR159 was handed off first; this branch changes only the S3 surface and current consumers. Full working source is in this PR; no installed-runtime claim.

## Deleted mechanisms

- Deleted `state_tags.py`, both factories, and six generated StrEnum views: WakeMode, ClaimDisposition, ExecutionStatus, AttemptPhase, ObligationState, RecoveryKind.
- Deleted TriageVerdict's now-unused internal enum and WakePolicy.engagement_verdict duplicate. Engagement/claim owners determine verdict; preengagement callers no longer pass a redundant verdict argument.
- Deleted four module-level transition views and active/terminal attempt/execution rosters. Edges and capabilities come from actual state declarations. Tool admission uses AttemptState.allows_tool_admission instead of a four-case caller roster.
- Deleted all four dual-shape record constructors and their old internal scalar getters. WakeClaim, ExecutionRecord, AttemptRecord and ResponseObligation require one lifecycle object. No old constructor/API fallback remains.
- Replaced internal string coupling from execution to claim with ExecutionState.claim_state(), shared by snapshot validation and SQL projection generation.

## Complete consumers and boundaries

SQLite decoders build lifecycle objects once; mutation, cohort, publication, admission, wake, selected write, source coverage and native DurableTurn consumers use their behavior/data directly. Foreground CLI serializers use declaration names. Recovery socket records refer to the actual nominal declaration classes; A2 FieldCodec supports those references and uses the same owners for both socket encoding and decoding. No second roster/codec.

Only explicit `snapshot_*` codec fields preserve the existing external snapshot schema; they do not expose the removed internal API. Current SQLite values, schema and stored history remain unchanged/readable. Capture tests pin persisted names/edges and roundtrip tests cover the current state API. No obsolete-client coexistence fixture or gate.

Current TurnRunner/OwnedTurn has no removed state-API calls and required no edits. Current Toad live-pins/export-caller source audit found no calls to the deleted APIs. Parent's concurrent backend/ResponsePolicy deletion is disjoint except three existing ResponsePolicy constants in tests/test_wake.py; S3 changes in that file only migrate wake states/assertions.

## Local acceptance

Integration venv `/home/ts/wt/comms-refactor-integration-20260927/.venv`, `PYTHONPATH=src`, `pytest -q -o addopts=''`, each shard shell-bounded at60s. Reports retained alongside this handoff. Counts overlap and must not be added as unique tests.

- `core5.txt`:249 passed. Coordination/schema/store/nominal/wake/cohort/publication/recovery projection/gateway/client. Real SQLite transactions/reopen/rollback and Unix sockets, frozen publication receipts/uncertainty, graph/data/new-state tests.
- `runtime2.txt`:114 passed. Coordinated runtime, native failure recovery, admission verifier/baseline, ACP selected write, ordinary N/K delivery, FieldCodec. Verifies actual composed local runtime with offline provider fixture and strict nominal class-reference boundary.
- `seams.txt`:158 passed,7 skipped. Store rerun after removing verdict duplicate, foreground/NK child processes, wake candidate indexes, native prompt binding/native Pi tests. Skips are existing opt-in native/environment checks; no paid provider execution.
- Foreground fixtures migrated to current saved model selection, actual packaged coding extension bytes, and Response/ContextCommitted observations; provider boundary remains fake. The child process, bus/SQLite path, custody/uncertainty and nonreplay checks are real.
- Ruff on changed Python files and `git diff --check` pass.

Failed/intermediate evidence is retained as gzip logs, with authored migration scripts. These were source-directed transformations where NRA DSL native equivalence was not established; passing tests are the behavior evidence. Architecture scan coverage is recorded separately below.

## Remaining scope / blockers

No known S3 implementation blocker. CI deferred by owner. Parent integrates and activates; installed current-provider/UI acceptance remains parent-owned and is not claimed by these local checks. Existing unproduced historical durable phase names remain part of actual persisted data, not an old-client interface.

Exact changed production/test paths: [changed-files.txt](changed-files.txt).
