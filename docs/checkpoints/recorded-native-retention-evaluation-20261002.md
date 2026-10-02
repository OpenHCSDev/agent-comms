# S4 recorded native evaluation consumer

Base: merged main533 `c4f22dcd`. Owner: Arendt. Existing scorer and fixture files:
`tests/compaction_retention_fixture.py`, `tests/retained_native_fixture.py`.
Mendel and Singer confirmed no competing evaluation/scorer claim. Einstein owns
the original201 preparation failure and S1/native policy; that source fix takes
priority and is not part of this evaluation implementation.

## Missing consumer

`RecallScenario`/`RecallRound` own frozen questions and exact-answer scoring.
`ScoreView` derives totals; `RecordedAnswers` decodes external answer JSON once.
The existing entrypoint only exports questions or scores a supplied answer file.
No consumer joins answers to their original native probe/context, a selected
compaction checkpoint, source/model/settings records, or the original raw answer
artifact. Naming a condition does not construct it or prove retention.

Build that receiving consumer using the existing retained native fixture and
scorer. Native messages/proofs, `CompactionJournal.observe_readonly`, original
selected request/commit records and native returned results remain the sources.
Evaluation is a read-only measurement: it cannot grant admission, select a
production source, repair a summary, rewrite UNKNOWN, or change runtime policy.
Do not build another launcher, provider client, oracle, retained store, or native
recovery algorithm. Unsupported original linkage is unevaluable, not a guessed
successful result. Original typed record APIs must carry the relation.

## Delivery and experiment boundary

Source ownership and caller closure come first; final focused sanity checks and
one controlled native fixture exercise follow implementation. No matched-provider
experiment is authorized by this scope. Model, sample size, margins and spend
must be agreed before collecting model outcomes. Existing scorer fixtures and
controlled responses establish plumbing only, not research acceptance.

Full S4 remains broader: coding/research/goal traces; three genuine checkpoints;
canonical availability, prompt presence and model recall reported separately;
revision mass and held-out distinctions; eligible full-context controls; paired
recall, cost and latency margins. No quality/default flip follows from this
consumer checkpoint or an existing lifecycle pass.

## Implemented receiving path

`RecallRound.probe_text` exports only its frozen public questions. The native
reference boundary uses `FieldCodec`: session identity, original input ID,
answer entry ID, and, when applicable, original journal/selected operation/commit
references. These references do not replace native or journal ownership.
`RecordedNativeProbe.observe` borrows `NativeEntry.open_evidence` and
`NativeContextProof.read_evidence`; `RecordedNativeCheckpoint.observe` uses
`CompactionJournal.observe_readonly` and `CompactionOperation.require_summary_link`.
Missing journals, mismatched identities/source digests and unsuccessful terminal
answers refuse measurement. No original ACK or admission capability is consumed.

The protocol is deliberately a direct tool-free probe immediately after its
declared compaction entry. That parent relation is read from the original native
entries, not reconstructed from timestamps. The round requires the exact frozen
native user prompt and decodes the original terminal answer into `RecordedAnswers`.
Existing `Question.score` and `ScoreView` compute all results. Missing rounds
remain in the denominator. The scorer deletes its private duplicate-key parser
and uses the existing strict JSON hook.

No production source changed. Original coverage/cursors, floors, input receipts,
session selection and recovery remain with their current owners. The new
reference records own measurement inputs only. They neither establish experimental
condition construction nor prove provider prompt presence. Actual model/usage
come from the original assistant entry; checkpoint settings/retained facts come
from the original selected request. Canonical availability is reported, not yet
independently scored. Full S4 quality/latency acceptance remains unimplemented.

## Use and final validation

Export frozen probe text with `python tests/compaction_retention_fixture.py
--probe-prompts`. A native references JSON file maps each round ID to
`RecordedNativeProbe` fields: `session` (original `sessionId`/`sessionFile`),
`input_id`, `answer_entry_id`, and an optional `checkpoint`. The checkpoint has
`journal`, `reference` (existing `session_file`/`operation_id`) and `commit_id`.
Run `--native-probes FILE --condition full-context` to consume it. The label is
still a label; this command does not construct the experimental condition.

Final validation used installed Core457b104 and native00c2 through the existing
`native_backend` fixture, localhost transport only. Two original calls seeded
the first authored history and answered its frozen probe. They completed before
the initial export failed: the located context proof contains a filesystem Path.
The corrected reader derives wire fields from `NativeContextRecord`; the
external journal reference declares the existing `PathText` representation.
The corrected CLI then consumed those **same completed originals**, with no
additional native/provider call. Eleven scorer controls passed; the original
failed pytest log is retained, not relabeled as a full passing pytest run.

The receiver scored seven controlled answers and kept fourteen missing answers
in the denominator. Wrong-round and nonterminal references refused. A read-only
observation of the already qualified529 original commit verified exact selected
request/native linkage and retained-summary content. Missing journal storage
refused without creating it. Native files, input proofs and journal hashes stayed
identical, and no fixture native child remained. This qualifies reader/scorer
plumbing only, not model retention, cost, provider prompt presence or S4 quality.

Evidence: `evidence/recorded-native-retention-evaluation-20261002/` contains the
installed receipt and the before/after AST declarations/references for nine
scorer/reader/dependency files, with zero parse omissions. Dynamic resolution is
not inferred. Twelve scorer/reader lines were deleted, including the displaced
duplicate JSON parser; all production source and runtime formats are unchanged.
