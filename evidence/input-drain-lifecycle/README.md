# InputDrain admission and live handoff ownership

Owner Wegener. Base7070ad1f includes merged378; parent deployment/native package unchanged.

Deletion first: removed separate steering key/goal maps, forwarded-ID bookkeeping and original input key/text mirrors. Existing OriginalTurnInput now supplies notice identity/text; AcceptedFollowingInput supplies the captured followup source. Durable dispositions determine pending status. QueuedInput/DeferredQueuedInput own live future-receipt capability and promotion; InitialInput owns original dispatch. Existing PromptRequest subclasses now own queue/steer behavior, replacing their string-label return and consumer switches. Original and selected-late dispatch share exact captured acceptance and current handoff checks; TurnRunner's duplicate handoff procedure is deleted.

Patterns: IMPL-1/IMPL-4 existing request cases carry behavior; IDEN-1/IDEN-3 captured source and future capability replace scattered mirrored facts; BOUND-1 typed request enters InputDrain unchanged; TIME-1/TIME-3 all deleted attribute/API callers migrate, no aliases. Declaration-derived dataclass transition discards deferred authority on promotion without a second field roster.

SelectedExecution/coordinated_runtime are Boyle-owned and untouched. OwnerCompactionCommit/381 unchanged; only consumers of FutureInputQueue migrate. MessageBus and HistoryViews remain Dalton-owned. Native fcadec/native proof and SessionContext interfaces unchanged, no live conversion/replay/provider use.

Draft checkpoint: core imports work. Queue/selected-lifetime tests initially29PASS/1FAIL/1skip13.85s; failure exposed a removed captured-source check at final native admission. Corrected via existing typed admission rule and both missing-source cases PASS2.32s. First nonexistent test filename produced no tests and is retained honestly. Remaining focused authority/feedback, current bridge fixture closure, actual installed saved-native/ACP/queued acceptance and ratchet still in progress; not ready/live.

Private native-evidence experiment preserved only on local47518041, excluded from this branch. Older completed own derivatives (~15MB) removed after process-reference checks;378canonical/candidate/wheel/receipts and source retained. Resource warning means serial bounded checks, no additional agents or large matrix.
