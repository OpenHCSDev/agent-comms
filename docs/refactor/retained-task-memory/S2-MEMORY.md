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

## Four retention classes and lock-in

The user's Paper4b translation supplies the intended retention semantics below.
Its proposition numbering, `D < K`, full signature `Y_T`, and `rec_P` terminology
are user-supplied theoretical references, not independently verified paper
citations in this checkpoint. Do not claim a formal result has been proved by
this plan or the scorer. The practical distinction is between collapsing
irrelevant narrative and deleting a constraint or a real choice distinction.

1. **Constraints:** user corrections, prohibitions, scope limits and valid claims.
   Preserve exact wording and original source references, never a paraphrase.
   Accepted constraints intersect the applicable valid-action set. Removal can
   enlarge that set and admit invalid actions. Preserve author, authority, scope
   and correction/supersession lineage; retain superseded evidence without
   presenting it as active. A compactor cannot resolve conflicting constraints.
2. **Decisions among alternatives:** preserve the chosen alternative together
   with the valid but rejected alternatives, scope and original source turn.
   Spend this extra budget on genuine choice-resolving items, not every transcript
   statement. Later tasks can need a distinction the current goal did not need.
   Do not infer rejected alternatives or claim a complete signature when the
   bounded admissible alternative set was not actually declared.
3. **Forced facts:** preserve exact references for artifact identities, paths,
   hashes, symbols and unresolved failures when their value identifies the fact.
   If a path itself was selected among valid alternatives, preserve that choice
   under class 2 as well; spelling alone does not establish it was forced.
4. **Narrative:** use the lossy summary for contextual material around classes
   1-3. Narrative is neither their authority nor a substitute when budget runs out.

These are behavioral ownership obligations, not authorization for a repeated
string-kind switch or independently maintained kind registry (MEMB-1, IMPL-5).
Admit the projection/selection contracts and all consumers before implementing
retention cases. If exact mandatory material exceeds the budget, refuse or change
packing/strategy through the existing policy owner; do not silently summarize it.

### Original message provenance, not transcript reconstruction

Prompt-injected peer messages also appear in native history. Retention must read
the original message/wire owner, keyed by original message identity, rather than
extract another authoritative copy from that injected transcript. Preserve source
revision, author, recipient and scope. At refreshed main `4295d680`, Message
provides its original seq/ID reference; WireLog reads those references under its
existing certificate lifetime. ClaimTransition binds claims/releases to that
original message and ClaimProjection derives current claim ownership from wire
rows. Do not introduce a separate mutable claims store. The join to retained
constraints and their admission remains OPEN; a native transcript event ID is not
interchangeable with a bus message ID (IDEN-5, BOUND-2).

Exact retention does not promote every peer sentence into an instruction. Existing
authority, trust, claim validity and scope rules determine applicability. A thread's
summarizer cannot rewrite another author's constraint, adjudicate their claim,
clear an unresolved delivery or elevate peer text above user instructions. Only
an explicit authorized source correction changes the effective retained value.

### Declared Decision owner

A decision currently represented only as prose cannot be reconstructed losslessly
by a compactor. Plan a nominal `Decision` record emitted through an explicit tool
when the agent resolves an ambiguity: choice, valid rejected alternatives, scope
and original source turn, with stable identity and correction lineage. The agent
emits the record at decision time, not during summarization. The bounded alternative
set and emission authority must be admitted; an incomplete record cannot certify
full `Y_T` retention. Audit any existing richer decision authority before adding it.

There must be one canonical Decision owner, integrated with the existing durable
owner/ledger and declared tool/FieldCodec capabilities. A separate task-memory
store, mirrored transcript extractor, parallel codec or second commit pipeline is
forbidden. Retention, packing, UI and evaluation only project that owner's facts.
The storage location, tool admission, native-turn/message join, update authority
and all-callers closure are production design obligations, not implemented here.
Decision emission and later correction must not authorize execution or input replay.
`PiCompactionDecision` is an existing settings/threshold observation, not a record
of an agent's chosen/rejected task alternatives; do not overload it into a second
purpose because its name contains Decision.

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
Run S4's oracle separately on authoritative projection and model recall. Add
verbatim-constraint and source-message identity checks, Decision emission and
supersession checks, zero unauthorized revision mass, and held-out probes needing
previously irrelevant alternatives. These new gates are planned, not supplied by
the existing seven-question scaffold. Same
history/questions/models for controls. Stored facts passing is not a model pass.
Done means all intended consumers derive from the accepted source owners, guarded
invalidation is complete, and native plus repeated recall gates pass. No owner is
assigned by this draft; confirm source contract before S1's shared changes.
