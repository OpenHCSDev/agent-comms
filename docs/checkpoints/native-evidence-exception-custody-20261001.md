# Native evidence resource exception ownership — 2026-10-01

Owner Arendt. Receiving base main1f8bf0ef; urgent follow-up to merged493,
independent of unfinished489 and pending494. No live input replay, restart,
state clear or public mutation.

Six actual sequence319 diagnostics show successful native triage reaching the
ignored-result cursor projection. Original nonblocking wire/bus lock acquisition
raises BlockingIOError. ExitStack throws that consumer exception back through
PrivateEvidenceRead.open, whose acquisition translation incorrectly spans yield.
It becomes NativePiUnavailable, bypassing NativeSourceCursor.advance's existing
bounded contention policy. This is not evidence of a model outage or a missing
native file.

Source ownership first: existing PrivateEvidenceRead owns file acquisition and
file I/O errors; caller lock and settlement errors remain with their original
owners. Acquired NativeEvidenceRead/Scope retains bytes only and must close while
propagating a consumer exception unchanged. Inventory similar resource-yield
translation scopes and close their consumer paths in the same source change.
Do not add retry deadlines, proof/state copies or disable reader borrowing.

Validation LAST: focused resource exception checks plus an installed continuous
private journey with at least three actual native owners simultaneously finishing
triage, cursor advancement and bus publication. Provider only may be controlled.
No acceptance result claimed at this work-start checkpoint.

## Source closure

Existing owners, no new type/store/policy: PrivateEvidenceRead now translates only
its open/fstat/read/parse/snapshot operations. Its yielded rows and open resource
propagate caller exceptions unchanged while closing. NativeContextJournal's
indexed acquisition and post-observation inode check likewise own only their
operations, retaining query-only/schema/private-file/inode checks. All public
readers continue through NativeEntry/NativeEvidenceRead/Scope; their ExitStack
closes descriptors but does not reinterpret consumer bus errors.

Bounded authored source AST inventory found translating yield scopes also in
NativeTranscript.tail and BusPageIndex.iterate: tolerant decode/index failure is
now scoped to iterator advancement/decoding; yielded consumer exceptions stay
consumer exceptions. Their original display tolerance and malformed-index refusal
remain. GoalHistory._transaction deliberately owns its private write transaction
and commit/fsync uncertainty (only internal goal SQL callers); backend.run owns
turn-level completion/failure events. CompactionJournal/CoordinationSession and
NativeCustody catch BaseException only for rollback/cleanup and rethrow unchanged.
Those are legitimate operation/cleanup owners, not native-file classification.

Unchanged NativeSourceCursor.advance already retries only BlockingIOError from
its original auxiliary cursor transaction, bounded by its existing two-second
policy. This change restores that declared behavior; no new retry/timeout,
provider input replay, acquisition bypass, cursor mirror or fleet reduction.
Storage/wire/native formats and current private-file/prefix proof checks unchanged.

## Representative-history contention relation

The first three-owner installed gate02 passed: three simultaneous real native
triages and three input-backed cursors,21 ACP updates,0diagnostics,22.73seconds,
all worker cleanup complete. This small-history result is scoped, not sufficient
for large-history contention.

Owner review identified that the original cursor clock began before potentially
seconds-long initial decoding. Advance now acquires one existing NativeEvidenceScope,
observes the original source, and starts its unchanged two-second contention budget
only on the first original BlockingIOError. Retries borrow the same acquired reader;
every observation still verifies original byte-prefix and original SQL proof/current
owner/generation. No new timeout/cursor/proof permission, provider operation or
input is retried. The first nonblocking probe without an original committed input
still refuses. File/decode/mutation failures remain distinct and are never retried.
Representative saved history plus real publication contention is the remaining
installed acceptance; no live originals are used as inputs.
