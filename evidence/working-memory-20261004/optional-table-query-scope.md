# Optional typed-table query fields

The installed authored W6 batch passed three controls and failed both historical-reader cases while creating a new private store, before App/native launch. The original `SnapshotMeta.select(connection)` is an unfiltered query; the c599ba0 keyed-query change incorrectly sends its empty filter through the required SQL column-list renderer. Existing WHERE-only and ordered reads have the same contract.

`TypedRow._fields_named` now owns declaration-backed subset resolution, including an empty query filter. `TypedTable.select` and `update` borrow those original field descriptors for encoded values. The column-list renderer separately requires a nonempty SQL list; updates separately require assignments. Unknown fields remain refused, request order in SQL lists and declaration order in bound values are preserved. Schema declarations/digests and caller APIs are unchanged. No SnapshotMeta or caller special case.

Before evidence uses the original NRA Package AST across 324 Core production, 366 Core tests, 288 Toad production and 394 Toad tests; zero parse omissions. Its 1,055 lexical sites include unrelated select/update methods, so they are not claimed as dynamically resolved table consumers. The relevant sidecar, annotation, typed-row and schema callers were read semantically.

Installed negative: 3 PASS / 2 FAIL, 7.428 seconds. App never mounted; no native child, prompt, provider, fork or actual Codex session read. Initial missing-pytest runner negative retained. A corrected runner borrowed existing system pytest after installed application import paths without dependency changes. All 1,470 original holder files, 69 packages and 390 protected proofs restored; privileged 229-process census has zero references and gaps across holder, authored fixtures and native keeper. Both execution purposes returned. No new installed loan or repeat inferred.

After source closure, one batch checks unfiltered and filtered reads, required update/SQL-list rejection, unknown fields, generated-field refusal and actual sidecar verification. A new installed W6 successor remains required for the App historical read/search/export path.

## Final source result

Seven proportionate controls passed in 1.06 seconds: the original typed-table family (unfiltered/WHERE-only/ordered/keyed reads and one, required update/SQL-list/unknown/generated-field refusal, strict stored values and transaction rollback), streaming query cursor ownership, real sidecar snapshot verification, and original working-memory read/correction/calibration controls. A fresh original Comms private initialization then completed in 0.278 seconds with schema 10. No App/native process/provider was involved in this source check. The normal main union includes Parent654/C4 and S4 source without altering frozen native producers.

The installed App remains unqualified. A separately issued W6 successor is needed to consume the corrected source; the original 3PASS/2FAIL and runner negative are not replaced.

## Changed installed successor (closed)

The exact corrected355-asset7d0f Core and unchanged319-assetc632 ToAd were consumed once under the e29f successor grant and Sch's separate matching7a2e READ renewal. The original no-key private sidecar failure no longer occurs: new private schema10 initializes, original controlled ModelLabel/Unclassified and HumanLabel author=user/Promised rows persist and decode through SpanAnnotationsRow. Native runtime input rows0, annotation requests0, bus0bytes. The authored fixture is retained intact.

The runner reached its original90-second bound without a completed App test result (exit-15/90.0326s). Its captured output supplies no coroutine/stage trace, so App mount/native-reader fault/precise blocked phase are NOT inferred. Source SessionLifecycle.bind_owned already starts RuntimeServer when enabled; no missing-start patch is justified. Existing App selected-content and sidebar readiness resources, AttachedSessionLifecycle proxy subscription and context inspection lifetimes remain the source investigation.

After terminal, no marked/root-owned processes remained. Exact original1470bytes/modes/links+69origins+390protected floor restored by normal filewheel installer, no metadata correction/no extraW6assets. Fresh privileged244-process census0refs/0gaps; whole style22 and7a READ/execution returned. Bohr separately closed the immutable issued purpose. No next package/import/run grant.

The existing ToAd App control now uses the unique original private root as its cleanup attempt and isolates XDG cache under that root. Its existing Core debug capability plus ordinary boundary output retains where an interrupted run actually waits; the next explicitly granted runner must keep that output (-s). These are unexecuted driver/isolation changes, not a production hang fix, App success or authorization for an unchanged repeat. No new native/protocol/frontend framework or timing guard.
