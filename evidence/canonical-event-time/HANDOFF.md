# Canonical original transcript time

Base: main `01a9b7dd`. Owner: native/backend sidecar. Tesla owns Toad142 callers and installed return-paint acceptance; Carver T4 AgentProcess and Noether151 plan boundary are untouched.

## Contract and ownership

BOUND-1/BOUND-2: `NativeEntry.timestamp` remains the external native journal ISO8601 value, without changing saved files or proof readers. Its existing `events()` boundary now decodes the clock once per native record before projecting all parts. Message and compaction declarations own content through `_events()`, while their common parent guarantees every projected part retains its source time. `TranscriptEvent.timestamp: float | None` is Unix seconds including fractional seconds, transported by existing FieldCodec through ACP. Missing, malformed or timezone-less external time stays unknown; never substitute the current clock. Live receipt time is a separate UI decision.

TIME-9: no codec subclass, time wrapper, parallel timestamp store, migration or alternate runtime schema. Existing dataclass replace/StreamingMerge automatically preserves time and refuses merging different recorded times. Actual native timestamps remain exact external ISO strings; ACP uses the declared seconds projection.

## Deletion and completion

Two subclass overrides of the full projection boundary are replaced by content hooks; no consumer can omit the clock for a content subclass. Core has never fabricated a replacement timestamp; that default is in Toad, owned by Tesla. This urgent bug adds a missing fact, not a net deletion claim. Deferred selected dry-run RPC deletion has no implementation edits.

Initial boundary checks: five pass (external offset/fractional times, split/routed input, compaction, merge, and unknown time). Actual native plus ACP transport/rebuild and installed Toad return-paint are pending. Production is not installed by this branch.

## Actual boundary acceptance

- Initial run:26pass,1failure (obsolete merge test still implemented removed `replay_update` instead of required `routed`). Corrected the fixture to declare its actual routing behavior; no production compatibility API restored.
- Final followthrough:8pass in4.07s, including actual pinned native local-provider request, native user/assistant ISO timestamps, official ACP SDK over real localhost byte streams, TWO production `session/load` snapshot publications, and fresh bounded page rebuild. Same two event times throughout; native JSONL and input-proof bytes unchanged; exactly one loopback request. All test-owned native and ACP resources closed.
- The first three socket harness attempts were interrupted during teardown; they are retained as unsuccessful attempts. Python3.14 server `wait_closed()` waited for the accepted socket whose lifecycle belongs to the test server; final harness closes each accepted writer and joins handler tasks before awaiting server closure. No production teardown changes or success claim for those attempts.
- Full-context NRA source scan:243 files indexed,18.81s, zero raw findings on the two selected report targets. Full payload does not emit detector/omission metadata; do not infer a complete clean architecture verdict. Author-authored boundary change, not an NRA equivalence proof.

## Explicit local R0 conflict

Independent class-size measure reports NativeEntry+17, TranscriptEvent+2; other ratchet measures unchanged. This missing-feature addition extends the two existing correct authorities. No facade/alternate owner introduced to evade the measure. Parent review has the exact conflict in337comment5883327190; no CI wait or deployment performed. Installed Toad142 return-paint acceptance belongs to Tesla and is not claimed by these core results.

## Installed core receipt

Noneditable wheel built with `uv build --wheel`, installed with `uv pip install --no-deps --target .artifacts/canonical-event-time/installed`. Verified import resolves inside that installation, not `src`. With existing runtime dependencies and immutable5fde native package,9 targeted tests passed in4.20s, including actual native + official ACP load/reload + clean teardown. New native content declaration inherits original time without any central dispatch change. Initial parametrized new-case test redeclared the same nominal family twice; fixed the test to declare its new case once, retaining the failed receipt.

Production diff:2 lines deleted,22 added. Test closure deletes the obsolete replay_update fixture method and replaces it with the actual routed behavior. Net production addition is required original-time propagation, not a claimed deletion-only refactor. Latest NRA/refactor-audit22:16 skill reread; BOUND-1/BOUND-2 and TIME-9 decisions above applied. Exact contract and candidate sent directly to Tesla142; Carver/Noether notified of disjoint scope. No paid provider calls or live owner changes.
