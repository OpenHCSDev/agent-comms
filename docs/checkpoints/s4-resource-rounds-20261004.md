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

## Remaining acceptance

Implement first, then one focused resource/paired-output sanity batch and the
actual existing scorer export path. Authored controls establish measurement
semantics, not model recall, billed cost or complete workflow timing.
The separate comparative study is unapproved and will not be launched.

W1 system-source assembly/native projection remains Einstein's claim. Runtime,
native artifacts, provider inputs and original retained records are untouched.
