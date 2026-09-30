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

Additional transferred fixture gap from parent C4: at baseline main7995bc5a,
`test_acp_private_nk_delivery.py::test_acp_session_selected_native_pipeline_never_uses_legacy_ack`
expects covered_seq equal original injected sequence 1, but source coverage is 2
after the sender response. Parent reproduced this on baseline in 1.75 seconds.
Injection identity and source coverage answer different questions. Migrate its
fake pipeline to the same actual retained/native journey; preserve original
input identity, coverage proof and no replay, not a compatibility coverage value.

Current main72062939 normally integrated for the shared actual native setup and
turn contract. Additional baseline fixture debt from C3 acceptance: four
test_goal_standby_liveness fake-stream cases omit actual InputStarted and now
correctly produce public RequestError for the unstarted original; one obsolete
_emit_event callback also rejects its client argument. Reproduced unchanged
main4FAIL2.64s with the approved native package leaf configured. Log:
main720-standby-baseline.log in this draft's named scratch. Do not synthesize
started/covered values or adjust exception expectations until the existing
actual retained/native fixture owns terminal-vs-settled semantics and delayed
stale callbacks. Full selected-summary correction/settings/source/UNKNOWN
negative cases and the injected-vs-covered original gap remain assigned here.
