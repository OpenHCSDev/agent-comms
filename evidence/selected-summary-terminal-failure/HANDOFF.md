# Selected summary terminal provider failure

Base: main 5620e294 (merge of PR269). Requested shorthand `2695620` was not a Git revision.
PR271 native/receiver checkpoint: 2c9d35be.

## Contract for parent

Native summary-only request may return `status: failed` only after pinned provider error terminal receipt matches the joined stream result, all concurrent streams have settled without uncertain failures, operation was not cancelled, and full original native witness, selected model/settings/bindings and idle queues still match.

Wire: version, status, operationId, witness, selected, settings, reason.
Reason is diagnostic only; no quota/provider-error-string classification. Cancellation, malformed/unjoined streams, native source drift and uncertain transport remain UNKNOWN.

Python decodes SummaryFailedData, verifies operation and complete witness/model/settings plus live child and unchanged session revision, calls `journal.fail_selected_summary(operation, reason)` then raises SelectedSummaryFailed outside the UNKNOWN handler. The parent supplies that journal method and owns durable FailedSummary/NotSentInput and recovery. Neither native nor receiver sends original input, retries a summary or commits a summary on failure.

## Package

`/home/ts/wt/comms-selected-cold-compaction-native-20260928/stack/.pi-native-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent`

Canonical builder passed per-file and entire-tree manifest validation. Durable/live 905f untouched. Parent owns integration/deployment.

## Evidence

- Actual pinned cold CLI with selected local OpenAI-compatible HTTP 400 and 429: both return typed failed, two distinct map chunks with no retry, saved history unchanged, no original input events, fresh reopen idle, all twenty historical messages preserved, no provider calls on reopen.
- Actual CLI stopped while HTTP request active: selected operation becomes durable UNKNOWN, input remains blocked, source unchanged, fresh reopen does not replay. Three actual native cases passed in 12.27s, no paid provider calls.
- Receiver and durable local-pipe controls: 33 passed, two opt-in synthetic native cases skipped. Includes exact failed-receipt fence controls for foreign operation/witness/model/settings, missing proof and invalid diagnostic text.
- The opt-in synthetic provider-error integration expectation now requires parent's FailedSummary journal implementation. Run with PI_NATIVE_PACKAGE_DIR after integrating parent changes.

## Parent API integration

Merged parent 855d8760 into this branch. Combined receiver/native/recovery tests: 41 passed in 31.75s with both native environment variables enabled; no skips.

Upgraded the same real HTTP 400/429 fixtures to call SelectedSummarySlot and the actual journal.fail_selected_summary method. The tests assert SelectedSummaryFailed, durable FailedSummary (terminal, settled_without_original, original_eligible false), empty blocking_selected_summary, live idle child, unchanged saved source, no retries or original input events, and fresh unchanged reopen. The active-child-disconnect control still uses the actual slot/journal and remains UNKNOWN. This closes the prior direct-RPC-only gap without new provider usage.
