## Complete original R7 / S5 caller and deletion closure

- Replace run_one_sealed_claim and repeated parameter bundles with single-use SelectedExecution owning source/lease/session/triage/full/publication/cleanup. DurableTurn is the only evolving attempt fence.
- Delete old attention claim declarations/APIs, turn-claim APIs, internal epoch vocabulary, ParticipantSnapshot generation alias and ProjectionRecord/RecoverySnapshot forwarding. Migrate current core/test consumers; preserve SQL/native/saved spellings at their boundaries.
- Use existing MutationStore, lease/native-tool policy and FieldCodec. No new executor, parallel registry, replay policy or live migration.

## Local evidence

95 selected/projection cases;158 declaration/store/tool cases;102 generation cases (21 opt-in native skips);49 source/foreground cases.156 caller cases plus separately repaired maintenance case. Integrated actual pinned Pi + localhost fake provider executed all four normal tools, published once and released claims/lease. Three native queue/compaction cases passed. NRA79 detectors, no omissions/findings. Counts describe overlapping batches; earlier failures/timeouts are retained honestly in HANDOFF.

Parent owns installed/Toad rollout; CI deferred. Only observed R6 production seam is3 ThreadManagement participant accesses. Paired Toad fixture: channel_history_reader_pilot claim_local_turn -> lease_local_turn. Details: evidence/r7-selected-execution/HANDOFF.md and CALLER-CONTRACT.md.
