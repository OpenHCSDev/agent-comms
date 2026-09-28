# Complete S7 Registration + TurnRunner integration

Includes Registration PR159, TurnRunner PR161 and the prior live export/native diagnostic changes. Resolves the ACP deletion conflict by retaining the new component architecture; the removed scalar fence alias becomes TurnLeaseFence in TurnRunner. No compatibility facade is restored.

Parent local acceptance:
- 62 Registration/identity/export/compaction-gate/revision checks passed.
- 65 combined component, queue, Registration, gate and summary-exchange checks passed; two opt-in native cases skipped there.
- Fresh installed configured-provider queue acceptance passed: summary committed, accepted queued followup remained unstarted until the original, each native input started once in order, original facts retained, no UNKNOWN/errors. See real-queue-result.json. No earlier input replay.
- Six current Toad pilots migrated to actual component owners. Additional stale fixture-only interfaces found by installed checks are migrated to current Agent RPC requests and typed AgentEvent values. All six passed after fixture migration: goal edit/retry/set, input delivery/failure and queue view. Final results recorded alongside earlier failed attempts.

Parent owns live activation and final pins. Worker source evidence is included. CI deferred. This integrates PR161 including its required typed turn fence; no old implementation is retained.
