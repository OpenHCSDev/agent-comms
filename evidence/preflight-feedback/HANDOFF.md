# Truthful terminal preflight feedback

Deletion first: removes exit-only startup stderr dispatch and partial-row ACP input classification. Removes singular InputDispositions.finish_unbound; terminal callers and tests now use one atomic settle_unbound batch. InputDrain cleanup delegates without growing its existing god class. No backend/watchdog policy, startup budget, provider, live history or launcher change.

Owner: InputDispositions settles original unbound reservations under existing wire/document fences before terminal error publication (Done and OwnedTurn exception). Sent/started/uncertain states retain their declaration-owned transitions. Complete homogeneous InputDocument.shared_state projects durable evidence; missing/mixed rows cannot assert not-sent. ACP keeps existing RequestFailedUpdate/InputFailedUpdate family and codec. The existing emitted_errors map now holds ACPFailure rather than a string; deduplication compares complete typed diagnostics and delivery evidence. Earlier identical text is republished when terminal settlement changes evidence, while unchanged failures remain single publications. Done and exception callers share this owner.

TurnFailure.with_startup_diagnostics owns applicability; InputIdUnavailable enriches exit OR timeout with original phase/timing and stderr. TurnOutput retains existing sensitive/image redaction. No string inference grants delivery/retry, no second store/state family/codec, no global caps or automatic replay.

Latest NRA/audit reread. Patterns IMPL-4/5 (failure-owned shared dispatch), IDEN-1/3 (durable evidence vs transport diagnosis), BOUND-2 (existing InputDocument), TIME-9 (same canonical ACP family/store). Parent owns backend.py/turn_watchdog.py intermittent startup diagnosis; these files unchanged.

Focused owner tests and actual native/UI receipts under evidence/preflight-feedback. Actual native import boundary intentionally rejects an unapproved private extension; no provider request or native user input. Normal installed App/Pilot sends via physical Enter and paints useful native startup cause plus Not sent/input not retried; this is not an intermittent timeout reproduction. Timeout diagnostics additionally tested through existing subprocess backend test. Native initial attempts retained RED harness marker/path failures, corrected capture follows.

Isolated persistent worktree /home/ts/wt/comms-preflight-feedback-sol-20260929 from current main a01927. Private native d396/Textual1738/installed Toad165; parent owns live gate/merge/cutover. No live mutation or tools/provider calls. CI deferred.

## Final evidence

- Actual installed native App physical Enter -> import-boundary native startup failure -> RequestFailedUpdate and InputFailedUpdate NotSentInput -> compositor Not sent/input not retried/useful native stderr: PASS exit0 (`native-complete.log`, `native-complete/feedback.svg`, `native-complete/paint.txt`, ACP log). Zero provider requests/native user starts. This proves actual startup failure presentation; it does not reproduce the intermittent 41MB timeout or solve parent startup latency.
- Focused timeout subprocess + typed projection/failure owner: 14 PASS/189 deselected (`focused-ready.log`). Includes timed-out stderr retained and sensitive redaction.
- Canonical document/summary recovery: 28 PASS/2 native-disabled skips (`store-final.log`). No claim skipped cases passed.
- Eight touched production files ratchets: no foreign-absence, chain-term or >500 class-excess increase (`ratchet-complete.json`). InputDrain class excess decreases 1.
- Earlier obsolete PersistentPiSession.proc fixture corrected to actual custody child; earlier marker/path harness REDs retained. Terminal prior Error is intentionally corrected from reserved/unknown to durable not-sent; unchanged terminal/exception feedback still once. Final focused terminal cases recorded separately.

Next: parent review and affected live-entry gate; backend/watchdog startup latency remains parent-owned and unchanged. No paid calls or live mutations performed.

Final terminal correction cases: 3 PASS/6 deselected in 35.28s (`terminal-corrected.log`), after old already-collected text-only assertion RED. Full terminal run had 8 other cases PASS; only that replaced assertion failed. No unchanged broad rerun. Final production delta deletes 32 old lines across 8 files.

## Reviewed post-dispatch correction

Removed unconditional "The prompt was not sent" from InputIdUnavailable diagnostic enrichment: this failure also represents partial native writes after capability attestation. Enrichment preserves original failure text plus stderr only; the existing durable ACP input_state owns delivery feedback. Focused real-store check binds a reservation to BoundUnknownInput/SentInput, records post-dispatch BrokenPipe diagnostics, settles terminal batch, and proves no transition/no false not-sent claim; unknown feedback remains truthful. 4 focused checks PASS/0.05s (`post-dispatch-correction.log`). Prior actual installed UI preflight receipt remains applicable; no unchanged native matrix rerun.
