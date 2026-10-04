# Original submitted input / recorded context binding

## What changed

The private reader compared the submitted row's turn ID with itself. It now uses the original sealed ContextManifest's RecordedContextTurn.identity as the expected turn in SentInput.matches_native. It returns that same acquired input member to construction, which passes it to the existing request reader. StartedInput.require_started checks every retained request lease's admission_generation. Turn generation and admission generation are deliberately separate; neither is inferred from the other.

InputDocument is decoded once, original sent text/native ID remain checked, and UNKNOWN is never selected as started. MissingInput represents a direct-native control. A capture without its manifest keeps only submitted/sent-text scope; missing diagnostics cannot establish admission. Stored names/admissions do not establish birth, process identity, a complete lease, provider submission or replay permission.

## Owners and consumers

Existing StoredInput/SentInput/StartedInput own submitted source, native binding and admission; ContextTurn owns recorded-versus-preview semantics; TurnLeaseFence owns the diagnostic admission. No new class, store, codec, registry or runtime observation. RecordedNativeProbe.read is the sole submitted-prompt consumer; it passes the acquired member through construction/observed_requests. Capture, recorded source delivery, continuation, individual/public scorer and paired runner consumers inherit through the existing reader. All direct construction/request controls migrated to the required existing InputAttempt argument.

The existing refactor-audit Package parsed 734 Python modules under src/tests/tools without omissions. Before/after lexical declaration and consumer output is in [evidence](../../evidence/s4-input-context-turn-20261004/receipt.json). It does not establish dynamic dispatch or JavaScript behavior. The reader deletes 11 replaced lines; private controls/caller migration delete 14. Runtime, stack and tools have zero change relative to determining main.

## Final validation and scope

One final affected batch: 5 passed, 34 deselected in 0.33s. It checks independent turn mismatch, preview refusal, exact native text, unchanged pinned source, UNKNOWN refusal, direct/missing evidence, acquisition once and admission3 versus turn generation11. Existing request budget/timing and branch-construction consumers run in that same batch. Normal main integration preserves both qualified private files byte-for-byte; no repeated checks or original SDK/provider/native/session read.

[Raw hashes and exact source](../../evidence/s4-input-context-turn-20261004/receipt.json) are published. This is private recorded-reader qualification, not configured multi-request capture, complete transformed-source/capacity, HTTP, intervention, billing or full S4 acceptance. No package/env/holder operation, provider input or original replay. Separate 30-pair/USD75 study remains unapproved. Historical missing records remain unavailable.
