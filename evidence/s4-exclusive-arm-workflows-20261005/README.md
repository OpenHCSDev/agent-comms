# Original arm workflow accounting

Source work in progress; no runtime, SDK, holder, provider, input or original-record operation.

The existing configured runner deliberately shares source inputs and committed cuts.
SessionLifecycle.selected_native_fork restores the exact original owner/source after
each arm. RecordedNativeProbes.alignment requires the same immediate SDK parent
cut and revision. These are useful functional controls, not independent entire
workflows. Their shared monotonic clock cannot be split into per-arm p95.

RecordedNativeCheckpoint and RecordedNativeProbe will expose original completion
coordinates with the usage they already acquire. ScoredScenario will derive shared
and exclusive accounting from those coordinates for every scope and trajectory.
Descriptive totals remain visible; cost-margin decisions cannot use shared or
missing completion ownership. No copied source collector or receipt store.

Separate end-to-end arms need genuinely separate source/summary executions and
an explicit matched-source design; current same-cut alignment cannot certify that
experiment. No new configured execution or 30-pair/USD75 study is authorized here.
All frozen reports, old clocks, raw refusals and uncertain inputs remain untouched.
