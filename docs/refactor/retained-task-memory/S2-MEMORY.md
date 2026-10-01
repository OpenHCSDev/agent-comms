# S2: Exact retained task facts

## Current implementation ownership

Schrodinger is the sole S2 integration owner, assigned 2026-10-01. The persistent
worktree is `/home/ts/wt/comms-retained-task-facts-s2-20261001`, based on reviewed
main `e191bcf44c292dfedc5b8b62b2b503f06de3ae7b`. This scope implements original
wire Decision emission/correction, declaration-owned retained classes, canonical
fact projections and existing native compaction packing/commit closure. Einstein
owns S5 context assembly separately; Mendel owns native execution identity in
#472. Shared methods are coordinated directly before edits. Parent authorizes one combined S2/S5 normal native package and matched installed
cohort, with Schrodinger the sole native builder and Einstein the query acceptance
owner. Parent alone owns paired Toad metadata, installation and public cutover.
Paid recall evaluation is a separate budget; source and native acceptance remain
distinct.

**Source reviewed:** `4295d680`.
**Rules:** [00-RULES.md](00-RULES.md). **Step 2. Origin:** PR48 proposal.
**Shared abstractions** ([02-SHARED-ABSTRACTIONS.md](02-SHARED-ABSTRACTIONS.md)). *Builds:* Decision. *Uses:* existing fact stores, source/witness, native packing and commit.

## Gap and source witnesses

**There is no wired exact-fact retention projection corresponding to the historical policy.**
Dormant RetentionPolicy accepts supplied facts/tombstones and provenance; it does
not obtain them from real stores. `HistorySummarySource` serializes conversation
and previous summary. Goals, attempts, inputs and native history already have
owners; retaining them in another mutable memory store would create a replica.

## Required questions and relation

For each preserved fact: what is its identity, exact current value, canonical
source reference/revision, allowed lifetime, and invalidation rule? Distinguish
canonical availability, presence in the model prompt, and actual recall.

Required: current Goal/revision -> retained goal view; current InputDispositions
row -> retained disposition; current user correction/source receipt -> exact
correction and symbol/path/revision view; failure owner -> unresolved-failure view;
native witness -> checkpoint source equality. PR48 requires source-owned facts
and revision-bound selection.

Forbidden: summary text -> goal completion, UNKNOWN clearance, input replay or
claim mutation; prior correction -> current export root after supersession;
renamed/deleted fact -> active retained fact after invalidation. A source reference
must identify the same fact, not just a similar spelling (IDEN-5, BOUND-2).

## Ownership

Project facts from their existing authorities, carrying revisions and evidence.
Extend native preparation/packing and the existing summary payload/commit so facts
and narrative form one checkpoint. Preserve tool pairs and recent window. UI is a
derived view; no separate text injector, authoritative memory database or codec.
Derived lookup/storage can be an optimization only when its owner/rebuild and
invalidation are proved, not an independently writable authority (TIME-9).

### Source decisions and defaults

| Question | Decision and default |
| --- | --- |
| User correction source | Original user-authored wire Message with seq/ID, exact text and explicit supersedes reference. For direct native input, use the original durable input/native-user entry and its turn identity. Resolve the existing input-to-wire link once when present; never mint a duplicate correction. |
| Constraint applicability | Use original sender role, recipient/project/goal scope and claim generation. User instructions outrank peer instructions. Ambiguous scope retains exact evidence and blocks the affected optional action until clarified; it never grants authority. |
| Symbol/path/hash receipts | Derive from the original completed filesystem/tool result with repository revision, tool-call/result identity and original source turn. Revalidate the referenced source revision before packing/commit. Missing provenance blocks exact-retention acceptance; narrative guesses cannot fill it. |
| Unresolved failures | Read original failed input/attempt/operation diagnostics and their durable IDs through existing failure owners. Keep the failure unresolved until its owner records an explicit resolution; an assistant's reassuring prose cannot resolve it. |
| Facts exceed the budget | Pack all mandatory exact material first; shrink narrative and the policy-owned recent window while preserving outstanding tool pairs. If mandatory material still does not fit, refuse optional compaction and use the existing hard-limit policy's safe refusal rather than silently discard constraints. |
| Concurrent source changes | Capture one existing certified wire/native source boundary; recheck at preparation and commit. A mismatch discards the candidate and leaves original state unchanged. |
| Missing binding, dynamic caller or constructor/MRO effect | Refuse that unsupported projection; resolve the determining declaration and migrate every caller before enabling the surface. |

## Four retention classes and lock-in

Tristan's paper4b supplies the four retention classes below. A summary is an
installed rule: collapsing distinctions can lock in a choice that a later task
needs to revisit. Dropping a constraint enlarges the valid-action set. Preserve
constraints and choice-resolving signatures (`D < K`, `Y_T`); compress narrative.

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
Implement projection/selection contracts and migrate all consumers together.
When mandatory material exceeds budget, follow the source-default table above;
never silently summarize it.

### Original message provenance, not transcript reconstruction

Prompt-injected peer messages also appear in native history. Retention must read
the original message/wire owner, keyed by original message identity, rather than
extract another authoritative copy from that injected transcript. Preserve source
revision, author, recipient and scope. At refreshed main `4295d680`, Message
provides its original seq/ID reference; WireLog reads those references under its
existing certificate lifetime. ClaimTransition binds claims/releases to that
original message and ClaimProjection derives current claim ownership from wire
rows. Join retained constraints by Message.reference through WireLog's certified
reference read and apply the original authority/scope contract. A native transcript
event ID is not interchangeable with a bus message ID (IDEN-5, BOUND-2).

Exact retention does not promote every peer sentence into an instruction. Existing
authority, trust, claim validity and scope rules determine applicability. A thread's
summarizer cannot rewrite another author's constraint, adjudicate their claim,
clear an unresolved delivery or elevate peer text above user instructions. Only
an explicit authorized source correction changes the effective retained value.

### Declared Decision owner

S2 owns a typed `Decision` payload on the original wire Message and an agent tool
`comms_decision` that emits it when resolving an ambiguity. Declare these fields:
chosen alternative, tuple of valid rejected alternatives, scope, author and source
turn. Identity is the enclosing original Message.reference, not a second generated
record ID. Corrections name that reference and retain original lineage.

The tool accepts chosen/rejected alternatives and scope. Existing admitted owner
and turn context supply author and source turn; callers cannot impersonate them.
The original message append commits the Decision and its provenance together.
FieldCodec derives decoding/schema from the typed declaration; existing tools
derive their argument contract from it. No independent Decision ledger, memory
store, mirrored transcript extractor, codec or second commit pipeline.

Default scope is the current project and active goal revision, or the current turn
when no goal exists. Require a nonempty chosen value, nonempty unique valid
rejected alternatives, and chosen absent from rejected. Forced facts use their
source references instead of empty Decision records. The agent emits the bounded
admissible set at choice time; a compactor never invents alternatives. A missing
record fails the choice-retention gate rather than reconstructing prose.

Default update authority is the original author under the same scoped authority,
or an explicit superseding user correction. A correction must name the original
Decision reference. Cross-author agent updates reject. Retention, packing, UI and
S4 derive the effective value from original wire records and correction events.
Emission and correction cannot authorize execution or replay input.
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
previously irrelevant alternatives. S4 owns those gates. Same
history/questions/models for controls. Stored facts passing is not a model pass.
Done means all intended consumers derive from accepted source owners, guarded
invalidation is complete, and native plus repeated recall gates pass. Complete
S2's source contract before S1's shared changes.

## Combined native package checkpoint — 2026-10-01

The original stock recipe, exact 95 locked extension dependencies and canonical
`prepare-pi-native` verification produced the combined S2/S5 package. Manifest
`4519164ef97bb5ece65f98544179e544e0ac0b486d8976023b610f2397d937f8`, full
tree `a2b40e1da14b8db418c4d950c703273942a51ced0021e1280dea9b1625fbcd9f`.
The original full ancestor/layout/tree trust check passes; 49 diagnostic pins
include the matching compaction declaration. Original public package is unchanged.

Affected source: 22 passed in 2.11s on the normally integrated frozen-recipient
reader. Required changed-source debt ratchet against the original base passes
with zero positive deltas. Source-only controls do not establish installed native
checkpoint or recall correctness; those remain pending. General constraints,
artifact/tool provenance, non-selected native retention and recent-window
reduction remain incomplete. See source-checkpoint07.json and
native-canonical-ready04.json; original failed builds and UNKNOWN stay protected.
