# R2 catalog migration — merged and live

Core192 (054028b5c7cc3a36c964d1abbfd78af60c13545e) and paired Toad94 (0f647c43315bdd41fe30a90b33a0bbc684d22e62) are merged and installed in runtime-r2-catalog-20260928.

## Actual acceptance

- Source135 boundary and66 combined consumer cases passed (overlapping counts). Core evidence/catalog-document/HANDOFF.md contains complete scopes, failed/corrected receipts and source migration/deletion map.
- Paired Toad migrated both production catalog consumers directly. Mounted any-mode and sort pilots passed; complete channel view/navigation and cross-view channel/member pin pilot passed. Its earlier obsolete-API failures and one pin observation/teardown timeout are preserved, not reported green. The final full pilot used proper test-owner teardown and diagnostic assertions.
- check_saved_data.py compared old/new canonical message projections for8478 rows (8400+20 original,58 live) across copies of all3 roots. Zero decode failures; all catalog projections equal; no-op canonical migration and reopen preserved values. Fixtures/copies were removed after saved-data.json was retained. No original data rewritten/replayed.
- Installed candidate mounted original #comms20/#nra8 rows,111 saved-session choices and original UX transcript. Bus sequence unchanged. Installed mounted any-mode UI also passed. Paired Toad94 retains its local receipt files.
- Normal activation replaced exactly two idle owners. Both ready/alive/local,103 identities and58 bus rows unchanged. The actual catalog migration retained all27 preference records,4 explicit tag declarations and0 saved-view declarations, equal to its before-state. Four old source files unchanged; backup and projection are private local activation receipts.
- Read-only installed observation confirms five launcher paths, original configured models, valid checkpoint58, and no disabled automatic compaction overrides.
- Normal toad-comms PTY opened history label without import/traceback errors; inner exit0 after bounded interrupt, outer124 intentional.

## Operational boundary

Current code exclusively uses catalog.json after migration. Original catalog files are recovery input, not a concurrent older-writer channel. Older software cannot consume subsequent canonical edits; do not downgrade by deleting catalog.json. No old Toad UI process was observed before activation. Shared stack pins were copied only after unrelated configuration comparison; earlier shared files backed up in before-r2-stack-pins-20260928.

Earlier real-provider coding and adaptive queue receipts remain retained; this catalog change used local affected paths and saved-data migration evidence, without paid provider repetition or CI waiting.
R1 Pi boundary remains active with Pascal. R5 runtime/collaboration documents is assigned to Darwin. Remaining original R3/R6/R7 scopes are tracked, not closed by R2.
