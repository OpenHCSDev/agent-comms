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
and live acceptance remain required.

## Real-path completion correction

The paired configured-provider CONNECT-refusal run exposed another competing
reconstruction that the initial source pass missed: `AgentEventUpdates.on_done`
converted the terminal `Done.text` into a new unrecorded `Error`. The actual ACP
log `Agent_Comms_2026-10-12T21_45_26_695430.txt`, lines 21 and 25, independently
confirms native/provider-connection publication followed by generic/unrecorded
publication of `fetch failed`. The initial checkpoint did not achieve once-only
publication. This is a confirmed defect, not a presentation-only issue.

The replacement deletes `AgentEventUpdates.on_done` entirely. `TurnOutput.done`
now yields the completion family: the original `Done`, preceded by an `Error`
only when failure first arose during terminal settlement rather than through
its existing error path. No new published flag or deduplication cache is added.
`TurnSession.finish_result` consumes this owner-generated sequence. The separate
invalid-launch producer publishes its actual launch error before its terminal
outcome; automatic retry exhaustion now uses `TurnOutput.error` instead of
writing terminal failure prose directly. These are the complete terminal-only
error producers; other Done consumers retain their outcome/goal responsibilities.

Before-edit completion census used NRA `ModuleSyntaxIndex` over the same 834
production/dependency modules, with zero parse omissions. It covered Done/Error
producers and consumers, output-error writes and terminal-result publication.
After-edit search has no Done-to-Error reconstruction in ACP publication and no
non-None direct write to `session.output.error_message`. Native preparation's
Done consumer was inspected: it still raises failed preparation outcomes, while
its caller retains the existing genuine-exception publication path.

Production imports and `git diff --check` pass for this correction.

## Corrected candidate live acceptance

The installed private paired candidate uses Toad `e48836d43` and Core production
`b8537e449`. The subsequent Core instruction-only commit does not change those
production bytes. Independent integration inspection of the raw ACP log
`Agent_Comms_2026-10-12T22_46_53_152699.txt` confirms exactly one `request_failed`
notification, with `native_message` provenance, and one terminal error receipt
containing the identical failure and `notification_published=true`.

The canonical native file independently matches the retained evidence: new
failure input C_F5 produces assistant entry `b3aa86d5` at
`2026-10-13T03:05:29.785Z`; a separate input C_S6 produces successful assistant
entry `70adae50` at `2026-10-13T03:10:45.525Z` with
`CANDIDATE_SUCCESS_AFTER_FAILURE`. Their input IDs differ, and the later native
user message contains neither the failed input marker nor its requested reply.
The configured provider/model is unchanged. The controlled localhost transport
refusal produces `fetch failed` without a provider diagnostic, so generic failure
classification is correct for that native message; the original diagnostic-rich
WebSocket record was separately verified through the codec without diagnostic loss.

The same already-open candidate view's `after-success.png` visibly places the
failure at its original historical position before the later input and success.
The latest publication capture at native offset 147299152 contains only the
canonical `TranscriptHistory`, not local failure Notes or log-link siblings.
The evidence is owned by `toad-close-lifecycle` under
`/home/ts/wt/toad-close-lifecycle/.artifacts/error-placement/candidate`; native,
ACP and image evidence were read independently, not accepted solely from checks
stored in its JSON report. No original failed/UNKNOWN input was replayed.

This establishes corrected candidate once-only publication and retained temporal
placement. Default-runtime installation and the installed final UI/log-access
journey remain distinct delivery steps with the existing Toad implementation owner. Original sessions, UNKNOWN inputs and blocked
historical goals are untouched. The cause of the original provider disconnect is
not established by this repair.
