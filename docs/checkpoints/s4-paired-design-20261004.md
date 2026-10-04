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

## Working source and final checks

Implementation `8f51717e` deletes 10 lines and adds 173 across the existing
fixture/scorer and its controls. Runtime `src/` and native `stack/` are unchanged.
The design owns inference parameters; it does not copy the oracle or native
model. `compare()` verifies the oracle through the existing original-file reader
and delegates the complete native pair read to `RecallScenario`. The scorer uses
`PiModel.require_selection` on already-aligned original admission evidence.

All frozen recall questions must be represented in each unassisted trajectory.
Missing alignment, assistance or missing questions makes the whole interval
unavailable. Pair count and a different actual model refuse. Existing duplicate
original-input checks still run before reading the pairs. Cuts are never counted
as independent samples. The percentile bootstrap resamples complete paired-rate
differences with the supplied seed and count; endpoints use outward empirical
order statistics. This is a conditional interval, not certified coverage or
independence. The method is stated explicitly; changing it for a paid study
requires a reviewed design. See the [SciPy bootstrap method description](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html).

The existing CLI adds `--comparison-design design.json` alongside
`--recorded-pairs pairs.json`. A design owns the oracle and condition labels:
competing `--scenario-file`, `--condition` and `--baseline-condition` operands
refuse. Calls without a design retain their bounded defaults and descriptive
output. Original typed comparison records are encoded by FieldCodec at the JSON
output boundary. `study_acceptance` remains unevaluated.

After implementation, the same AST owner/consumer scan parsed 729 modules with
zero omissions. The new design declaration exists only in the existing fixture;
its consumer is the fixture CLI. Native acquisition and alignment consumers are
unchanged. New inference is on the existing `ScoredScenario`, with controls in
the existing measurement module. No scanner, store or provider runner was added.

Final batch: five affected scorer checks and six subtests passed in 0.27 seconds.
They detect wrong model/count, changed oracle, invalid inference parameters,
missing/assisted denominator removal, recall mixed with other measurements and
loss of original request correlation. Actual source CLI count/duplicate-operand
refusals and the existing research public export passed. Raw CLI outputs and
`cli-receipt.json` are in `.artifacts/s4-paired-design626/`. No native source was
reread and no model/provider input was submitted. The first launcher lacked
pytest; the broader CLI test module required unavailable ACP imports. These
collection failures executed no controls. The final batch used the existing
system interpreter and the relevant offline measurement module, without installs.

## Remaining S4 and proposed study

No supplied file proves prior registration or grants spend. Complete submitted
condition construction, full-history eligibility, intervention scheduling,
randomized sampling, cost/end-to-end margins and S1 budgets remain unfinished.
Original missing HTTP, returned-model, effort and historical converter evidence
stays unavailable. No current optional-policy default changes.

The concrete proposal remains 30 matched trajectories: 10 each for the frozen
coding, research and goal cases, three sequential cuts per arm, configured
openai-codex/gpt-6.1-sol with HIGH, candidate task-memory versus bounded control.
Proposed inference is a 95% paired trajectory interval, margin -0.02, 10,000
bootstrap resamples, seed 20261004. The proposed USD75 ceiling is unapproved;
normalized journal cost is not billed spend and does not prove this design fits
that ceiling. Registration must freeze complete construction, sampling and
resource accounting before results. This checkpoint does not start that study.
