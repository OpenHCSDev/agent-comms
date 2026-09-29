# Adaptive compaction native fixture cleanup

Owner: Mendel; transferred from Arendt by Tristan. Arendt owns automatic
compaction UI acceptance only. Parent owns production/input code; Schrodinger
owns explicit route startup; Heisenberg owns warm tabs.

Scope: `tests/test_owner_compaction_adaptive.py` and owned evidence. Reuse the
existing selected owner fixture, native backend and retained native host. Delete
the hand-authored malformed JS history and obsolete nullable/disposition status
assumptions. Preserve correction, settings/source-change and no-replay cases.
No new production mechanism, paid provider calls or live-root mutations.

Baseline: main `c13737d1` (PR412 integrated). The parent's
`comms-journaled-goal-input-20260929/native-shared-final.log` reports eight failures;
independent bounded reproduction is pending.

Persistent workspace: `/home/ts/wt/comms-adaptive-native-fixture-20260929`.
Mendel scratch: `/home/ts/.cache/agent-scratch/comms-adaptive-native-fixture-20260929`.
Run serial tests using the existing installed runtime and pinned native package.
Record exact baseline/candidate results and process cleanup before merge. CI deferred.
