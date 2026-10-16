# Observed thread status pass

Follow-up to #946, draft #967. Source implemented and checked; not merged or installed. The full observer latency target remains unmet.

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

## Implemented owner correction

No new production class is introduced. `ThreadPresentations` already owns the acquired status pass. It acquires the actual transcript source root's existing `ReadDocument` once and passes that typed value into `ThreadPresentation.with_sources` and the existing `MessageNotification` projection family. Independent notification operations and recorded-source readers likewise supply one acquired document, including multi-window reference reads. The inner per-window ledger read is deleted. No new cache, codec, wrapper, ledger format, worker or timer. Empty/missing selections still perform no unrelated document IO. Release metadata locks before source IO, retain the original per-source native capture/incarnation checks and foreground yields.

Core #950 owns typed read membership and its separately reviewed live carry. This compatible acquisition change does not depend on installing that format or duplicate that implementation. Core #955/#916 own status/attention and outgoing publication changes; only notification acquisition/signature migration belongs here. Native registry/source-lock work remains separately visible; removing repeated ledger decoding is not a claim to eliminate every wait.

## Source evidence before editing

Refactor-audit `Package`/`Repository` parsed Core328, Toad282 and Textual254 production modules at main `272b9a285`, Toad `45a4457b`, Textual `2fea115f`; zero parse omissions. Inventory includes declarations, inheritance, imports, assignments/writes, decisions and all attribute/name consumers across the affected family. Existing installed dependency inventory covers metaclass-registry9, ACP38, Pydantic105 modules, zero omissions. Attribute spelling/dynamic receiver resolution and compiled locking are not statically certified.

Required ownership: `ReadLedger` decodes its `ReadDocument` at the operation boundary; all projections consume that acquired document. Per-thread source windows remain independent authority, not a batch-wide synthetic source. BOUND-1 is the repeated boundary work being removed. AST is source evidence, not behavioral or installed proof.

## Verification and remaining cost

The same retained 73-thread composition, one warmup plus **one** unprofiled measured pass on this source: **one source-root ledger decode**, one status registry snapshot, one runtime decode, one waits decode, 73 actual source windows, zero busy/unavailable. Complete 3173 ms, source 1199 ms, outside capture 1974 ms. Growing original sources/shared host load were not frozen. No controlled A/B latency improvement claimed; the pass still takes seconds. Native registry/bus lock waits and notification assignment/projection costs remain visible to their existing owners. The 71→1 ledger-decode result is not a full sidebar budget acceptance.

Actual installed Toad bad338d6f with Core source overlay ran on owned plain st/Xvfb197: retained core-route-counts startup, growing native history and sidebar/status updates, then normal quit. Full-filename read-only stack confirms `ThreadReading → ThreadPresentations.read → ThreadPresentation.with_sources → MessageNotification.project_delivery_window` from this checkout. All frontend/ACP/observer/pool descendants exited; Xvfb retained. This is affected source application-path verification, not installation or a timed73-thread actual-UI pass. Remote-execution counter probes produced no receipts and were not used as verification; no production change was made for those diagnostics.

After normally integrating main e7a08b454 (#955), all30 focused checks pass again, including single ledger decode, metadata locks released before native IO, missing-name laziness, original rename refusal, per-source busy isolation, same-revision recovery, live multi-window references and recorded-source read/identity behavior. Nine broader failures reproduce exactly on base272b9a285: old receipt labels, phase-summary assertions, receipt-object equality, gateway test rendezvous and an obsolete error-string assertion. They are unchanged, not weakened. Local logs and the bounded profile remain in the existing owned evidence tree; CI deferred.

Final current-main source check also used the normal launcher with installed Toad2885cddb9/Coree7a08b454 plus this Core source overlay: saved startup, live native body/status updates, physical sidebar focus and prompt return, normal quit. Actual observer stack confirms this checkout's current sidebar/native process path; all owned descendants exited without traceback/reset/EBADF/unraisable text. No queued/failed input was retried and no UI message was submitted.

Final AST: Core328/Toad282/Textual254, zero parse omissions. `ReadDocument` and `ReadLedger` each remain declared only in read_ledger.py; `ThreadPresentations` remains declared only in history_views.py. All delivery projections consume the explicit captured document. Live and recorded reference callbacks were traced through their shared two-argument sources/document contract, including the existing recorded-source partial. No positive production debt-ratchet deltas against e7a08b454. Ruff py314 comparison reports21→19 pre-existing whole-file findings, zero added issues; full-file lint is not clean. Review and installed acceptance belong to Codex; no separate runtime build performed.
