# Scoped drain and cursor owner closure

Production checkpoint d0eae668af04fa8782ce7b28ab483d554b9fbf3e removes
72 lines and adds138 in seven files, against main f34cd8ea. No schema or native
bundle change. Parent reviewed that complete production delta.

InputDrain's existing scheduling hint observes its original RegistryOwner,
ParticipantSnapshot/pointer, next sealed pending assignment and durable cursor.
Other owners' registry/SQL activity does not invalidate it. The original committed
message cut, source inode, admission floor and coordinator replacement still do.
Empty/unavailable cursor recovery is retained; this is not delivery authority.

PendingNotification membership already owns runnable assignment selection. The
shared sealed query derives its SQL selection from those declarations before row
decoding. It no longer decodes settled history to rediscover that same membership.

CursorOwner owns exact returned-row validation using the same operation's original
ProvenSourceCoverage. A refused advancement must validate its prior row; unchanged
SQL bounds return that exact row. ACP and CursorPublication consume this verified
refresh result once instead of advancing then rereading history. Their original
owner/admission, source, SQL and publication-order fences remain.

## Affected controls

scope-controls.log: seven passed in3.27s. Actual private bus/registry/SQL controls
cover quiescence, unrelated owner writes, own task/recovery changes, original
append during a scan, newly sealed pending work, runtime settings, exposed-file
mode repair and replaced-owner refusal under cursor contention. No native or
provider execution is mocked or authorized by these controls.

The first combined batch was5 passed/5 failed in4.57s: one old empty-bus test
assumed an absent cursor never needs refresh; four existing native fake helpers
still demand removed session_dir arguments. Those fakes were not adapted, and
production compatibility was not added. The changed idle controls now use a real
absent-audience initial and actual coverage-only cursor. Two subsequent fixture
mistakes are retained in invalid-self-recipient-control.log and
unsealed-source-control.log: self-DM is invalid; a published cohort is not yet a
sealed assignment. The final control uses the original acceptance owner.

## Original public469

Original native input identities join the actual request diagnostic stream in
original469-inputs.json and original469-requests.json. The boundaries source has a
managed compaction entry5ff8839b committed at+144.264819s, corroborated by its
original ManualCommittedSummary reservation90d275e581b24c96b2d969ce144ebba7
and CommittedOperatione41a3c9f6c4c40739d0447786f0c8bcd. This is a running-source
read-only observation, not stopped carry qualification.

TRIAGE preparing+146.666s to finished+150.067s; FULL preparing+154.201s to
finished+158.105s. Thus its relevance/answer requests span7.305s; the major delay
precedes them on compaction preparation. The existing selected-summary RPC emits
compaction progress but does not retain the normal model request dispatch/first
event clock. These observations do not divide that earlier delay between summary
provider and local preparation, or claim this drain batch fixes it.

Parent's one next configured public channel wave is the user latency acceptance.
This change claims removal of repeated owner/history work, not146s compaction
latency closure. Original public inputs, UNKNOWN, proof bytes and dispositions are
unchanged. Catalog: IDEN-7, IMPL-12, BOUND-2.
