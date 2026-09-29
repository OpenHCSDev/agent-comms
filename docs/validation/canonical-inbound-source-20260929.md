# Canonical inbound history source

Companion to Toad #207; owner: existing inbound history worker. Scope: existing durable assignment and native transcript sources, one combined chronological cursor/page, no independent state store. Pure passive/ignored receipts must remain recorded history rather than become live tail input after a whole-source coverage scan.

Acceptance: actual installed private ACP/native saved-history UI, approximately 41 MB retained source, continuously open 30 seconds, cold/new window and A/B/A with immediate input, exact wire identity once, original placement/handling and no replay. No live root mutation or uncertain input replay. Implementation underway; this scope commit establishes the draft before extended work.

## Ownership and deletion checkpoint

102 production lines deleted from the replaced native-only preview/page traversal. Existing `NotificationAssignment` decodes original durable assignments; `AssignedTranscriptSource` projects their original wire records into the native source's chronological traversal. `AssignedSourceCursor` carries root, ThreadIncarnation and wire sequence; `TranscriptCursor` owns the combined progress comparison. No receipt index, seen registry, clock cutoff or reconstructed acknowledgement is added.

IDEN-5 / BOUND-2: historical assignments were consumed as new tail input because native file coverage could never establish the existence of a passive receipt. TIME-1 / IMPL-13: Toad's full-source coverage scan and every caller are removed in paired #207; bounded preview and paged history now consume the same source. Earlier/Later traversal hooks own direction rather than maintaining a second traversal procedure.

Actual isolated installed paired candidate: small saved source and 47,316,341-byte native SessionManager journal passed continuously open 31-second view, genuine fixture owner restart, actual ACP reconnect and first native input, no old live tail and no old model replay. Final physical A/B/A immediate-Enter/CPU journey in progress, not a default activation receipt. Current source verification: 27 passed, one unsupported source-case skip. The live user's bus, owners, history and uncertain inputs have not been changed.

The native source, ACP process and UI are real. Only the localhost provider responses are controlled. Existing FieldCodec carries the composite cursor unchanged; no alternate protocol decoder or compatibility format. The selected installation must pair #420 with #207; parent owns integration and activation.

## Final paired acceptance

Exact paired installation passed with exit 0: 41,602,421-byte retained native journal; continuous 31-second open view; cold fixture owner restart and ACP reconnect; first new input; real fork to alpha; five physical A/B/A agent-tab selections with immediate Enter; then a fresh channel receipt and repeated handling updates. Exactly 11 expected localhost provider requests, no old input replay, no old tail append, no UI exception. Earlier 47,316,341-byte saved-source run also passed the continuous/reconnect/first-input boundary.

Instrumented selection took 606–1540 ms and Enter-to-provider 4728–6080 ms; cProfile and current native/UI work are included, so this proves useful functionality, not the final latency target. Parent and #202 keep residual warm/CPU/turn-lifecycle scope moving independently. Final retained source, profile and logs are at `/home/ts/.cache/agent-scratch/toad-restored-inbound-chronology-20260929/candidate-switch-final`. No live bus or owners were changed; uncertain user inputs were never retried.

Ready for parent review and paired #207 activation. Local source tests: 27 passed, one existing unsupported source-case skip. CI is deferred; final default entrypoint verification belongs to parent activation, not this isolated candidate receipt.
