# Final T2 integration handoff — 2026-09-28

Core source head before this receipt: b19fd25f (includes92909639; main272 fdf3c917; parent270 aa413870). Paired Toad source head0505280 plus final mid-turn receipt commit. Same origin/refactor/t2-acp-boundary-20260928 branches / PR262 and122.

## Install/pin contract

Use both candidate wheels together. ACPFailure is independently importable without Agent/widget: from_error(code,message,data), title/detail/action/input_disposition/feedback. Delivery state is structured InputAttempt declaration provenance, never diagnostic text. Display cases own matches/classification_priority. DeliveryFailure inherits270 DeliveryPresentation; admission/unknown presentation and EventMerge are retained. Canonical272 FieldCodec handles Message wire projections; TranscriptCodec is not restored.

No additional T2 runtime-format reset. Existing history, registry, queues, coordination stores retained. Parent272's compaction journal/input_dispositions reset is separate. No live installation, route/launcher/root mutation, paid provider call, CI wait or input replay was performed here.

## Exact current installed/native results

Candidates were built as noneditable wheels in .artifacts/paired-installed; Python imports use those wheels. Native current package prepared via stack/bin/prepare-pi-native into owned persistent stack/.pi-native-5fdef596596173bd. Do not use old905 for current272 acceptance (tree mismatch correctly refused).

- `PI_COMPACTION_TEST_PACKAGE=<current package> .artifacts/paired-installed/bin/python evidence/t2-boundary/native_error_acceptance.py`: PASS actual ACPstdio -> independent owner -> current pinned Pi -> local HTTP402. One POST, no replay, provider reason and structured Started disposition. Captured native-error-receipt.json.
- Paired Toad `PYTHONPATH=tests T2_ERROR_RECEIPT=<receipt> <installed python> evidence/t2-boundary/acp_failure_feedback_pilot.py`: PASS actual captured wire via SDK into mounted UI; failure summary/action/disposition visible, next draft retained, busy settles. Unstructured nested summary reason explicitly Unconfirmed.
- `RETAINED_COMPACTION_SOURCE=<owned copied137MB history> AC_NATIVE_STACK_BIN=pi PI_COMPACTION_TEST_PACKAGE=<current package> <installed python> -m pytest -o addopts='' tests/test_retained_manual_compaction.py --basetemp=<owned>`:2 passed91.61s, manual AND adaptive actualnative commit/journal/reopen/original unchanged/no replay.
- `AC_MCP_NATIVE_BIN=pi AC_MCP_TOAD_ADAPTER=<paired tests/toad_mcp_observer.py> PYTHONPATH=tests:<paired tests> PI_COMPACTION_TEST_PACKAGE=<current package> <installed python> -m pytest -o addopts='' tests/test_mcp_acceptance.py -k allow --basetemp=<owned>`:1 passed12.08s actualnative Pi+MCP SDK+owner socket+mounted UI+loopback provider. Offered Allow once selected only by the test adapter, MCP echo called, result delivered, receipt cleared.
- Same MCP native command `-k 'not allow'`:2 passed (no_controller, revoke_midturn),1 failed (disconnect),33.96s. Failure: unchanged Question mount callback queried missing #option-container after disconnect removed the widget. Integration owner parent/Carver notified; no claim full MCP matrix green.
- Installed queue/cursor request-race/queue-admission pilots:3 passed; exact IDs, foreign/retired owners, generation fences, delayed new/load/stop results and rollback retained.
- Installed midturn_compaction_pilot: PASS typed producer events through UI; stale usage cleared, one summary, manually aborted failure explanation visible, subsequent measurement restored.
- Installed native_message_parts_pilot and coordination_context_pilot: bothPASS; one timestamp/exact Markdown/list/fence, owned hidden context lazily inspectable, human quoted headers retained.
- Installed acp_sdk_boundary_pilot: PASS preserves raw external extensions and visibly rejects malformed ACP updates.
- Installed focused current core boundary/runtime/image/queue/tool batch61 passed10.38s; later current native combined93 passed2 skipped3 failures. Obsolete fake cold-start MCP function removed in favor of actualnative harness; typed settings exception expectation corrected. Targeted correction14 passed2 skipped9 deselected10.14s. Earlier ACP/selected-write/goal/failure batch110 passed3 skipped11 failures on old905 native pin; actual owner prepare rerun against current package passed affected production cases above. No whole-suite claim.

## Integration review facts

Core changed production ratchet has no increases; no old flag/shape decoder restored. Toad deletes QueueReducer/CursorReducer/copied notification classes and reduces TypeIdentity7, LongBooleanChain9, StringSubscript51. Four view lexical ClassSize counts still grow (App36, MainScreen7, CommsScreen4, SessionView4); local per-class ratchet is NOT green. Preserve original source formatting/comments on unchanged methods/statements; remaining integration review belongs to parent/Carver and must not be hidden by aggregate deletion.

Existing Textual70-column reply-route layout recursion also reproduced on current live installed baseline, independent of T2. Main T2 path works; discovered Question disconnect race and lexical ratchet residual are explicitly routed for integration. CI deferred.

Predecessor trees unchanged. All source work recovered/published in persistent ~/wt checkouts. Own large disposable pytest/history copies are cleaned after retaining these receipts; current package and installed candidate kept for integration reproduction.
