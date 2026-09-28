# R3 paired caller contract (complete source contract)

Production Toad ACP `inputDisposition` / `input_dispositions` payloads remain unchanged. Current source has four core-facing test consumers in parent's `toad-export-caller-migration-20260928` tree:

- `input_delivery_owner_pilot.py`: `AcpDeliveryCursors(comms.root / AcpDeliveryCursors.filename)`; initialize returns typed `DeliveryCursor`, with `.cursor` and `.legacy_through`.
- `native_input_attribution_pilot.py`: `InputDispositions(comms.root / InputDispositions.filename)`; record/bind remain store mutations.
- `queue_view_backend_pilot.py`: `producer.inputs.dispositions.read().rows["acp:" + id]`; `.unresolved` is UNKNOWN, its negation STARTED; `.declared_name` only when asserting external spelling. No `.get`/`.status` store aliases remain.
- `current_delivery_owner_pilot.py`: core CommsAgent fixture uses `owner.inputs.dispositions` and `owner.inputs.backend_inboxes`, not retired ACP `_dispositions`/`_backend_inboxes`. `before = store.read().rows`; row fields are attributes. Notice-only unchanged-evidence assertion is `replace(after_row, notice_dismissed=before_row.notice_dismissed) == before_row`. Do not convert actual public UI dictionaries to attributes.

Store snapshots are `InputDocument` / `DeliveryDocument`; `.rows` holds declared records. Queue receipts are `InputAttempt` values, still requiring the original process-local `QueuedInput` permit. No reconstruction from documents.

CompactionJournal.begin now explicitly receives `inputs: InputDocument` from the caller's existing retained disposition lock (OwnerCompactionCommit production caller). Independently entered journal readers take input shared lock before SQLite, avoiding the newly discovered read-lock reacquisition under compaction's exclusive boundary. No journal-to-input lock edge remains in those paths.

`transcripts-consumer.patch` is confined to `Transcripts.repair_input_routing` and typed receipt attributes. It does not touch R1 `_events`/ToolDiff/PiToolResult conversion. Parent can combine those disjoint methods directly.
