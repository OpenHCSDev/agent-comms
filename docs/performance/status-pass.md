# Observed thread status pass

Follow-up to #946. Implementation and acceptance are unfinished.

## Measured source

One warmup and **one** cProfile pass on installed Core `e069e0b48`, 73 observed executable threads, using the existing frozen-status composition with actual read-only transcript source windows. This is not a frozen whole-bus A/B. No UI message or provider test input.

Warmup 5254 ms. Profiled pass 18225 ms, including instrumentation and live lock contention: 13230 ms in source capture, 4995 ms outside it; 73 presentations, zero busy/unavailable. Do not compare this profiled elapsed time as an uninstrumented regression against #946's 2831 ms median.

| Inclusive frame | Calls | ms |
|---|---:|---:|
| `Transcripts.capture_page_window`, transcripts.py:434 | 73 | 13229 |
| `Registration.snapshot`, registration.py:363 | 143 | 7145 |
| `ThreadPresentation.with_sources`, thread_presentation.py:148 | 73 | 4912 |
| `MessageNotification.delivery_window`, presentation.py:168 | 73 | 3399 |
| `NotificationAssignment.read`, notification_assignment.py:459 | 73 | 1474 |

Frames overlap and cannot be summed. `fcntl.flock` alone consumed 10386 ms across 617 calls. The largest non-source projection contains 71 full read-ledger document reads (1749 ms), 1547 ms decoding, and 1.36 million scalar decodes. `MessageNotification`, not `BusPresentation`, owns these delivery projection methods; both are in presentation.py.

## Owner correction being implemented

`ThreadPresentations` already owns the acquired status pass. It will acquire the actual transcript source root's existing `ReadDocument` once and pass that typed value into `ThreadPresentation.with_sources` and the existing `MessageNotification` projection family. Independent notification operations and recorded-source readers must likewise supply one acquired document, including multi-window reference reads. Delete the inner per-window ledger read. No new cache, codec, wrapper, ledger format, worker or timer. Empty/missing selections still perform no unrelated document IO. Release metadata locks before source IO, retain the original per-source native capture/incarnation checks and foreground yields.

Core #950 owns typed read membership and its separately reviewed live carry. This compatible acquisition change does not depend on installing that format or duplicate that implementation. Core #955/#916 own status/attention and outgoing publication changes; only notification acquisition/signature migration belongs here. Native registry/source-lock work remains separately visible; removing repeated ledger decoding is not a claim to eliminate every wait.

## Source evidence before editing

Refactor-audit `Package`/`Repository` parsed Core328, Toad282 and Textual254 production modules at main `272b9a285`, Toad `45a4457b`, Textual `2fea115f`; zero parse omissions. Inventory includes declarations, inheritance, imports, assignments/writes, decisions and all attribute/name consumers across the affected family. Existing installed dependency inventory covers metaclass-registry9, ACP38, Pydantic105 modules, zero omissions. Attribute spelling/dynamic receiver resolution and compiled locking are not statically certified.

Required ownership: `ReadLedger` decodes its `ReadDocument` at the operation boundary; all projections consume that acquired document. Per-thread source windows remain independent authority, not a batch-wide synthetic source. BOUND-1 is the repeated boundary work being removed. No behavioral or installed acceptance claimed yet.
