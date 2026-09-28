# S3 coordination lifecycle ownership — implementation in progress

Worktree: `/home/ts/wt/comms-refactor-s3-coordination-20260927`.
Branch: `codex/refactor-s3-coordination-20260927`; started at S2 `cb54e15`.

## Current checkpoint

- Actual execution/claim/attempt/response records store nominal lifecycle members with state-specific data. Legacy scalar properties and construction keywords decode/derive at the compatibility boundary; the scalar enums are generated views of A1 membership, not independent declarations.
- State classes own successors, receipt/publication rules, execution/attempt coupling, claim decisions and recovery audit admission. Four literal Python transition tables replaced by derived views.
- Gateway projections and client share A2 field schemas. Public recovery vocabulary is derived from response declarations. Strict types, exact fields, bounded values and gateway privacy remain.
- Detailed snapshot projection derives from owner fields and explicitly declared redactions/computed properties; no parallel record mirror. A2 gained strict Literal support and read-only projection support.
- Baseline captured all four original names/graphs in `legacy-lifecycles.json`; checked before/after state migration.

## Evidence

All tests use existing integration venv, `PYTHONPATH=src`, `-o addopts=''` (xdist defaults disabled). Logs retained in owned `.artifacts/s3`.

- Baseline: 200 passed, 22.92s.
- Execution/response: existing focused tests passed.
- Attempt/projection: 177 passed, 19.33s.
- Record/projection: 172 passed, 12.04s.
- Current declaration/store/gateway/codec shard: 216 passed, 21.21s.
- New nominal graph/data-shape/schema/extension tests: passed (`nominal-checkpoint.txt`).
- Failed command with nonexistent `test_wake_injection.py`, positional-constructor failures and removed re-export import error preserved; fixed, not counted as passes.
- NRA before scan completed with full package context; findings/evidence in `nra-before.json`. Async/state ownership changes are authored transformations: not claimed as native NRA equivalence proof.

## Remaining closure

- Merge parent's published coding branch `305bdd8` (PR146), now explicitly handed over coordinated_runtime.py/native_pi.py. Preserve CodingToolMode and tool lifecycle/context generation fixes. Do not edit coding policy/broker/claim internals.
- Finish consumers and actual native-event durable attempt progress; remove the post-result synthetic phase loop.
- Derive SQLite state/edge constraints; preserve stored v2 values/semantics and test new-state store roundtrip.
- Finish wake-frame behavior migration and remaining cross-lifecycle rules; rescan with full context and acceptance tests.
- No final PR yet. No S3 completion claim or live deployment.

## Ownership

Parent owns integration, deployment/history and coding policy/broker/claims internals. Darwin owns runtime.py/cli.py and ACP session/configuration/transcript work in another worktree. This worker changes no live data and starts no helpers or provider calls. CI deferred; local bounded tests suffice.
