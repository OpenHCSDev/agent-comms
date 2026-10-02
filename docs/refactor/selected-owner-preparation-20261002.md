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
