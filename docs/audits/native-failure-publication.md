# Native failure publication and history provenance

## Defect and ownership

The retained `cce5b071` assistant failure produced two ACP notifications: a
provider-connection failure with its diagnostic, then a generic request failure
with the same text but no diagnostic. `OwnedTurn.run_native` raised its existing
published failure receipt inside the exception handler's protected body.
`TurnProgress.report_failure` caught that receipt and reconstructed an `Error`
from the prior failure's text. `AgentEventUpdates` compared whole failure values;
the reconstructed value did not equal the diagnostic-bearing original.

The completed native failure receipt now leaves `OwnedTurn` outside that exception
publication body. Genuine preparation, streaming or result-publication exceptions
still settle the input and publish their own diagnostic. `TurnProgress` no longer
reads or receives the published-failure registry. The value-equality suppression
in `AgentEventUpdates` is deleted: distinct native failures are observations, even
when their text/diagnostics happen to match.

## Source and consumers

`FailureSource` in `acp_failure.py` declares the provenance family once.
`ErrorStopReason` supplies `NativeMessageFailureSource` containing the decoded
`AssistantMessage`; errors without native provenance use `UnrecordedFailureSource`.
`TurnOutput` applies its existing privacy policy before publication. The source
owns redacting its nested message and takes the already-redacted text from
`TurnOutput`, rather than declaring a second privacy message. Ordinary publication
retains the original message object. `Error` and `RequestFailedUpdate` carry this
same source through the existing publisher and codec.

Native provenance does not assert durable journal identity. Pi's
`AgentSession._handleAgentEvent` emits `message_end` before `appendMessage`.
Canonical history remains the owner of the actual native entry ID and timestamp.
No response ID is relabeled as a journal ID. Toad's corresponding consumer repair
is owned by `toad-close-lifecycle` and is not claimed delivered by this Core PR.

## Structural evidence

Before editing, NRA's `ModuleSyntaxIndex` covered Core and Toad production roots
and installed Textual: 834 modules, zero parse omissions. The relevant declarations,
imports, calls, assignments and conditions were inspected, including
`MessageEnd.apply`, `AssistantMessage`, `ErrorStopReason`, `TurnOutput.error`,
`AgentEventUpdates.on_error`, `OwnedTurn.run_native`, `TurnProgress.report_failure`,
`RequestFailedUpdate`, native entries, and Toad's failure consumers. Dynamic MRO
handlers were read semantically; this census is not proof of runtime dispatch.
The query output is retained under the owning scratch directory
`/home/ts/.cache/agent-scratch/comms-native-failure` until delivery.

Before: error registry read in publisher, owned turn and progress; progress
reconstructed native failures; publisher guessed duplicate identity by equality.
After: registry is written by the publisher and read for terminal receipt selection
by `OwnedTurn`; progress publishes only genuine outer exceptions. Provenance is
one declared family, carried by the event and transport rather than inferred
from failure prose, classifications or diagnostic presence.

## Verification checkpoint

Manual source-overlay sanity check used the installed paired Python environment
and the original retained failure read-only. Existing `encode_updates` and
`decode_updates` round-tripped the exact native message and provider diagnostic;
classification remained `ProviderConnectionFailure`. Sensitive publication removed
content and diagnostics, retained native provenance, and round-tripped. Unrecorded
startup failure also round-tripped. Production imports and `git diff --check`
passed. No automated tests were run.

Real ACP/native failure publication, already-open Toad reconciliation, installation
and live acceptance remain required. Original sessions, UNKNOWN inputs and blocked
historical goals are untouched. The cause of the original provider disconnect is
not established by this repair.
