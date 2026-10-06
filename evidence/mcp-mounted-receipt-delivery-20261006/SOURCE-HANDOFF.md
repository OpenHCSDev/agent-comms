# Mounted MCP receipt owns fixture response release

## Original result

The consumed installed-entrypoint attempt passed the allow case in 47.833 seconds.
Its no-controller case failed at the five-second attachment settlement wait;
revoke-midturn and disconnect did not run. The batch and all package/native claims
are closed. Original raw evidence, authored files and allow assertions remain intact.

## Source correction

`test_mcp_acceptance._mock_model` already waits for `receipt_seen` before replying
to the first localhost POST. Previously the passive owner `Audit.session_update`
set that event before the subscribed `Attachment` and mounted Toad observer had
consumed the MCP receipt. The response could therefore complete a no-controller
turn while its attachment was still processing earlier notifications.

`Attachment.session_update` now sets the same event after awaiting the original
mounted observer and decoding the genuine `McpClientReceiptUpdate`. Without an
observer it still requires that subscribed attachment to consume the receipt.
The passive audit remains an evidence sink. Both teardown unblock sites remain.
The model, timeouts, native producer, permission decisions, assertions and Toad
reset/binding owners are unchanged. No participant or turn state is injected.

## Determining source and evidence

The original no-controller audit records all 33 updates, including final settlement.
Its mounted observer records 17 updates and stops at the MCP receipt with no managed
turn or rendered MCP note. It first loses the managed projection after prompt
acceptance. The allow observer retains its managed turn at the receipt.

`OwnerSnapshotConsumer` may reconcile the actual registry after a turn finishes
when `AgentController.prompt_in_flight` is zero. This external observer has no local
controller prompt operation. Snapshot sequence checks prevent a read from replacing
a newer local projection; they do not establish ordering against undelivered native
notifications. A registry read can therefore precede an older receipt on this
attachment. The existing allow permission wait provides incidental ordering that
the no-controller case does not have.

The exact read/reset invocation and forwarding callback exception were not captured
in the original run. The response-release gap is established by source; attributing
the original timeout to a particular callback remains an inference. No production
reset or snapshot patch is justified by that missing trace.

`OWNER-CONSUMERS-BEFORE.json` uses the existing NRA/refactor-audit Package owner:
1,478 Python modules across both repositories' src/tests/tools, zero parse omissions.
`OWNER-CONSUMERS-AFTER.json` records the single operational release owner and unchanged
cleanup unblocks. The external `AC_MCP_TOAD_ADAPTER -> module.open_observer` selector
remains genuine; unselected third-party adapters are outside this trace.

Compilation and AST checks confirm all original native-case assertions and all other
top-level control functions are unchanged. The seven other frozen helpers are exact.
No imports, collection, tests, package builds, installed-prefix operations or native
operations were performed for this source checkpoint.

## Remaining acceptance

`AFFECTED-QUALIFICATION-PROPOSAL.json` selects only no-controller, revoke-midturn and
disconnect under the original controls and bounds, stopping at the first failure.
The accepted allow case and standalone origin guard are retained without repetition.
Holder, floor, package stage, proof, fresh matching READ/EXEC and final release remain
unbound. A future run needs those actual original-owner purposes; closure of the old
attempt supplies no authority.

The Core checkout normally joined current main before this change. Main's forced
APPEND_SYSTEM and notification source changes are present in the source branch;
the retained bee/4b/b2e/16c9 runtime is still its historical selected cohort. No full
current-main wheel equality, new build, native readiness or full four-case pass is
claimed.
