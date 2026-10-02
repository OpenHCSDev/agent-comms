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

## Published source and original evidence

Final production source is `6369fe7a28eff15d0148eda71266b351dff9d2b8`,
normal-main integrated through #518. The existing `JournalTable` base now owns
`for_session`; all journal roles inherit the lookup. The former history-only
copy and the inspection reader's repeated SQL are deleted. Against main #518,
the complete production change is eight files, 216 added and 38 deleted lines.
No new nominal class or schema declaration was introduced.

The stored-cut control exercises goal/input/native artifact facts (including
multiplicity), original refusals and UNKNOWN outcomes, immutable intent bytes,
source diff, status and narrow atomic export together. Original source files,
registry, input ledger and journal are hash-identical afterward. Representative
artifact rows in that control exercise inspection, not native artifact production.
Two existing producer controls initially refused their stale fixture constructor:
they supplied encoded revision text to the current `FileRevision` field and
`None` to plural pending keys. The existing helper/callers now use their actual
typed declarations. Original failures are preserved; the final installed batch
passes 19 checks. Each check covers exported-source isolation, captured-source
fences or the shared read-only journal/inspection workflow; there is no broad
suite or provider exercise.

An actual configured `nra-architecture` SDK saved fork passed the installed
`retained-context`, absent-cut diff contract, `compaction-status` and authored
export journey. The captured launch configuration/model/worktree was inherited
through the existing receiving fixture. The original session/profile files and
private input/registry/native files remained unchanged. No native owner was
launched, no input was supplied, and there were no provider calls or replays.
That original saved-session path had zero compaction journal rows: this is not
an observed full historical-cut or new native producer qualification. Its source
proof describes the prejoin installation; it is not the joined S1 package proof.

General native tool failures remain `ToolResultMessage` original journal and
transcript evidence. `completed_artifacts` passes the recorded success bit to
`NativeTool.result_artifacts`; failed operations cannot become successful
`NativeArtifactTaskFact`s. Original stored input rows represent Core's logical
text/disposition/provenance; native message parts, including images or transformed
prompt content, remain native journal/context evidence rather than reconstructed
input facts. These boundaries are not claims of full-context retention.

Before mapping covers 913 tracked Python files; after #518 integration it covers
914. NRA parses all but the same PEP695 file; candidate Python3.14 stdlib AST
supplements that exact Git source. Annotation, field load/store and call output,
actual declaration search, parser boundaries and dynamic ambiguity are published
in `evidence/retained-context-inspection-closure-20261002`. JavaScript/SDK and
external declaration machinery were read semantically, not counted as Python
AST coverage.

## One joined receiving package

Schrodinger normally merged the frozen source into #522 `b736fb95` without
changing native `ea043`. Einstein #520 owns the combined Python caller closure,
one source freeze and one installed S1/inspection package. Singer grants that
integration and will inspect the same completed configured fork/package; no
second environment, provider journey, fork or uncertain-input replay is needed.
Frozen #316/#65a/#c601 are independent and unchanged. Joined installed inspection
is pending that candidate; #521 remains draft. Broader S2 repeated retention and
S3/S4 research scope remain open under the original goal ledger.
