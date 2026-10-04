# Original input settlement observation

Base fa222810dbff56e221433a7a7d9f23aed1a08102, after accepted 614.
Mendel owns InputDocument/InputDispositions retirement and complete callers.
Same persistent checkout. No new native/environment/provider or public effect.

## Owner, storage and lifetime

InputAttempt.finish_unbound owns each original member's transition: ReservedInput
becomes NotSentInput, preserving its original source and attention/retry notice.
BoundUnknownInput/StartedInput/NotSentInput/MissingInput do not transition. A known
never-sent input is not native history; an uncertain binding is never reclassified
or replayed. The existing row hooks remain unchanged.

InputDocument now owns composition of those original member transitions.
InputDispositions uses the existing LockedStore.update publication lifetime and
returns its exact changed document. The old nonlocal changed flag is deleted;
this internal retirement API returns source, not a competing state boolean.
OwnedTurn cancellation observes shared_state on that exact published result,
removing its second full document read after settlement. The same callback serves
pre-native acquisition rollback and native failure/cancellation. The original
wire cut and joined worker/cancellation/resource lifetimes remain intact.

Production callers: record/reserve_turn register settlement before durable
publication; InputDrain.finish_original_inputs joins the same retirement;
OwnedTurn settles before cancellation/failure observation. InputDrain also removes
its original_sources binding on the loop that created it, passing only the
original immutable keys into the SQL/document worker. The prior worker-side pop
is deleted. A missing live original resource requires no document/lock operation;
all following/queue/admission cleanup still runs in the original finally block. Rollback callbacks
ignore the returned value. ACP errors separately read current notice/started
state at error publication: that is a different observation and stays fresh.
Compaction/source selection, native bind/start grant checks and recovery all keep
their original current reads. No stored schema, codec, membership, input key,
lease, routing, native acknowledgement or uncertainty disposition changes.

All former bool-return callers are tests of the actual terminal row: migrate them
to NotSent/unchanged-bound/started observations, not truthiness of a document.
Existing tests-only continued source/recovery fixtures remain at their original
strength; no new native/protocol authority is introduced to improve them.

## Source relation and final affected checks

NRA/refactor-audit Package.load before: src311/tests364/tools53, zero omissions.
All declarations/imports/lexical calls are retained before/after; dynamic aliasing
is a semantic reading limitation, not a zero-consumer claim. Full closure covers
InputDocument, InputDispositions, OwnedTurn, rollback callbacks, InputDrain,
subscriber error/cancel projection and every direct fixture consumer.

BOUND-1/BOUND-2 and IMPL-12: keep original transition hooks and consume the original
publication result, rather than decode/reselect a second time. No new class,
mirror, cache, state label, alternate codec, timing owner, timeout or provider.

After the complete source/consumer batch: one bounded installed batch on a named
released existing holder, using original document transitions and actual saved
SDK/native owner cancellation/rollback. Each control detects either uncertainty
misclassification, mutation/partial publication, or wrong cancel feedback/custody.
No unchanged 611/613/614 rerun, provider prompt or 13s/98s performance claim.
