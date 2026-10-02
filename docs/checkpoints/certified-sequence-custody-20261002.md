# Certified sequence custody

Owner: Arendt. Source base: current main 5395eb6a. This checkpoint is independent
of unfinished Native6/session-custody work in #489.

## Original relation and observed contention

The canonical bus lock enters `WireLog.verify_before_read_unlocked`, whose
durability owner returns the acquired `CertifiedSourceRead`. Its original
`PrefixWitness.through_seq` describes the committed, validated global source cut.
Its `latest_source_seq` is an addressed-page projection; silent observations do
not allocate a sequence. Neither is a second sequence allocator.
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

## Implemented checkpoint and qualification

Functional head `74877f2dd108ed211871a098064178eaf3a4fdb5` normally merges main
including #505. Two production files change: 32 additions, 17 deletions; no new
type, API format, store or identity. `_max_sequence_unlocked` and both certified
full traversals are deleted. `WireLog` owns the one sequence-consumer algorithm;
the acquired `CertifiedSourceRead.committed_sequence` owns validation of its
original sealed bytes. Full freshness consumers still use `require_current`,
including its exact marker relation.

The committed-prefix method deliberately differs from current-marker freshness:
a failed append can reserve a marker sequence without committing it. The source
owner factors the existing final-seal/inode/revision/resource checks into
`require_open_prefix`; the original acquisition still verifies root identity and
the upper bound against the marker. Thus that case returns the prior committed
sequence and cannot turn a reservation into delivered history.

The AST reference inventory and sanitized live stack are under
`evidence/certified-sequence-custody-20261002`. Attribute references are a census,
not automatic proof of dynamic dispatch; all listed sequence consumers were
reviewed against their original source contracts.

End sanity: the initial selected batch passed 42 controls and failed 11. Ten
failures reproduce unchanged installed main (obsolete response fixtures and
reader assertions). The eleventh exposed the reservation/freshness distinction
and was fixed at the source owner. Eight affected final controls pass, including
reservation, silent observations, corrupt original prefix and changed source
witness; the newly merged #505 resource control passes on the rebuilt matched
wheel. The intervening stale-wheel failure is retained, not counted as a pass.

Actual normal receiving Toad channel-read qualification remains pending; the
initial Core797 candidate is protected and will not be used to claim this final
checkpoint. Native5 and original UNKNOWN inputs remain unchanged. Full async bus
admission and bounded snapshot lifetime closure are further scope, not fixed by
this sequence checkpoint. No public mutation or restart is performed by this worker.
