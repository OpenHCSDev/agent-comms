# S5 phase 2: authored retained context operations

Owner: Einstein, the same S5 integration owner. Dependency: accepted S2 retained
classes and S5 phase 1, tracked by Core PR473. This PR reserves the dependent
scope now; it does not claim implementation or live readiness.

Extend the existing ContextSegment family with RetainedSegment from S2's original
source-owned retained classes. Pin, supersede, drop and export are authored
commands on original source records, not mutations of an independent context copy.
The parent user instruction and S5-TURN-CONTEXT.md remain authoritative.

Acceptance journeys:

1. Pin an original applicable constraint; compact via the existing native
   operation; observe its exact text and original author/provenance intact.
2. Export an original constraint into a fresh thread; inspect the same source
   identity and exact text, then verify actual model input contains it intact.

Preserve native sessions, source/proof and UNKNOWN. No new memory database,
second commit path, alternate codec or input replay. Coordinate with S2's
implementation owner before shared packing/preparation edits. S2 not landed
is a named dependency for this phase, not a hold on phase 1 or other delivery.

## Published source integration, 2026-10-01

The dependent branch normally includes S5 receiving source and S2's published
TaskAttachment API. The expanded original journey keeps its independent receiver,
private/public correction checks and receiving-only queue handoff, while consuming
`task_sources`, `Message.task` and `current_authored_sources` directly. The
accepted receiving61 package stays on Core4345/Toad25d/native0064. This branch
changes neither that immutable package nor public defaults.

Sch481 owns the generic declaration, source packing and human pin admission.
Einstein474 owns CLI, RetainedSegment and export consumers. They use the original
certified wire source cut and member applicability; atomic file publication stays
with WireTranscriptExporter. No new retained ledger, semantic text copy, reader,
codec or human claim inferred from an agent turn is authorized. The commands and
export/model journeys remain unfinished; source integration is not readiness.
