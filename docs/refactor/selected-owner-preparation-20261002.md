# Selected owner preparation closure

Owner: Einstein. Runtime/cursor/custody: Arendt #489. Native preparation/result producer: merged #499; no competing producer.

## Source finding

Native SessionContext correctly refuses managed input when restored saved context needs journaled compaction. SelectedExecution → SelectedConsideration → SelectedRequest.reserve → PrivateSendAdmission.execute → TrackedTurnSession has no owner preparation between capture of the persistent recipient and the original prompt. Normal OwnedTurn instead prepares StateData and calls maybe_compact_owner_turn.

Tracked launch deliberately disables autonomous native compaction/retry. Native selected-settings RPC already observes storedContext.requiresCompaction, but the Python adaptive consumer separately skips settings.enabled=false. Mandatory readiness and elective automatic compaction are being decided at different consumers.

## Existing owners and intended closure

Reuse NativeSessionPreparation for actual selected launch/StateData; NativePreparationResult for saved cut; OwnerCompactionCommit/CompactionBoundary/RetainedTaskFacts for original capture and writer; SelectedSummarySlot for the actual configured selected provider; CompactionResult for refusal/commit. No synthetic InputDocument row or fabricated ingress key for a private delivery. Preparation must finish before SelectedRequest reserves its original input, then ordinary one-use admission consumes the resulting saved source.

All selected callers: ACP private drain, cohort foreground, N/K foreground. Shared automatic/manual callers: OwnedTurn adaptive and compact_manual_owner. Remove detached settings-preflight implementation once all callers use selected native evidence. Preserve the native hard admission check, original a489s01 UNKNOWN/diagnostics, public seq327, and active source/native pins.

## Validation order

Complete source ownership/caller implementation first. Then one affected sanity batch and one authorized actual configured saved-fork/native/ACP journey against an immutable installed pair. No public input, replay, default publication, threshold increase, or provider substitution. No readiness claim yet.

## Working source checkpoint

Normal integrated Arendt489 a87a7065: richer SelectedSession/SavedSelectedSession/FirstSelectedSession, original launch and ContextBudget source. This is an unready producer dependency, not installed evidence. Its native SessionContext/ContextBudget already owns compactionRequired; #506 changes no native policy or thresholds.

SelectedRequest.reserve is the one async factory that prepares BEFORE original private reservation. Both triage and FULL use it, including FULL after triage. SavedSelectedSession uses NativeSessionPreparation.open_launch on the exact tracked launch; pristine FirstSelectedSession retains its original first-start verification. NativePreparationResult → OwnerCompactionCommit.compact_selected → source-owned summary_outcome → existing journal/native writer is shared by private no-original preparation, manual compaction, and original-admission adaptive compaction. No new type/store. Prior idle child is discarded by existing writer path before source mutation. Temporary preparation child is closed before original tracked turn; no borrowed reader crosses native writer acquisition.

Deleted alternate adaptive_summary_strategy provider hook, adaptive_compaction_enabled admission bypass, Python settings.enabled discard, duplicate manual/adaptive provider/source orchestration, and detached compaction_settings helper. Native hard input guard remains.

### Concrete remaining direct batch relation

OriginalTurnInput.compaction_key currently returns a key only for len(keys)==1. A genuine ChannelInputBatch has multiple accepted original ingress rows, while existing SelectedAdmissionSource/InputReservationCheck/SelectedSummaryAdmission currently bind one ingress key. A fabricated master row, copied queue, or no-original source would erase that authority. This remaining original-family closure is explicit; current checkpoint does not claim every direct batch or live readiness. Existing InputBatch/OriginalTurnInput and input admission owners are being coordinated.
