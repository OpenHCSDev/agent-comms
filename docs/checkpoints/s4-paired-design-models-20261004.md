# S4 supplied-model inference checkpoint

PR645 closes an actual source gap: both arms could share the same mixed set of journaled models while inference checked only the final admitted request against the supplied design. Earlier tool steps and admitted requests are now checked too.

`PairedRecallDesign.model_alignment` owns this requirement through existing `PiModel.require_selection`. `RecordedNativeProbes.alignment` lends its already acquired request groups and completion identities. `ScoredScenario.paired_inference` deletes the final-candidate-only decision and delegates to the design. Matching mixed-model sets remain valid descriptive observations; known conflicting models refuse inference, missing admitted model observations leave it unavailable. Absent earlier budget stages do not establish complete capture.

Source evidence: existing audit.Package parsed 734 Python modules across src/tests/tools, no omissions. One design method and its inference consumer; original journal/diagnostic producers and PiModel unchanged. Cross-arm matching and supplied-design selection are distinct questions. No dynamic-resolution claim.

Final sanity: 5 focused controls and 10 subtests pass in 0.27s. The existing comparison-design CLI exits0 with two missing trajectories and unavailable inference. Initial pytest configuration refusal (missing xdist) is retained; final single-process invocation overrides repository addopts, no environment installation.

[Original receipts](../../evidence/s4-paired-design-models-20261004/README.md) preserve exact source/CLI/control hashes. Runtime/native/tools delta0, originals/SDK/provider calls0. No configured multiple-request, complete capture, HTTP, returned-model, capacity, billed-spend, intervention or full-S4 claim. The separate 30-pair/USD75 study remains unapproved; this source checkpoint launches none.
