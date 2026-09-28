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

## Current integration boundary

- Parent combined242 installed wheel has passed actual retained143MB ACP/manual commit and strict reopen; native optional customInstructions is paired in045bbd7. These are no longer pending source tasks.
- Python framing/state commit cb5eb59 is ready to cherry-pick. Parent owns native commit-child exact encoded-length read and remaining historical resource work; selected transport uses existing PiRpcChannel with full-record buffering, not a new capacity protocol.
- Parent must inspect exact live reserved op7b2e8... with the recorded correlated limit_exceeded before any explicit refusal transition. This branch does not mutate or replay it automatically. No live root/native install/restart/provider changes have occurred.

## 2026-09-28 framing and recovery ownership closure

Owner direction: behavior lives on public nominal abstractions/subclasses; delete replaced state flags and classifications. Sizing stays with native CompactionPolicy, framing with PiRpcChannel/authority child, manual and selected commits share the existing journal.

- Removed selected `_MAX_RESPONSE` derived from retired file-count/text ceilings. Existing PiRpcChannel assembles full JSONL records across buffer boundaries, strict decoding and correlated progress deadline remain. **It still buffers the complete record; this is not bounded-memory streaming.**
- Native generation/output remains CompactionPolicy-owned. No new token-byte heuristic, response-size registry or native selected protocol added.
- OwnerCompactionCommit serializes its existing admitted request once and supplies exact encoded byte length as the third helper argument; parent implements matching native exact-length read, removing postallocation512KiB ceiling. This length supplies framing, never authority.
- SummaryState owns commit authorization, refusal transitions and manual recovery; ReservedSummary and RefusedSummary override only eligible behavior. Removed reservable_commit flag and every consumer plus manual/journal RefusedSummary type dispatch. UNKNOWN/reserved manual recovery stays refused. Identical refusal may be recorded again; changed reason is refused.
- Restored strict UTF8 text validation lost when the old summary cap was deleted. No size limit restored.
- `framing-state.log`: initial38pass/2skip/1fail exposed surrogate regression; `framing-state-current.log`:39pass2skip after repair. Includes a valid15MB metadata frame beyond old cap, duplicate/foreign progress/stall/UNKNOWN/strict-boundary cases.
- Parent reports combined installed242 retained143MB actual ACP manual→native journal commit→strict reopen passed16.20s;601read/211modified,7loopback requests, original unchanged, owner idle, no new inputs. This supplements prior source proof; this worker has not repeated the expensive retained run or made live changes.

- `manual-state-native.log`:5passed15.59s after state polymorphism, using parent's canonical prepared package; real clean manual commit, refused-original recovery preserving UNKNOWN, actual ACP route, reserved/UNKNOWN refusal without repeat. Parent's JS currently accepts the extra argv but still has old512KiB postallocation guard; these five cases do not claim that native guard removed.
- `refusal-recovery-cas.log`:1passed; exact duplicate reason allowed, changed reason refused, refusal cannot authorize commit, stale retirement fails after exact durable transition.
- Duplication review: ManualSelectedSummary only overrides NativeSummary commit options and postcommit admission outcome. Both use compact_owner_once→OwnerCompactionCommit→CompactionJournal. Manual adds explicit refusal recovery and no-original completion; it does not copy the selected adaptive admission token machinery or native writer.
