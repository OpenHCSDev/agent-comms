# S4 frozen construction sequence and prospective arm order

Source base: merged629/622 main `a40560fa`.

The existing scorer can read matched records, and `PairedRecallDesign` owns
the pinned oracle, selected conditions, model and sample count. It currently
has no runnable export for prospective construction. `RecallScenario.public`
exports cumulative snapshots; a caller would have to re-decide which history
is new at each cut and separately assemble the held-out prompt.

Extend the existing owners. `RecallRound` derives the new history from an exact
cumulative prefix; `RecallScenario` assembles ordered history/probe operands
once. `PairedRecallDesign` reads its pinned oracle and assigns prospective arm
order using an explicit sampling seed, distinct from its bootstrap seed.
The existing CLI exports that plan without launching anything. Preserve
Unicode/source text and omit oracle answer/evidence metadata from probes.

A construction plan is not an original input, a completed cut, a condition
intervention, preregistration, independent sampling, provider-capacity result
or spend approval. Full-context eligibility remains with original native
ContextBudget/source observations; no source text is truncated to make it fit.
The plan cannot retrospectively reconstruct historical submissions.

No new type/store/scanner/launcher, no model run, native edit, package or loan.
W1's native assembly and source spans remain Einstein's claim. The existing
configured native fixture continues to own execution/admission/commit/recovery.

## Published implementation and final checks

Implementation freeze: `863241dc`. **7 deleted / 105 added** across the existing
fixture and controls. No `src/`, `tools/` or `stack/` change. No new class.

One source method per fact: `RecallRound.history_after(previous)` refuses
changed/truncated source; `RecallScenario.construction_rounds()` produces the
ordered new-history and held-out-probe operands; and
`PairedRecallDesign.construction_plan(sampling_seed)` acquires its pinned oracle
once and derives prospective order for its declared sample count. The sampling
seed is explicit and independent of the inference bootstrap seed. Original
design decoding is shared by comparison and construction CLI modes.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python -m pytest -o addopts='' -q tests/test_recorded_retention_measurements.py -k 'construction_rounds or construction_plan or supplied_design or paired_inference'
python tests/compaction_retention_fixture.py --construction-plan --comparison-design design.json --sampling-seed 20261004
```

Final focused batch: **4 passed / 22 deselected / 9 subtests, 0.28s**. Controls
detect duplicate history feeding, rewritten prefixes, answer metadata leaking
into probes, changed oracle bytes, ordering coupled to inference seed and
changes to existing comparison/model-selection semantics.

The actual CLI exported all three original authored source cases: coding new
history lengths **6/3/3**, research **7/3/3**, goal **6/3/4**. Each reconstructs
its exact cumulative source in order and keeps public probes unchanged. Ten
prospective paired samples per case yield 30 **unexecuted** samples. Explicit
seed/model/conditions remain proposed parameters, not observed native facts.
Missing seed, seed without plan and competing condition overrides refuse with
exit2 before producing a plan. All three positive exports exit0. See the
[CLI receipt](../../evidence/s4-construction-plan-20261004/cli-receipt.json);
raw parameters/plans/outputs remain in `.artifacts/s4-construction-plan630/`.
No original native records, provider calls, packages, environments or loans.

Existing AST tooling (`audit.findings.Package`) parsed src311/tests365/tools53
modules before/after, zero omissions. The final call path is CLI → existing
design → existing scenario → existing round. Existing frozen/scored records,
public snapshot exports, original-input scoring and configured functional
journeys continue to use those same source/probe owners. Lexical AST evidence
does not prove dynamic resolution or execution.

This closes construction **operands and prospective ordering**, not condition
installation/submission, complete-history capacity, original cut creation,
independence, preregistration or billed/workflow accounting. Do not send the
whole plan JSON to a model: design metadata includes the pinned private oracle
reference; only the explicit history/probe operands belong in the configured
runner. Existing native/session owners still own admission, source selection,
commit and recovery. Actual matched interventions and the separate unapproved
30-pair/USD75 study remain unfinished and have not been launched.
