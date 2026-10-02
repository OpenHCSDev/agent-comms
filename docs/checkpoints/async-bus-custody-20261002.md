# Async bus read and publication custody

Owner: Arendt. Separate continuation of the useful #508 sequence checkpoint.
The matching installed #508 channel-click journey passes; this draft does not
claim the remaining cross-owner bus concurrency is fixed.

## Existing owner search and required relation

The physical owner is `StoreLock`, acquired by `_store_lock`; bounded physical
wait is already owned by `StoreLockContention`. `WireLog` owns ordered append,
marker reservation and certification. `CertifiedSourceRead` borrows its opened
file and SQLite connection from that one barrier. `PrefixWitness` describes the
original sealed prefix; `FinalSeal`/`PendingSeal` own current/recovery relations.
`DeliverySources` owns exact original pointers and validates frozen rows, not a
second message store. None of these facts needs a replacement family or cache.

Two existing read lifetimes differ. `_record_snapshot` opens a fixed inode and
byte boundary and releases the physical writer lock before public decoding.
Conversely `conversation_sources`, reference windows, addressed pages and exact
cohort resolution decode original typed payloads inside the certified lock.
`certified_read` cannot simply become a shared lock: its acquisition guard can
complete/recover an original writer checkpoint. Publication retains the original
wire -> bus -> registry -> coordinator order; no provider await belongs there.

The source census includes history/sidebar/notification, transcript routing,
source coverage, goal waits, cohort foreground, original input and response
publication consumers. Current-fence consumers at actual admission/publication
remain distinct from display consumers of an immutable bounded original cut.
Read snapshots grant no input, cursor, replay or owner authority.

## Implementation trajectory

Extend the original resource owners so bounded original bytes/pointers are
captured once under certification, and display decoding happens after release.
Keep exact root/seq/id/audience checks with `DeliverySources`; delete replaced
locked decode paths across every affected reader. Current proof consumers retain
freshness at their actual write boundary. Reuse `StoreLock` acquisition/custody
for async wait; do not block owner loops with flock/sleep, move coordinator
connections across threads, or introduce a second lock/admission algorithm.

Mendel owns the disjoint Toad caller census and migrations only if the original
Core API needs an explicit change. Existing readers already run in worker
threads; no speculative frontend wrapper, snapshot cache or high-water copy is
required. Singer owns NativeSourceCursor producer/consumer closure separately;
Einstein owns compaction preparation hooks.

Order: semantic source/caller closure, coherent implementation and deletion,
then one batched affected sanity and actual configured multi-owner/publication
and isolated UI read qualification. Original UNKNOWN/native inputs and durable
bus/session bytes are protected. No public prompt/replay/restart is authorized to
this worker. Parent owns release publication.

## Working code checkpoint — bounded read capture

`CertifiedSourceRead.capture_deliveries` captures original sealed pointers and
bounded raw row bytes while the certificate is held. Its returned decoder owns
only those immutable bytes/pointers and the original root ID: no live stream,
SQL connection, current marker, owner status or admission authority survives.
`DeliverySources.read_bytes`/`decode_bytes` retain the single original seq/id and
frozen sender/audience validation algorithm; the ordinary locked delivery path
uses these same methods. No new class, store or wire format was introduced.

`WireLog.conversation_sources` and `deliveries_for_references` now finish
certification and release their publication lock before consuming the captured
decoder. `SourceCoverage._page` captures its original page witness, addressed
high-water and marker floor under the same certificate; it decodes after leaving
the bus lock. Whole-prefix/UNKNOWN and final cursor publication checks remain
with their existing owners. `TranscriptRoutes` and `TaskSources` consume the
same API *inside their actual write custody*, preserving original publication
freshness. Deleted free algorithms `delivery_references_unlocked` and
`conversation_sources_unlocked`, including every production/test import.

This is a published implementation checkpoint, not Ready. Exact remaining
relations: physical acquisition still needs the shared async wait algorithm;
`full_history`/`total_messages` and context-manifest reads still retain a writer
lock while decoding; goal-wait addressed iteration needs bounded snapshot
iteration; exact keyed response lookup still resolves under its publication
transaction. Fresh source/prewrite and publication/cursor transactions must
consume the original current fence, not a display iterator. Source/admission
scheduler callsites must use async acquisition without moving live coordinator
connections across threads. These are owned remaining closure, not hidden
compatibility fallbacks. No validation or provider journey has been claimed for
this checkpoint; validation follows the coherent source implementation.

## Working code checkpoint — physical async acquisition

The existing `Platform` family now owns its irreducible native file-lock attempt
and release: inherited POSIX last-close custody and original Windows byte unlock.
`StoreLockContention.waits` is the one bounded physical wait algorithm. Its
synchronous driver sleeps; its async driver yields to the owner event loop.
Untimed synchronous POSIX callers keep their original kernel-blocking acquisition,
so this change does not introduce polling into synchronous legacy callsites.
No new OS family, state store, timeout, provider retry or lock registry exists.

`_store_lock` and `_async_store_lock` share `_store_lock_file` descriptor custody
and `_held_store_source` durability/refusal/release behavior. Cancellation while
an async acquisition waits closes the unacquired descriptor; it cannot leave a
background lock-acquisition thread or a later stray write. Acquired guard/consumer
failure closes the same original resource. TrackedTurnSession.send now awaits
maintenance-wire acquisition before the original capability write. Its final
pipe-drain watchdog and irreversible prompt-writer boundary remain unchanged.

This does not yet claim all async owner callsites are migrated. Certification
itself can recover a damaged prefix and still runs synchronously inside custody;
that operation must remain one guarded owner transaction, with async callsites
borrowing capture work through a complete owned operation rather than exporting
its SQLite connection to a thread. Remaining reader/publication relations above
are still explicit closure obligations. No tests or installed journey have run
for this unfinished source checkpoint.

## Source error-family closure

The source trace confirms `CommittedDelivery.from_wire` does **not** normalize
raw/FieldCodec failures: duplicate JSON keys, malformed JSON, Unicode decoding,
message/private-field shape and frozen policy checks raise ValueError/TypeError.
A non-object JSON root otherwise reaches mapping operations before those checks.
`DeliverySources.decode_bytes` now owns that boundary once: require an object,
then call the original committed decoder, preserve existing RelationViolationError,
and translate malformed source ValueError/TypeError to RelationViolationError
with the original cause. Its locked `delivery` and every captured iterator call
this same method. `read_bytes` similarly translates only its own seek/read
OSError; it does not catch consumer exceptions. No per-caller catches or generic
error suppression was added, and input/UNKNOWN disposition is unchanged.

## Working code checkpoint — complete read stream lifetime

`_opened_wire_snapshot` factors the original fixed inode/byte-boundary acquisition
and closes its resources through ExitStack. Public page accounting and strict
`verified_snapshot` consume that same opened resource after publication custody
ends. `full_history` and `total_messages` now use the original WireScan outside
the publication lock; uncertified original streams keep strict parsing instead
of a generic fallback. `context_manifests` captures source and registry rename
membership under the original maintenance-wire ordering, then releases both
physical locks before strict decoding. The registry observation is a read
snapshot, not a competing current identity/admission authority.

New incoming consumers from #503/#507 are explicit remaining closure:
`SelectedParticipant.sources` is a plural certified acquisition but still
invokes locked delivery decoding; `CursorPublication.refresh` is async yet
calls synchronous NativeSourceCursor.advance. The latter needs a complete
operation-owned async resource path, not a consumer-only thread wrapper or a
connection transferred across threads. Singer keeps cursor proof/participant
behavior; this draft owns its acquisition/scheduling API. These unfinished
relations keep the draft non-Ready.

## Incoming 503/507 closure — owned async cursor operation

Normal main integration includes the qualified plural-source and cursor owners
from #503/#507. Kepler owns migration of `SelectedParticipant.sources`/selection
to the bounded captured originals and async resource contract; no competing
scalar selector or copied assignment state is being added.

The existing `Coordination` owner now lends a complete `run_async` operation.
Its worker opens and closes a connection to the same canonical database; only
the operation result returns. This is resource callback custody, like the
existing LockedStore update callback, not a selectable lifecycle policy. The
existing `join_retirement` accepts its executor Future and retains acquired
operation custody through cancellation until the connection has closed. No
SQLite connection is transferred, no background task can later publish after
the caller releases custody, and no new store or executor registry is created.

`NativeSourceCursor` owns async read/advance/refresh operations using that
resource. Each invokes the existing read/advance algorithm and its original
CursorOwner, prefix, participant, native receipt and final publication fences.
Refresh derives the current registry admission and participant inside that
same complete operation; it cannot resume an input. No proof predicate or
2-second auxiliary physical wait policy changed.

All ACP consumers await the original operation: CursorPublication observation
and refresh, no-selected-input drain projection, and CoordinatedTurn capture
for completed/ignored/rejected outcomes. Trusted metadata is now async, with
all session new/load/rename, subscribe and identity publication consumers
migrated together. Existing test callers have matching async signatures; no
tests have run during this source pass. CursorDelivery stays on the connection
event loop and publishes the original observation, never a worker copy of its
ordering/bookkeeping. The small outer registry scope checks remain explicit
remaining event-loop acquisition work, not a claim of total async closure.

This checkpoint removes three consumer-built cursor acquisition lifetimes;
the cursor operation owner supplies them once. Remaining relations are the
plural selection contribution, bounded goal/foreground addressed iteration,
full async durability acquisition and current keyed-publication lookup. Final
validation stays last after their coherent closure. Native5/durable formats,
original UNKNOWN inputs and all native history remain unchanged.

## Working code checkpoint — addressed original read windows

Deleted `CertifiedSourceRead.addressed_deliveries` and every caller. Existing
`WireLog.addressed_sources` now traverses the original AddressedPage resource
in its existing bounded page size. The first certificate's PrefixWitness owns
the complete read cut; subsequent pages cannot include later appends or change
its root/opened inode. `PrefixWitness.require_read_window` owns that source and
monotonic-prefix relation. The page's pointers/raw bytes are captured under
certification, then the original DeliverySources decoder and all consumer
predicates run after bus custody closes. No new read cursor, high-water store,
page cache, source identity or family was introduced.

All three former addressed-iteration consumers migrate together: GoalWait
reply observation, GoalManagement input-review projection, and foreground/ACP
visible-original discovery. Existing foreground acceptance still reopens the
canonical current source at `accept_delivery_cohort` before its SQL write;
the read window cannot grant acceptance or native admission. Actual goal wait
consumption retains its original current write fence. Its enclosing goal
command's maintenance custody is still an explicit remaining relation when
decoding is part of a write predicate; this draft does not claim all physical
lock lifetimes have been eliminated.

Patterns: IMPL-10/IMPL-14 resource lifetime belongs to the existing acquired
owner; BOUND-1 external original bytes have one decoder; IDEN-1 the committed
prefix and addressed initial high-water are distinct facts. The new-case
maintenance relation is source based: an external malformed row is translated
once by DeliverySources, and an append can only extend the current seal while
the selected original window stays bounded. Neither case needs a caller guard.
Source syntax and replaced-reference searches are complete; batched behavioral
and configured installed validation remain last, after remaining source closure.

## Current publication source lookup

The original PublicationIntents declaration already validates its expected
message ID through Message authority. Keyed receipt lookup now uses that
existing ID to select original DeliverySources pointers instead of decoding
every unrelated history row in reverse under the publication transaction.
The sealed ResponseKeys row still proves presence versus absence; the actual
original receipt must still match key, execution and complete typed intent.
Message ID uniqueness is not assumed, and a present key without a matching
original source still refuses publication rather than returning absence or
authorizing another append. SQL may still scan pointer rows: this is not a
constant-time or new-index claim. No schema, seal, source format or write-fence
change is needed, and every keyed-publication consumer keeps the same owner.

## Owner/caller check and working read/publication closure

Read OpenHCS PR60 commit `5e8812ee83d0dc8714392445bad3e32fc47a1755`,
including its root parsing, member annotations, imports, inherited declarations,
and removed-facade checks. Reused the existing refactor-audit `Package` and
`ParsedModule` parser for a scoped before/after AST query, rather than copying
its helpers or adding a guard framework. The output is
`evidence/async-bus-custody-20261002/owner-consumers-before-after.json`:
719 tracked Core files across `src/agent_comms`, `tests`, `tools` parsed in each
snapshot; 275 Toad dependency source files parsed; zero parse omissions.
Eighteen selected existing owner definitions are unique in those Core roots.
Members, annotations, bases, self/cls reads and writes, imports, and matching
references are retained. The dependency checkout/head is recorded. These are
lexical candidates, not proof of dynamic receiver resolution or runtime MRO.
Untracked fixtures, shell/JS generated commands and installed package bytes are
outside this Python census; the source receipt does not call them absent.

The existing `WireRecord` ancestor now owns `delivery_messages`; the existing
`CommittedDelivery` member supplies its original frozen sender lookup. Deleted
`DeliveryMessage.from_wire`'s external choice between deliveries and messages.
Inbox reads consume the original strict opened stream after publication custody
closes. Claim reads and retained-context export likewise decode after release;
their actual write counterparts still consume the current protected source.
The strict scan, claim algorithm and retained-fact/digest algorithm each have
one implementation shared by their current-write and opened-read consumers.
No snapshot grants admission, cursor progress, native proof or replay.

The existing `LiveResponseOwner` owns the complete asynchronous multi-route
publication operation. `SelectedAttempt` supplies only its original owner/fence
and decoded proposals; the acquired operation owns its coordinator connection,
prepares every route before the first append, verifies every published receipt,
and consumes original dependency waits before closing or propagating cancellation.
Deleted the caller's two publication loops and its separate post-publication
wait settlement. `Coordination.run_async` and `join_retirement` are the acquired
resource mechanism; no connection, participant or active transaction escapes.
Patterns: BOUND-2, IMPL-5, IMPL-10. This is code-bearing source progress, not Ready.

### Exact remaining lock scopes

- `MessageBus._pending_projection` still decodes its disposable route projection
  under the bus barrier; `channel_activity`/`last_sent_timestamps` likewise retain
  it during append-index decoding. Those existing projection owners must borrow
  an opened source cut rather than make a second authority.
- Certification can repair an original checkpoint. Its guarded write must stay
  exclusive; ordinary borrowed snapshots cannot simply flip `shared=True`.
- `PrivateSendAdmission._exclusion` currently borrows `_response_boundary`, then
  acquires coordinator EXCLUSIVE and original prompt/journal exclusions. Those
  global locks survive the raw pipe write. No model-stream await is inside that
  scope, but pipe backpressure serializes unrelated admissions. Narrowing requires
  the original stop/rename/maintenance/revoke consumers and UNKNOWN settlement,
  not an async wrapper or removal of a fence. This remains owned #509 work.

Validation follows the complete related source migration. No new provider,
public input, restart or installed-readiness claim is made by this checkpoint.
