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
