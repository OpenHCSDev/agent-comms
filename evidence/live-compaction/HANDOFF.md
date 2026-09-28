# PR240 installed-base compaction correction

Base15a4d00 (installedPR225); branch fix/live-compaction-recovery-20260928.
PR236 refactor remains separately preserved at73c9dbc; no D22 journal reset is required here.

## Published boundaries

- b310d63: correlated native refusal persists RefusedSummary(reason); it cannot admit an original input. CLI `agent-comms --root ROOT compaction-status --thread NAME` reads operation/state/reason without printing source text.
-93ea52f: one typed progress event, correlated monotonic activity renews existing model inactivity grace; no90s total budget. Dead detached source snapshot and its tests deleted, zero production consumers.
- f6bff12: canonical explicit manual command uses selected live Pi summary + existing OwnerCompactionCommit/nativeCAS. ManualCommittedSummary has no original input capability. Stock manual operation remains separate outside canonical roots.
- Explicit manual recovery retires only an exact RefusedSummary (known prestart result); original UNKNOWN input remains byte-for-byte unchanged and is never replayed. Reserved/UNKNOWN provider/native outcomes stay blocking.
- PR242 initial75c8364 source/file metadata closure is merged here. Parent owns its JS/progress/output implementation and prepared package.

## Evidence (overlapping batches)

- decline-recording.log:19passed2opt-in skipped; actual local pipes/journal durable refusal and CLI inspection.
- progress-bridge-current.log:27passed2opt-in skipped; multi-progress exceeds old total duration, repeated sequence cannot hide stalled provider, timeout/cancel retainUNKNOWN; stock manualbridge behavior preserved.
- manual-recovery-native.log:2passed; real SDK selected native summary, authority child, journaled native commit. Clean manual has no original input; failed adaptive original remains UNKNOWN byte-identical through explicit manual recovery.
- manual-acp-first.log:1passed; actual CommsAgent+ACProuter /compact, retained native child, real summary+commit, transcriptChanged and owner turn cleanup. No fake bridge/prepared input.

## Remaining before activation

- Full actual retained143MB copied-session commit + strict reopen on parent combined package; source/manual fixtures are not that acceptance.
- Paired optional customInstructions on selected native request: Python/native contract requested from parent.
- Terminal framing/resource ownership: eliminate file-count-derived _MAX_RESPONSE with an exact native serialization capacity declaration (proposed), not a guessed token-to-byte constant. Native owns per-call policy; Python owns transport/inactivity.
- Parent must inspect exact live reserved op7b2e8... with the recorded correlated limit_exceeded before any explicit refusal transition. This branch does not mutate or replay it automatically. No live root/native install/restart/provider changes have occurred.
