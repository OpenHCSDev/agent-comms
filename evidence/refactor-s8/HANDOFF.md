# S8 implementation checkpoint — 2026-09-27

Owner: authorized Codex S8 worker, only `/home/ts/wt/comms-refactor-s8-goals-20260927`, branch `codex/refactor-s8-goals-20260927`.

Exact owner directive: "and makenthem be aggressive in yhebrefactor, large highvleverage butes, no incrmental busywork, we have itblaidbout clewrly".

Started from A8 b97a108 (A1/A2 reused); main 0309f7b merged normally. S1 7c0b7f4 is ready for normal merge; goal ACP/event ownership transferred in dispatch README. Parent retains runtime/recovery/native/diagnostics; S4 owns read/routing. No live changes, providers, agents, replay, deployment, restart, or PR merge.

Implemented independent domain/action closure: GoalState and PauseSource on existing DeclaredFamily, shared registry-free LifecycleState ABC mixed into each domain family; no second lifecycle registry. `successors`, `may_become`, `transition_table` are the A3 API for future S3. Command supplies A2 decoding; GoalAction's shared apply owns CAS, actor checks, registry/history publication, wait cleanup and audit. All eight legacy actions now have classes and fields; model choices derive from capability membership. Goal stores one state; dataclass status/reason/source fields are read-only boundary projections preserving asdict/replace consumers. Paused source persists with the goal; legacy unmatched pause defaults to OwnerPause. Existing pause document is audit/legacy ingress only.

NRA inspected current CLI, catalogs/public API, PatchTargetOperation and the OOPSLA manuscript paper1_jsait (required-answer fidelity and no-free-erasure). Package-context before scan hit the contextual detector deadline; no global claim. Presentation recipe simulated with valid syntax and reviewed diff; application receipt retained. Most moves are authored because deciding ownership/transaction effects is not established by syntax replay. Behavioral tests, not that replay, establish runtime behavior.

Evidence so far: core-first 39 passed; standby/input-review/resume 45 passed. State shard found five obsolete expectations (model pause and absent attribution); updated tests reflect explicit S8 contract, final rerun pending. Completion not claimed. Remaining: S1 event and ACP goal migration (G5), retry migration, attempt lifecycle, new-case A/B/C and golden records, focused integration shards, NRA rescan, formatting/type checks, publication and cleanup.
