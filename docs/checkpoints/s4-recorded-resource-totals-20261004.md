# S4 recorded resource totals

Arendt continues unfinished S4 in the same checkout after merged612. Existing
RecordedNativeCheckpoint and RecordedNativeProbe acquire original summary and
assistant usage. ScoredScenario will derive resource totals from those acquired
PiUsage values; public encoding remains FieldCodec's job. Delete early encoding
that otherwise makes a consumer reconstruct the original usage type. Preserve
missing versus zero for each counter and normalized cost. These totals cover
recorded completions only, not billed spend, retries, HTTP bytes or end-to-end time.

Source/callers first, one coherent measurement batch, bounded original-record
checks last. No provider, package, native, environment, public input or old attempt
run. Full S4 still needs matched construction and registered margin results;
the separate 30-pair/$75 study is not approved or started.
