# T2 current paired closure — 2026-09-28

Implementation is published in the existing paired PR262/122 branches. Core integrates main272 fdf3c917 and parent integration aa413870 (270 DeliveryPresentation / EventMerge). Toad preserves117 ViewportPresentation and uses its windows/anchors. No parent cold-preparation methods or identity/input declarations were replaced.

## Shared interfaces and deletions

`acp_extension` owns CommsRequest / PromptRequest commands, encode_request / decode_request and AgentCommsUpdate records, encode_updates / decode_updates. Requests and updates decode once using canonical FieldCodec; TranscriptCodec was removed by272 and is not restored. Runtime producers and Toad consumers use those same declaration objects. Extension admission is current-only; no second decoder or own capability negotiation.

Deleted: copied Toad turn/input/queue/cursor/coordination/goal/compaction/transcript/MCP notification classes, QueueReducer and queue_view, native cursor parser/reducer, old raw golden fixtures, dead live replay fallback, snapshot/image/queue capability flags and private coordination shape probes. Queue/cursor attachments retain generation/revision/request-token fences and do not replay input or rebuild producer queue membership. App coordination_facts is the single public observed owner; weak keys avoid retaining closed screens.

`acp_failure.ACPFailure.from_error(code, message, data)` is public and widget-independent; title, detail, action, input_disposition and feedback are its display contract. Cases own matches()/classification_priority and are discovered from the existing declaration registry. Diagnostic text only classifies display; it never determines input delivery, retry, or binding. Structured InputAttempt classes own statuses; no structured fact means Unconfirmed. Parent DeliveryPresentation is inherited; UnknownDeliveryFailure/AdmissionBlockedFailure and parent EventMerge are preserved.

## Actual acceptance

Fresh noneditable core/Toad wheels in owned .artifacts/paired-installed; no candidate source override. The dependency fallback is the installed final runtime.

- Current installed ACP stdio -> owner process -> pinned current native Pi -> local HTTP402 provider: passed; provider reason preserved, one POST, no replay, structured started disposition. native-error-receipt.json is the actual captured wire/ledger receipt.
- Mounted installed Toad consumes that actual captured failure via SDK: passed; title/detail/action/disposition visible, next draft retained, busy state settles; unstructured nested -32603 remains Unconfirmed.
- Copied converted live native history (143686055 bytes): bounded28-event read and installed mounted ViewportPresentation passed; no prompt or source mutation.
- Current installed manual AND adaptive retained native compaction/commit/reopen:2 passed91.61s. Copied source unchanged, journal/native IDs checked, no original input replay.
- Actual current native Pi + MCP SDK + owner socket + mounted Toad Allow once:1 passed12.08s. Native receipt rendered, permission dialog mounted, actual MCP echo executed, result returned, receipt cleared at settlement.
- Installed queue and cursor request-race pilots and queue-admission pilot: all3 passed, including new/new, load/new, stop/retired agent, draft rollback, exact IDs and generation fences.
- Focused current installed boundary/image/queue/tool/runtime tests:61 passed10.38s before parent270 extension sync. Later affected combined native batch:93 passed,2 skipped,3 failures. Two failures were obsolete fake cold-start MCP tests (deleted in favor of the actual native path); the last was an old exception expectation now tested against PiSettingsEvidenceError. Corrected targeted remainder:14 passed,2 skipped,9 deselected10.14s. No aggregate full-suite-green claim.
- Changed ACP/selected-write/goal retry/failure cases in earlier124-case run:110 passed,3 skipped;11 owner-prepare failures were old905 pin mismatch, rerun against freshly prepared current native package above. Existing native opt-ins were not called green from skips.

## Integration and limits

Install the paired core/Toad commits together for current ACP request/update records. **No T2 persisted-format reset is required**: native history, registry, durable queues and coordination stores keep their formats. Parent272 owns compaction-journal/input_dispositions reset. Parent owns cutover; no live root, launcher or route changes occurred here.

Shared ratchet: core changed surfaces have no increased measured debt. Toad removes TypeIdentity7, LongBooleanChain9, StringSubscript51 and the old reducer/message classes. Four existing view classes still show lexical ClassSize growth for public route/coordination getters and changed caller formatting; the Toad per-class ratchet is not claimed passing. This is reported to parent for integration, not hidden behind the aggregate reductions. No CI waiting.

Existing Textual70-column reply-route layout RecursionError reproduced on current live installed baseline; not introduced by T2. Parent owns integration and can address that separately. MCP controls and final mid-turn fixture receipts are recorded in FINAL-ACCEPTANCE.md as they complete.

---

## Historical checkpoint (superseded)

# T2 paired checkpoint

Shared update decoding now covers queue, cursor, coordination, goals, compaction progress/commit/publication, transcript snapshots, input-ledger invalidation and MCP receipts. The native MCP JSON receipt is decoded once into declaration-owned server state and call policy; its old schema walk and state roster were deleted. Transcript snapshots are unconditional on this paired protocol; own snapshot/image capability negotiation was removed. Runtime presentation rebinds typed session scope rather than probing fields.

Verification: focused family round-trip/strict boundary test: 1 passed (pytest -o addopts=""). Default repository coverage configuration failed its aggregate 85% threshold when running just this test; no full suite pass claimed. Fresh ACP producer process through SDK JSON RPC into mounted actual Toad passed turn start, stale settlement rejection, matching settlement and compaction start/progress/end rendering using the installed round2-final Python with candidate source paths. No provider call or live-root mutation.

Remaining: typed request decoding, copied UI-message deletion, dead replay fallback deletion and all meaningful caller/test migration; queue/cursor freshness and copied converted-history/native acceptance. These paired drafts are not complete or ready to install. Parent266 owns startup preparation/readiness; this batch changes only backend MCP receipt parsing and preserves the parent startup surface.

## TR0 sync and turn caller closure

Synced core main fc5dd38a (268) and Toad integration e6c5227 (117 merged into round2-l0a-callers, not main). Preserved ViewportPresentation windows/anchors and shared per-class ratchet. Removed duplicate Toad TurnStarted/TurnSettled classes; actual turn facts travel in CommsUpdated with immutable local sequence/session/agent envelope. Migrated all source and pilot turn constructors. Remaining historical pilot protocols still need the separate snapshot/compaction/MCP migration noted above.

Built both candidate wheels into owned .artifacts/paired-installed. Dependency fallback points at installed final runtime site-packages; candidate packages are loaded from their isolated wheel installation, with no candidate source override. Actual mounted producer/consumer turn+compaction pilot and ACP validator isolation/order/retirement pilot both passed. Initial guard attempt used a tooling venv without the new agent-comms-ratchet executable; installed environment resolves that. Parent266/267 startup work is not duplicated.

Installed focused family + shared ratchet regression tests: 20 passed, 11.76s. One pytest configuration warning: asyncio plugin not installed for these synchronous tests.
