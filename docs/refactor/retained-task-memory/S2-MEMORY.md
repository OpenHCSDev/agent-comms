# S2: Exact retained task facts

**Head audited:** `697bba42f5f03e169ff8eae9490090cbd0d0b89e`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 2. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* none. *Uses:* existing fact stores, source/witness, native packing and commit.

## Gap and source witnesses

**There is no wired exact-fact retention projection corresponding to the historical policy.**
Dormant RetentionPolicy accepts supplied facts/tombstones and provenance; it does
not obtain them from real stores. `HistorySummarySource` serializes conversation
and previous summary. Goals, attempts, inputs and native history already have
owners; retaining them in another mutable memory store would create a replica.

## Required questions and proposed relation

For each preserved fact: what is its identity, exact current value, canonical
source reference/revision, allowed lifetime, and invalidation rule? Distinguish
canonical availability, presence in the model prompt, and actual recall.

Provisional required: current Goal/revision -> retained goal view; current
InputDispositions row -> retained disposition; current user correction/source
receipt -> exact correction and symbol/path/revision view; failure owner ->
unresolved-failure view; native witness -> checkpoint source equality. User task
and original PR48 authorize investigating these relations, not inventing their
canonical sources or automatic selection heuristics.

Forbidden: summary text -> goal completion, UNKNOWN clearance, input replay or
claim mutation; prior correction -> current export root after supersession;
renamed/deleted fact -> active retained fact after invalidation. A source reference
must identify the same fact, not just a similar spelling (IDEN-5, BOUND-2).

## Candidate owner and counterevidence

Project facts from their existing authorities, carrying revisions and evidence.
Extend native preparation/packing and the existing summary payload/commit so facts
and narrative form one checkpoint. Preserve tool pairs and recent window. UI is a
derived view; no separate text injector, authoritative memory database or codec.
Derived lookup/storage can be an optimization only when its owner/rebuild and
invalidation are proved, not an independently writable authority (TIME-9).

Goals and input disposition are traced. Canonical user-correction, symbol receipt
and unresolved-failure projection APIs remain OPEN: transcript statements cannot
be promoted blindly to structured authority. Bounded selection when facts exceed
the budget, source concurrency, constructors/MRO, dynamic and alternate callers
also require admission. Do not auto-enable a fact extractor to close a checklist.

## New-case experiment, deletions and guards

Add an exact artifact identity. Extend its determining owner's projection once;
packing, summary, commit and presentation consume the contract without new
per-kind rosters (MEMB-1) or dispatch arms (IMPL-5). Count actual edits. Replace
and delete the dormant RetentionPolicy contract/tests when the native path owns
that relation. Guard against writes to canonical goals/inputs from retention and
against a second checkpoint/commit path.

## Tests and done when

Three real native checkpoints: preserve exact identity, correction spanning
segments, rename, deletion, goal replacement, unresolved failure and queued input.
Prove stale source refuses preparation/commit; unknown input is unchanged; exact
facts fit with narrative and tool pairs; cancellation never commits partial data.
Run S4's oracle separately on authoritative projection and model recall. Same
history/questions/models for controls. Stored facts passing is not a model pass.
Done means all intended consumers derive from the accepted source owners, guarded
invalidation is complete, and native plus repeated recall gates pass. No owner is
assigned by this draft; confirm source contract before S1's shared changes.
