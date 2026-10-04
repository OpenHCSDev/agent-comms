# S4 paired design and recorded inference

Continue the existing recorded-data runner after merged624. No model calls,
study launch, native/package changes or new environment belong to this change.

`RecallScenario` owns the frozen questions, `RecordedNativeProbes` owns original
cuts and request alignment, and `ScoredScenario` owns trajectory denominators
and aggregates. None currently owns supplied inference parameters. A small
immutable design will bind the original oracle file, conditions, actual admitted
model, sample count and paired bootstrap parameters. The existing scorer will
calculate intervals from complete unassisted trajectory rates, never individual
cuts or a selected subset of successful samples.

A supplied design is not evidence that it was registered before results, that
samples are independent, or that an experiment was authorized. Construction,
cost, end-to-end timing and full S4 acceptance remain separate requirements.
The proposed 30-pair/USD75 study remains unapproved.

Before editing, existing refactor-audit AST parsed src311/tests365/tools53 files
with no omissions. Relevant calls are in the existing fixture CLI and recorded
measurement controls. `request_alignment` already uses `PiModel.require_selection`;
the new binding must use that owner, not infer models from registry settings.
Lexical AST does not prove dynamic resolution. No other comparison-design or
confidence-interval owner was found in these roots.

Implementation and final validation are pending. No repeated624 original read,
SDK/native run or provider experiment is needed for unchanged reader behavior.
