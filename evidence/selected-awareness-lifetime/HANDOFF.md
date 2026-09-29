# Selected awareness lifetime and result ownership

## Deletion first / scope

Base live main12bd75ca. 181 production lines deleted,155 added in the two existing owners at first checkpoint. Delete `OptionalAwarenessSupplement`, `_bounded_optional_awareness`, `_production_optional_awareness`, the `SelectedExecution.optional_awareness_builder` injection field, duplicate runtime result validation and the projection JSON -> runtime decoder. No compatibility API, converter, new registry, extra cap or store. All source callers and direct tests migrated in place.

Existing `OptionalAwarenessProjection` now owns capture of the selected window, the one admitted background reader, its completion/cancellation/deadline lifetime and prompt rendering. Existing `OptionalAwarenessResult` becomes an ABC: `CompleteAwareness` retains actual captured typed source/obligation records, while `OmittedAwareness` owns omission reason and behavior. An omitted result has no partial text or pretend-complete flag. `SelectedExecution` only awaits the projection's result presentation. The result carries no action, send, claim or cursor authority.

Read NRA and exact authoritative refactor-audit.skill SKILL, pattern README and IMPL4/8/9, BOUND4, TIME9. The semantic fixes are shared declaration-owned result behavior, complete-vs-omitted state construction, removal of shared closure plumbing and removal of JSON reparsing of our own result. Existing context-byte and final-prompt-byte checks retain their distinct units and exact defaults. Existing250ms/single-reader bound remains: a cancelled/timed-out read keeps the sole slot until its daemon finishes; default executor remains available to mandatory input work. No new resource limits.

## Ownership and caller closure

Claim sent directly on Boyle361. Boyle retains registration_change, registry_document, registration, thread_identity, threads, turn_lease and claim/admission/shared identity. No edits there. The current source/provenance owners from351 remain authoritative; this closes their runtime consumer/lifetime remainder. Parent owns integration/live installation. Startup359 unchanged and no replay/paid calls.

`rg` confirms no remaining source/test references to the retired supplement, free builders, optional-builder field or mandatory_complete flag. Tests now inspect typed source context and actual rendered output, including stale/other-owner exclusion; they do not resurrect the JSON payload API.

## Verification boundaries

- `focused-first.log`:29passed,3failed,54deselected34.27s. Three failures were remaining tests reading deleted `.text`, after their authority assertions had passed. Preserved as failed, not green.
- `caller-closure.log`:those three repaired consumers3passed1.28s. Tests keep old-owner/foreign-message exclusion assertions on actual rendered output.
- `native-source.log`:actual pinned d396 Pi, real loopback HTTP provider, real wire/SQLite, current source:1passed3.59s. Candidate index materialized; provider request asserts source-cited non-authoritative awareness, then actual read/edit/write/bash and one published reply, proof binding, cursor and exact owner lease release. No paid provider.
- `measures.json`:all declaration-owned measures for both touched production files, no increase including per-file chain terms, foreign absence, codec subclasses and independent class lines above500. Scoped measurement; no resource-heavy whole-project NRA scan or completeness claim.

Pending at draft: noneditable-wheel affected native case and final parent integration/live acceptance. Existing schema/history untouched; this is an in-memory ownership change, no cutover/migration needed.

## Installed checkpoint

Noneditable wheel actual native case PASS1test3.19s, source/history prompts sent only to the loopback provider. `installed-imports.json` verifies changed owner bytes and all loaded core modules come from the candidate; inherited PYTHONPATH was explicitly cleared. The first installed setup refused inherited PYTHONPATH before running any test (`installed-setup-first.log`), preserved as failure.

Measured touched-source debt: god-class excess315→305, exact-type checks13→9, long chains4→3, chain terms18→12, foreign absence11→9, string subscripts6→3, codec subclasses0. Per-file increases absent. These are scoped numbers, not a claim the whole refactor is finished.

Boyle now owns goal scheduler/controls too. Preserved incomplete262546bd implementation/handoff sent directly; no parallel goal-control continuation. PR363 remains limited to awareness.
