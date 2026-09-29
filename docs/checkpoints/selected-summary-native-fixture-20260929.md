# Selected summary retained-native fixture cleanup

Owner: Mendel, concrete defect transferred by Einstein Core417. Baseline350b14a9.
The `test_selected_summary_exchange.py` child fixture imports retired
`compaction_journal.SelectedSummaryAttempt` and fails before meaningful assertions.
Do not add a compatibility export or another fake state/protocol facade.

Reuse the canonical actual retained SDK/native/ACP owner fixture from merged
Core414. Delete replaced fake-child tests where the higher continuous journey
owns their contract. Preserve negative framing/correlation/progress, real
provider-inflight UNKNOWN/cancellation and noReplay semantics through existing
RPC instrumentation and controlled local provider responses. Coordinate shared
test helper hooks with Einstein's typed Pi vocabulary and Arendt's Core416 budget
tests; do not edit their production scopes or shared owner_fixture body/signature.

Pending acceptance is tracked by this draft. It does not block merged Core419
or independent C3 Core421. Serial bounded fixtures only, no paid calls/live root.
Persistent WT `/home/ts/wt/comms-selected-summary-native-fixture-20260929`;
scratch owner Mendel `/home/ts/.cache/agent-scratch/comms-selected-summary-native-fixture-20260929`.
