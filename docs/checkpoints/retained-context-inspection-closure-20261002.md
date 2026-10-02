# Full retained-source inspection

Singer owns the S2/S5 inspection consumer continuation from merged main
`a0ad64e2`. This reuses the finished #515 worktree on a new branch; #515's
production and evidence remain closed.

The current authored reader correctly resolves original wire declarations and
pinned original inputs through `TaskAttachment`, `RetainedTaskFacts`,
`RetainedSegment`, both CLI consumers and the original atomic exporter. It is
not the complete source used by native compaction. `HeldCompaction.capture`
also obtains the owner's goal facts, input material selected by existing queue
custody, and native journal facts from its witnessed branch.

Trace those determining owners and every producer/inspection/diff/export caller
before implementation. Extend the existing `ExactTaskFact`, `RetainedTaskFacts`
and `CompactionBoundary` machinery; delete replaced selection and capture
decisions with their caller migration. Preserve original identities, wording,
source revisions, queue distinctions, UNKNOWN dispositions, journal/tool-pair
coordinates and the compaction fences. Inspection grants no execution, replay,
resolution or compaction authority. Authored export retains its narrower scope.

Einstein owns native compaction and S1 timing. He grants the inspection
continuation without changing his source captures/fences; `TaskAttachment`
subtask hooks remain his coordinated methods. Arendt owns lifecycle and wire
resource changes; shared reader/capture methods require direct coordination.
CLI, retained inspection/diff/export and the complete receiving integration
belong to Singer. No parallel store, capture family or compatibility reader.

Source reasoning and AST declaration/consumer mapping precede the coherent
implementation. Final sanity and the actual configured saved-fork/inspection
journey follow that batch through existing infrastructure. No new full
environment is needed until the source checkpoint requires it. This early draft
records the implementation boundary, not a completion or live claim.

## Implemented source checkpoint

The existing `WireLog` now supplies one certified `retained_sources` lifetime.
Its original `retained_context` remains authored-only for export and Einstein's
S1 observation. Original pinned-input lookup moved into `RetainedTaskFacts`;
`InputDocument` supplies original fact membership to both compaction selection
and inspection. Existing `HeldCompaction.capture`, live queue selection, native
writer fences and source equality checks are unchanged.

`CompactionBoundary.inspect` adds the current goal and durable input observations,
including queued/UNKNOWN rows with their original disposition. These are not
compaction admissions. `retained-context` also exposes all declared journal roles
for the saved-session path: typed selected requests, unchanged source proof
bytes, native intents with their original one-way source views, operation and
summary outcomes, publication obligations and private-input/enrollment records.
Historical owners remain recorded rather than being relabeled as current.

`CompactionJournal.observe_readonly` owns the original read-only custody and
SQLite lifetime for both transcript snapshots and inspection. `compaction-status`
now uses that owner instead of constructing a writer. No absent database is
created. Journal projections are displayed without becoming a compatibility
reader or a new native source. `retained-context --diff` compares the last two
original typed selected source cuts, including exact goal/input/native artifact
facts and multiplicity. It does not compare live leaves or infer summary quality.

Authored atomic export still includes only current original declarations; goals,
unpinned inputs, artifacts and failure dispositions cannot become instruction
exports. `context --turn/--diff` continues to inspect original no-text manifests;
next-context inspection retains its existing before-input/provider-hook scope.

Native artifact provenance is the original witnessed session/leaf/revision and
SDK request/result pair. This reader does not infer a current native branch,
uncaptured results or current filesystem contents. Successful original SDK UTF-8
writes are the currently declared artifact family; arbitrary file operations and
free-form assistant prose are not additional artifact facts. Failures are the
original journal state/outcome, not a newly authored task constraint.

Before/after AST mapping covers all 913 tracked Core Python files: NRA parses
912; the existing PEP695 `tracked_turn.py` requires candidate Python3.14 stdlib
AST. No file is omitted from source mapping. Dotted/bound references remain
candidates, with semantics and actual MRO read separately. No new type, store,
queue, schema, native helper or environment was introduced. Final workflow
sanity and installed inspection remain pending at this source checkpoint.
