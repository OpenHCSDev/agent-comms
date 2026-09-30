# comms428 original native wait investigation

Owner: Mendel. Base d16238a5 (merged #449); Arendt owns configuration/startup
#448, parent owns public activation, Sch owns source cursor/outcome/UI.

Original public worker 3196900/birth 24802922, turn
`a4289a93bc4547d68593af946e736c29`, admission 1004, generation 13,
started 1790774930.957728, phase ModelWait. Original native process 3262993.
Root `/var/tmp/agent-comms-live-20260927-wzjtqhza`; retained native journal
`/home/ts/.pi/agent/sessions/--home-ts-.agent-comms--/2026-09-29T23-39-56-904Z_01a0ef8a-0c69-75a1-86fe-53cd38ab0d18.jsonl`.

Original toolResult `83b1f787` at 13:35:56.167Z → assistant `b8490b7e`
at 13:38:06.065Z → edit result `41c19d41` at 13:38:13.364Z. The native
gap is 129.898 seconds, so this cannot be attributed solely to delayed UI
publication. Completed assistant usage records 1034 reasoning tokens and
2060 output tokens; its message timestamp is 1790775356207. Those counts do
not establish continuous streaming or identify provider/network/CPU delay.

The owner's eventual response does not close the latency defect. Trace the
existing watchdog/event, original prompt and child/request custody and
publication timestamps read-only. No native/provider calls, replay, cancel,
owner restart, lease clear, direct pipe consumption or semantic state mirror.
Preserve history, drafts, UNKNOWN and original attempts. Identify uncertainty
when historical stream timing was not retained. If a concrete correction is
needed, use the original event/lifecycle owners and affected installed journey.

The independent R1 original sequence-161 attempt completed normally, its
original lease retired, and later public registry reading finds R1 idle.
Exact #449 recovery receipts remain in its original persistent worktree and
scratch directory; do not rerun its completed gates.

Scratch owner Mendel:
`/home/ts/.cache/agent-scratch/comms428-native-wait-custody-20260930`.
This draft is investigation ownership, not a latency fix or readiness claim.
