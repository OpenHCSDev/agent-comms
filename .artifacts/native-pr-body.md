## Work in progress: P18, P40, P41

Continues the full published settings work from #984. The older #984 head did not contain the complete runtime work. This branch includes current main's shared JSON flag record, newer thread identities, and the explicit caller/project changes from #977.

- Full work, whether direct or after TRIAGE, now enters SelectedExecution.full; only that method selects its action/tools. SelectedExecution owns the attempt limit.
- Next in this same change: Comms-owned runtime/settings, target-runtime fork copying, and deletion of executable choices from NativeThreadDefaults and native launch callers.

Not ready to merge. No installed fork or TRIAGE→FULL result claimed. Previous failed runs and uncertain inputs have not been repeated.

Before source inventory: 1068 Python modules across Core, tests/tools, paired Toad and the metaclass dependency; no parse omissions. Dynamic callbacks and class loading still require the real runtime checks. Final owner/consumer searches, new-case edit counts, focused test results and installed checks will be recorded here before review.
