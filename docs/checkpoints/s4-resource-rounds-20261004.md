# S4 resource totals keep the frozen round denominator

Source base: `486b8366`, including merged628/626/624 and621.

## Source decision

`ScoredScenario.source.rounds` owns the frozen measurement membership.
`recorded_resources` currently totals only `checkpoints.values()` and
`evidence.values()`. A missing round therefore disappears before each usage
metric decides completeness. Known zero counters remain valid, but a total
over an incomplete trajectory must not be presented as its complete total.

Make this existing scorer method consume its scenario's round membership.
Summary, assistant and combined totals keep the original observed usage and
record counts while requiring their applicable original round observations.
Do not invent a summary for an unobserved control or count absent usage as zero.
No condition label can prove that summary work did not happen.

All public native and paired outputs already pass through this method. Migrate
its existing direct controls to an actual scored scenario and close all metrics
in the same change. No new resource, sampling or lifecycle class is needed.

Before implementation, source search found one production-of-measurements
caller, `ScoredScenario.public_native`, and two existing resource controls.
Original `RecordedNativeCheckpoint.summary_usage` and
`RecordedNativeProbe.model_steps` remain the counter owners. Lexical AST source
mapping uses the existing refactor-audit package; it does not prove dynamic
resolution. This is a measurement membership correction (BOUND-2/MEMB-1).

## Implemented and checked

Implementation freeze: `49f140fd`. **25 lines deleted / 78 added** across the
existing scorer and controls. `src/`, `tools/` and `stack/` are unchanged from
the source base. No new type, store, native artifact or measurement clock.

`ScoredScenario.recorded_resources` is now an instance method. All seven usage
metrics in summary, assistant and combined projections use the same frozen
round membership. The combined projection requires both original groups for
each round. Missing rounds and empty assistant-step collections are explicit;
the number of complete expected records is unavailable rather than guessed.
Known record counts and reported `observed_value` subtotals remain visible.
This does not prove all intervening work/retries were captured, nor that an
unobserved control performed zero summary work.

One batched final sanity command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python -m pytest -o addopts='' -q tests/test_recorded_retention_measurements.py -k 'resource_totals or paired_inference or paired_recall'
```

**5 passed / 19 deselected, 0.22s.** These detect missing frozen rounds presented
as complete cheap totals, missing usage/fields turned into zero, lost tool-step
usage or double-added reasoning, and changes to existing paired denominators.
The new control exercises independent summary/assistant coverage, actual zero
subtotals, empty model steps and all complete metrics in one family.

The actual existing scorer CLI exported a paired authored absent-record control:
exit0; both arms retain all three expected/missing rounds and all seven metrics
remain unavailable, not zero. Construction and study acceptance remain false.
Raw outputs are in `.artifacts/s4-resource-rounds629/`; the small public
[receipt](../../evidence/s4-resource-rounds-20261004/cli-receipt.json) contains
the command, result and raw hashes. This is scorer/export acceptance, not an
installed native or model workflow claim. No original source reads, provider
calls, processes, packages, environments or loans were needed.

Before/after AST mapping used existing `audit.findings.Package`: src311,
tests365, tools53 modules; **zero parse omissions**. One owning declaration,
one `public_native` caller, and existing direct controls (now instance calls).
`score_native`, `compare_native`, `compare_native_pairs` and CLI consume the
same public projection. Two initial inspection commands used a wrong tooling
attribute and stopped early; the completed mapping above includes all roots.
Lexical references are source evidence, not dynamic resolution proof.

Full S4 condition construction, sampling, complete-workflow resource/timing
capture and approved comparative results remain unfinished. Authored controls
establish no recall, billing, HTTP or performance result. The separate
30-pair/USD75 study remains unapproved and has not been launched.

W1 system-source assembly/native projection remains Einstein's claim. Runtime,
native artifacts, provider inputs and original retained records are untouched.
