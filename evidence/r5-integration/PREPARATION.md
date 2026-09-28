# R5 parent integration preparation

Worker source: ~/wt/comms-runtime-collaboration-documents-20260928, based on main193. Source not yet published/installed.

Parent real saved-data copy comparison passed: two current runtime-info records and two latest activity records identical old/new; current ledger empty. Historical source snapshots have none of these files. This does not claim nonempty production-ledger coverage; worker source tests exercise nested free-form values and exact identity rename/delete. All owned copies removed; compact counts/equality in saved-documents.json.

No Toad production direct use of removed RuntimeInfoStore/SharedLedger/Activity serialization APIs found. One Activity.to_wire fixture in profile_hot_paths_pilot migrated directly to FieldCodec.encode in parent integration/r5-documents-20260928.

Source candidate Toad profile_hot_paths_pilot passes with10000 activity records: append/read median0.499ms in this local run. Session_sort_pilot passes all criteria, stable selection and shared settings. Raw outputs in paired Toad evidence/r5-documents. These are source acceptance, not installed R5 evidence.

Next: review worker final handoff, merge, pin/build paired wheels, run installed affected activity/sorting route, activate two idle owners through normal restart, verify launch/configuration/checkpoint, update pins and checklist. Existing R2 runtime remains live during preparation; no paid provider calls or old input replay.
