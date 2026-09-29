# Adaptive compaction native fixture cleanup

Owner: Mendel; transferred from Arendt by Tristan. Parent owns production/input
code. Arendt owns Core416 large-context native accounting and usage acceptance;
this test cleanup does not establish production compaction readiness.

Deleted: the entire 464-line `tests/test_owner_compaction_adaptive.py` injected
facade, including malformed hand-authored JS history and obsolete nullable/status
assumptions. Its meaningful contracts now belong to the existing continuous
selected-owner native/ACP journey, not a second test facade. The shared
`owner_fixture` signature/body and retained host are unchanged.
No new production mechanism, paid provider calls or live-root mutations.

Baseline: main `c13737d1` (PR412 integrated). The parent's
`comms-journaled-goal-input-20260929/native-shared-final.log` reports eight failures.
Independent `baseline.log` reproduces eight failures and one pass in 14.80 seconds.
Integrated main: `2c1ca485` through a normal merge, without production edits.

Replacement behavior: original reservation survives until actual publication;
native summary, journaled commit, validated reopen and two distinct ACP inputs
each write once. Correction and queue revocation preserve history, goal/grant
and unbound input. Real native `set_auto_compaction` after a real summary rejects
the original; a saved-file revision change is rejected by native custody.
Disconnect after the summary actually reaches the local HTTP provider retains
UNKNOWN, unchanged history and an unsent original, with no publication/replay.
Actual selected policy tests own disabled/custom model/project-settings cases;
existing native commit tests own metadata preservation. Detached fake-model
and fake-provider tests are deleted rather than granted compatibility paths.

Public ACP errors are `RequestError` with the actual typed cause preserved;
direct backend callers retain their backend exception contract. Assertions
verify disposition and durable effects, not refusal sentences.

Acceptance on the integrated tree: `canonical-final.log` six passed in 45.66s;
`canonical-policy.log` four passed in 19.17s, serial, localhost only. Native pin:
`native-current-776dc36857e630da`. Python/dependencies reuse the parent's installed
runtime with candidate Core source on PYTHONPATH. This is actual native/ACP
behavior, not a newly installed Core wheel or global-default activation.
Four dispatch subject/arm measures remain zero before/after for both files.

Persistent workspace: `/home/ts/wt/comms-adaptive-native-fixture-20260929`.
Mendel scratch: `/home/ts/.cache/agent-scratch/comms-adaptive-native-fixture-20260929`.
Run serial tests using the existing installed runtime and pinned native package.
`checkpoint-receipt.json` records provenance and cleanup; fixture teardown awaits
native child retirement and verifies its identity dead. Logs and private UNKNOWN
receipts remain preserved. CI deferred. C2 and complete queued C3 scope are owned
in Core419 at `/home/ts/wt/comms-restart-queue-cleanup-20260929`.
