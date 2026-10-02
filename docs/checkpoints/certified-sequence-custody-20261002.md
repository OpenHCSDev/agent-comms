# Certified sequence custody

Owner: Arendt. Source base: current main 5395eb6a. This checkpoint is independent
of unfinished Native6/session-custody work in #489.

## Original relation and observed contention

The canonical bus lock enters `WireLog.verify_before_read_unlocked`, whose
durability owner returns the acquired `CertifiedSourceRead`. Its original
`PrefixWitness.through_seq` describes the committed, validated source cut.
`WireMetadata.last_seq` alone may reserve a sequence before an append; it is not
a substitute for committed evidence on a claim bus.

The live installed Toad process was observed holding `.bus.jsonl.lock` exclusively
inside `HistoryReadRequest.read -> WireLog.latest_sequence ->
_max_sequence_unlocked -> verified_records_unlocked -> WireRecord/FieldCodec`.
Another thread in that same process waited in `_record_snapshot`, and owner
processes were independently observed in `locks_lock_inode_wait`. Sampling used
nonblocking py-spy, without prompts, provider calls or process interruption.
This identifies an actual competing read algorithm under the admission lock;
it does not allocate all historical public327 delay to this operation.

## Full caller closure

`latest_sequence` and `_record_snapshot` must consume the original acquired
committed cut through one `WireLog` algorithm. The changed decision applies to:

- Toad `HistoryReadRequest.read` channel preparation;
- `BusPresentation` page watermarks and `HistoryViews` original snapshots;
- `Transcripts` and `message_by_id` full-history snapshots;
- `ThreadManagement` registration/fork read floors;
- `GoalActions` original dependency watermark.

No caller changes its interpretation, source selection, audience, routing or
input disposition. The original strict scan remains necessary for a genuine
claim bus without an installed checkpoint; ordinary unclaimed markers retain
their existing sequence semantics. There is no new certificate, sequence cache,
index, shared-lock policy, runtime format, or alternate lifecycle authority.

Implementation and end validation are pending. Native5 and original UNKNOWN
inputs remain unchanged; no public action is authorized for this worker.
