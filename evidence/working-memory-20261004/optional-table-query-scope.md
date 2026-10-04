# Optional typed-table query fields

The installed authored W6 batch passed three controls and failed both historical-reader cases while creating a new private store, before App/native launch. The original `SnapshotMeta.select(connection)` is an unfiltered query; the c599ba0 keyed-query change incorrectly sends its empty filter through the required SQL column-list renderer. Existing WHERE-only and ordered reads have the same contract.

`TypedRow._fields_named` now owns declaration-backed subset resolution, including an empty query filter. `TypedTable.select` and `update` borrow those original field descriptors for encoded values. The column-list renderer separately requires a nonempty SQL list; updates separately require assignments. Unknown fields remain refused, request order in SQL lists and declaration order in bound values are preserved. Schema declarations/digests and caller APIs are unchanged. No SnapshotMeta or caller special case.

Before evidence uses the original NRA Package AST across 324 Core production, 366 Core tests, 288 Toad production and 394 Toad tests; zero parse omissions. Its 1,055 lexical sites include unrelated select/update methods, so they are not claimed as dynamically resolved table consumers. The relevant sidecar, annotation, typed-row and schema callers were read semantically.

Installed negative: 3 PASS / 2 FAIL, 7.428 seconds. App never mounted; no native child, prompt, provider, fork or actual Codex session read. Initial missing-pytest runner negative retained. A corrected runner borrowed existing system pytest after installed application import paths without dependency changes. All 1,470 original holder files, 69 packages and 390 protected proofs restored; privileged 229-process census has zero references and gaps across holder, authored fixtures and native keeper. Both execution purposes returned. No new installed loan or repeat inferred.

After source closure, one batch checks unfiltered and filtered reads, required update/SQL-list rejection, unknown fields, generated-field refusal and actual sidecar verification. A new installed W6 successor remains required for the App historical read/search/export path.
