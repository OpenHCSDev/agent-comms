# Provider connection failure feedback — PR276

Parent owns provider transport recovery and deployment. Current transport settings are unchanged. No original input is replayed or automatically retried.

## Ownership / public interface

Native `PiMessage.diagnostics` is decoded by the existing `PiPayload` normalization and canonical FieldCodec. `PiDiagnostic` owns its declared records; `ProviderTransportFailureDiagnostic` carries typed error code/name and details. Known stage cases own their descriptions; absent and unrecognized stages are separate declarations. No central phase roster or parallel codec was added. Native stack paths are not needed for readable feedback.

The existing Error event carries those typed facts into ACPFailure. `ProviderConnectionFailure` owns classification, title, detail presentation and action; the existing failure registry discovers it. `ACPFailure.from_error(code, message, data, *, diagnostics=())` remains widget-independent. `.detail` preserves the original provider text; `.description` and `.feedback` include the structured stage/code/transport/request facts. `.diagnostics` exposes the typed facts. The native caller supplies already-decoded records; raw error-data diagnostics use the same PiDiagnostic boundary.

Input disposition comes exclusively from the existing InputAttempt owner. Native usage0, empty content, event-emission status, stage text and diagnostic strings never determine send/binding/retry state. Without a structured input state the display is Unconfirmed. Actual native Started remains Started — input not retried.

Existing Toad already renders `.feedback`; no production Toad/widget edit is required. A log viewer wanting the stage should use `.description` or `.feedback`, not just the original `.detail` field.

## Evidence

Read-only observed native failure: nra-architecture18:35, saved assistant entry60478984. Fixture tests/fixtures/native_provider_transport_1011.json preserves actual diagnostic type/name/code1011, configuredTransport:auto, eventsEmitted:true, phase:after_message_stream_start, requestBytes159312, stopReason:error, content[] and usage0. Machine-specific stack paths are omitted from the public fixture. Original history/log/root was not mutated.

Fresh noneditable core wheel in owned .artifacts/paired-installed; no candidate source override. Installed Toad is the paired T2 wheel, with unchanged production failure consumer. Dependency fallback is the installed final runtime.

- `PYTHONPATH=tests <installed python> -m pytest -o addopts='' tests/test_provider_transport_feedback.py tests/test_acp_failure.py tests/test_pi_payloads.py tests/test_acp_extension.py`:35 passed0.42s. Observed frame decoding, strict native fields, current-only failure family, structured Started, no diagnostic-text delivery inference, unknown stage typed observation, ACP roundtrip.
- `PI_COMPACTION_TEST_PACKAGE=<current5fdef native package> <installed python> -m pytest -o addopts='' tests/test_acp.py -k 'error or failure or preflight'`:12 passed5.10s,71 deselected. Affected caller/preflight/error behavior retained.
- `T2_PROVIDER_FAILURE=websocket T2_ERROR_DEBUG=<owned debug> T2_ERROR_RECEIPT=<native-websocket-receipt.json> PI_COMPACTION_TEST_PACKAGE=<current5fdef> <installed python> evidence/t2-boundary/native_error_acceptance.py`:PASS actual ACPstdio -> independent owner socket -> verified unmodified native Pi -> loopback WebSocket response-created event then close1011. One provider request, no retry or replay, durable native input proven Started. Failure title/code/known after-stream stage/configured transport/events emitted/request bytes retained. Uses local fake-account credentials and no external provider call.
- `PYTHONPATH=<paired Toad tests> PROVIDER_FAILURE_RECEIPT=<native receipt> PROVIDER_MOUNTED_RECEIPT=<mounted receipt> <installed python> evidence/provider-transport-feedback/mounted_feedback_acceptance.py`:PASS actual captured native failure through ACP SDK into mounted Toad. Provider connection title, code1011, after-stream stage and Started disposition visibly rendered; draft unchanged, busy0, turn settled. Mounted stage wraps naturally across lines; the test checks its visible pieces and complete typed description.
- Changed production and focused regression test lint: passed.

Native receipt and mounted receipt are published beside this file. No live route, launcher, settings or installation changes here; parent applies the tested commit. CI deferred, no full-suite claim.
