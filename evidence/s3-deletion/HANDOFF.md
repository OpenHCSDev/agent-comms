# PR151 nominal state deletion closure — in progress

Owner: Pascal. Branch codex/refactor-s3-deletion-20260928, persistent ~/wt/comms-refactor-s3-deletion-20260928, from PR159 final e0d24b5. Parent integrates/deploys; no live work/providers/helpers.

Requirement: complete current caller/state ownership. No old-client or internal compatibility API retention. Preserve actual saved SQLite/history data, one current external codec.

Implemented first checkpoint (not published/accepted yet): deleted state_tags.py and six compatibility tags; removed materialized transition/terminal collections; removed WakeClaim/ExecutionRecord/AttemptRecord/ResponseObligation compatibility constructors. Records require their typed lifecycle. Internal scalar projection names are removed; only explicit codec snapshot fields preserve the actual external protocol. Existing A2 codec now supports nominal class references for redacted projections that do not possess full state payloads.

In progress: SQLite decode/producer consumers, typed mutation APIs, wake/admission/runtime consumers, recovery socket models/client, all tests. No complete test claim until migration closes.

Darwin seam: DurableTurn observes native Pi events and calls MutationStore.advance_attempt. It will pass AttemptState classes directly, without AttemptPhase wrappers. Existing moved TurnRunner has no direct compatibility tag imports in the read-only source audit. Parent retains live activation.

Local incomplete checkpoint before syncing main829ce55: core migration shard 163 passed, 3 residual fixture getter failures corrected (not yet rerun); no acceptance claim. Failed logs retained in owned .artifacts/s3-deletion.
