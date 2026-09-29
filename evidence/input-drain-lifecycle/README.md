# InputDrain admission and live handoff ownership

Owner Wegener. Base7070ad1f includes merged378; parent deployment/native package unchanged.

Deletion first: removed separate steering key/goal maps, forwarded-ID bookkeeping and original input key/text mirrors. Existing OriginalTurnInput now supplies notice identity/text; AcceptedFollowingInput supplies the captured followup source. Durable dispositions determine pending status. QueuedInput/DeferredQueuedInput own live future-receipt capability and promotion; InitialInput owns original dispatch. Existing PromptRequest subclasses now own queue/steer behavior, replacing their string-label return and consumer switches. Original and selected-late dispatch share exact captured acceptance and current handoff checks; TurnRunner's duplicate handoff procedure is deleted.

Patterns: IMPL-1/IMPL-4 existing request cases carry behavior; IDEN-1/IDEN-3 captured source and future capability replace scattered mirrored facts; BOUND-1 typed request enters InputDrain unchanged; TIME-1/TIME-3 all deleted attribute/API callers migrate, no aliases. Declaration-derived dataclass transition discards deferred authority on promotion without a second field roster.

SelectedExecution/coordinated_runtime are Boyle-owned and untouched. OwnerCompactionCommit/381 unchanged; only consumers of FutureInputQueue migrate. MessageBus and HistoryViews remain Dalton-owned. Native fcadec/native proof and SessionContext interfaces unchanged, no live conversion/replay/provider use.

Draft checkpoint: core imports work. Queue/selected-lifetime tests initially29PASS/1FAIL/1skip13.85s; failure exposed a removed captured-source check at final native admission. Corrected via existing typed admission rule and both missing-source cases PASS2.32s. First nonexistent test filename produced no tests and is retained honestly. Remaining focused authority/feedback, current bridge fixture closure, actual installed saved-native/ACP/queued acceptance and ratchet still in progress; not ready/live.

Private native-evidence experiment preserved only on local47518041, excluded from this branch. Older completed own derivatives (~15MB) removed after process-reference checks;378canonical/candidate/wheel/receipts and source retained. Resource warning means serial bounded checks, no additional agents or large matrix.

## Ready after current-main integration

Merged current main13d92f80 (381/378); resolved the one shared test fixture conflict using381's typed request/native writer APIs and383's OriginalTurnInput notice owner. SelectedExecution/coordinated_runtime and Boyle's `_drain_private_nk` selected-write callback region remain untouched. Production delta against that main: **213 lines deleted,308 added** across11 files. New capture/dispatch behavior includes an actual late authority check for initial inputs and a distinct before-dispatch refusal; postdispatch errors retain their uncertainty and stop handoff. This is admission/lifetime closure, not a claim that remaining InputDrain wake/pump behavior has disappeared.

Installed noneditable candidate `.artifacts/input-drain-lifecycle/runtime`; source production matches416be96c. Full package manifest verified against parent's combined immutable `/home/ts/.local/share/agent-comms/native-current-9213ee71479d1b20/node_modules/@earendil-works/pi-coding-agent`. No package changes or live conversion. Import receipt proves InputDrain and QueuedInput loaded from installed site-packages.

- **Actual installed selected native -> ACP queued fresh input -> one original start each PASS**, combined invocation7.91s. Other test in that invocation failed before native setup: obsolete `real_host` fixture parameter. Removed that obsolete argument, no compatibility restored.
- **Actual installed saved native selected summary -> native commit -> original start -> queued followup start PASS10.43s**. Reads durable native history and asserts compaction precedes exactly one start for each distinct input; no blocked summary remains. Both journeys use actual pinned native children and local HTTP, no paid provider.
- **52 installed focused authority/lifetime/queue/source checks PASS23.32s**,1 actual case deselected because already run above. Includes missing/foreign source, changed admission/goal, native refusal, terminal failure, late images/controller, no-replay cleanup and future receipt revocation.
- **5 ownership/deletion guards PASS0.61s**, including prohibition on the five deleted mirrored maps in actual production owners.
- Initial focused capture16PASS1.89s and feedback2PASS1.04s retained. The earlier owner replacement test was an invalid fixture: registry intentionally preserves creation identity. Corrected it to actual `new_owner=True` admission; it proves refusal before dispatcher entry. The first RED did not prove a production identity bug.
- Ratchet against main: **no increase**, InputDrain god-class excess−53, CommsAgent−1, raw string subscripts−1; long-chain terms/foreign absence/codec subclass counts unchanged. Pattern IDs in scope: IMPL-1/4, IDEN-1/3, BOUND-1, TIME-1/3. All removed attribute/API callers migrated, no aliases.

Known remaining boundary: parent owns paired live UI acceptance/installation. This candidate is ready for source review/merge, not already live. No repeat unchanged378 proof matrix and no CI gate. Canonicalfcadec/source/branch/receipts retained per parent; derivative fixture copies may be removed only after process-reference checks.
