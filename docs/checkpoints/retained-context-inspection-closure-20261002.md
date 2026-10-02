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
