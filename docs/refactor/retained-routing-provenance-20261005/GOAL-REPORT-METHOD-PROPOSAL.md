# Original goal-report preservation through existing owners

This is a source successor to 689, with an explicitly amended Git720 producer P.
It is not the unchanged720 c3e wheel and does not alter frozen routing operands.

GoalHistoryStore.acquire_read_only(registry_path) will acquire the existing private
SQLite journal with mode=ro and query_only, authenticate its declared schema, and
return all original GoalHistoryEntry rows including incarnation and disposition.
It cannot initialize, reconcile, commit a baseline, filter by current goal, or
change journal bytes. Its caller holds the original registry custody.

GoalReportMemberRetirement will acquire the already source-decoded registry and
release receipts with that full journal. Each original carrier must have a
truthful same-incarnation committed report/goal endpoint. Registry state requires
the complete latest endpoint; release receipts may be earlier endpoints in the
same committed chain. Clear/replacement does not erase the accumulated report
fact. Pending/uncertain, observed gaps, conflicting endpoints, missing journal or
missing report evidence refuse the operation. Aborted rows remain preserved but
cannot supply committed evidence. No baseline or report entry is synthesized.

Only this acquired operation can produce the declaration-compatible registry and
release postimages. Target FieldCodec must decode the entire postimage and every
full journal row, and compare its read-only journal acquisition before any source
replace or owner launch. Journal storage is never rewritten. Existing original
preimage retention includes the journal bytes/disposition.

Migrate ThreadRetirementCutover, live original capture/projection, target validation,
and original fixture/control consumers. Delete public raw thread/threads/releases
retirement helpers. Fixture producers use original GoalAction + Registration
journal commits; manual report strings cannot serve as preservation proof. The
existing recorded provenance projection stays recorded and cannot admit live work.

Selection/admission/fencing/OFD custody remains the existing 689 owner family.
No original roots/history, prefix, build, native/provider or live run is authorized.
Authored source controls come last; future live cross-format acceptance remains
unqualified and requires explicitly bound producer/target artifacts and custody.

Parent source distinction: actual PUBLIC475 Core3931 and the reviewed current
integration already share the current Thread/goal/history registry format. This
explicit amended720 retirement operation is not a necessary registry conversion
for that same-format cutover. Existing CurrentThreadProjection remains direct;
689 source/target handoff declarations still need coherent actual qualification.
