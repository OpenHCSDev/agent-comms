# Delete remaining routing and backend compatibility interfaces

Remove ResponsePolicy enum-style class attributes, derived compatibility alias injection, value adapter and resolve string/instance adapter. Message and historical views select concrete policy declarations; ResponseEligibility consumes its typed policy directly. Current tests use the same declaration contracts; the open family still exercises all five actual consumers.

Remove backend._JsonLineReader alias and _ACTIVE_STEERING_TASKS duplicate name. All backend construction and current test consumers use PiRpcChannel directly and the one authoritative steering map. No replacement facade or legacy constructor remains. Stored messages and native RPC records retain their single current external formats.

Local verification:
-123 routing/mention/wake/history/Pi RPC/summary cases passed;2 opt-in native skips.
-183 backend RPC/model/thinking/compaction/steering cases passed;18 unrelated cases deselected.
-Actual Python/native selected-summary RPC success and provider-error tests recorded in native.log.
-Ruff and diff checks passed; source/test/stack search has no removed-name callers. Current Toad has no ResponsePolicy callers or removed backend aliases.
-NRA full-src context scan attempted with one parse/analysis worker and165s bound, but timed out before emitting coverage. No complete NRA scan or native-equivalence proof is claimed for this authored deletion/caller migration. Runtime verification is explicit above.

Parent owns merge and live activation. Parallel workers own complete coordination-state and goal API deletion; this patch only intersects distinct caller changes in declarations/test_wake.
