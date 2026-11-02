# Supplement to Semantic Completeness for Correct Maintenance: part 2 of 10

Optional reading: the paper1 supplement converted to layout-preserving text, page by page. Figures are PNG files in `figures/`.

its result is forbidden. This is a contract promising outcomes on its accepted inputs; other methods have
their own declared contracts. The model does not mechanize language typechecking or Liskov–Wing’s proof
rules. The explicit Boolean witness permits either result: replacing false with true is valid, but disabling
the operation is not.
    Lean separately proves that if behavioral subtyping preserves promised operations, a child excluding an
operation promised by its proposed supertype cannot be its behavioral subtype. Such requirements call
for changing the subtype relation, for example by splitting a bundled parent into independent roles and
composing only the required roles. The role-normalization and multiple-parent construction already establish
the implementation-supply coordinate; each selected role still has its behavioral contract. The relaxed model
cancels unwanted implementation–class pairs while keeping supertype promises binding. Valid replacement
preserves the contract; promise-blocking violates it. Requiring behavioral admissibility can restrict those
repairs and raise their minimum cost, but cannot lower the exact-supply minimum. Legacy formal names
BlockingRepair, finiteBlockPairs, and BLK identify this set-level removal model; promised operations
remain subject to the behavioral contract.10

Declared property instances. The common contract is parameterized by four inputs: cases X, required
answers a, visible evidence (including an observer projection when one is part of the model), and the admitted
policy states. Table 5 records selected instantiations of this contract. Lean packages these four inputs and
proves that policy-wide answer preservation is equivalent to an answer-preserving resolver at every supported
state.11

                                                                  Visible evidence or
Property face         Cases X             Required answer a                                Admitted policy
                                                                  projection
                      executions or                               trusted boundary         supported versions and con-
Integrity                                 intended result
                      configurations                              state                    figurations
Projection-
                      possible worlds or                          observer projection      declared projection and
limited non-                             protected answer
                      secrets                                     π◦e                      supported worlds
extraction
                      finite trace pre-   whether the bad
Safety                                                            monitor-visible prefix   admitted traces
                      fixes               state is absent
                      request–state       required service        exposed deployment
Availability                                                                               supported failure states
                      pairs               result                  state
                                          whether the obliga-
                      declared fair
Liveness                                  tion is eventually      trace projection         supported fair traces
                      traces
                                          met
                      version–            required meaning or retained key, ancestry,
Maintainability                                                                       declared evolution policy
                      requirement pairs   reachability bit    and external routes

Table 5: Instances of the shared boundary contract. The safety, availability, and liveness rows characterize
preservation of the declared answer; they do not add latency, probability, scheduler, or fairness claims
not present in the row’s inputs. All rows instantiate the generic Lean theorem BPI1; the projection row
additionally uses PJD1–4 for its exact-recovery/non-extraction duality.

   For the projection-limited row, trusted recovery of every required answer is equivalent to the absence of a
conflict inside a full-evidence fiber, whereas every projected decoder fails somewhere iff the projected view
has such a conflict. The projection is the adversary hypothesis; side channels require a different projection
and therefore a different theorem instance.12

Uncomputability of unrestricted structural safety. For every fixed B ≥ 1, a total computable
transformation maps each program index e to a total observer fe such that fe has a fiber larger than B
10   BAP20-25   11   BPI1   12   PJD1-4

exactly when program e halts on input zero. Consequently the safety set, restricted to codes produced by
this total-observer transformation, lies outside the recursively enumerable sets.13
    For the computability classification, map program index e to the total observer
                                        (
                                          0,      if φe (0) halts within x steps,
                              fe (x) =
                                          x + 1, otherwise.

If φe (0) halts by step s, inputs s, . . . , s + B map to zero; otherwise the observer is injective. Halting therefore
reduces to HasBarrierB . For every fixed B ≥ 1, nonhalting reduces through the same total observers to
MultiplicityBoundB , and that safe predicate lies outside the recursively enumerable sets. Non-enumerability
persists after restricting the safe set to total observers.14
    A sharper fixed-table construction uses the carrier Fin(B + 1). At state s, bounded evaluation chooses
either the identity table or the constant-zero table. Each snapshot is total and decidable, but safety of the
whole trace is not recursively enumerable for any fixed B ≥ 1. For B = 1 the carrier has only two meanings.15
    The finite-table strengthening moves the clock from an observer input to the trace index. At state s,
finiteClockTrace returns identity until bounded evaluation sees the source halt and returns constant zero
afterward. Thus each state is an executable table on B + 1 meanings, while the trace is safe iff the source
does not halt, including when the carrier has only two meanings.16

Open-world one-parent adequacy. The inheritance-side transfer uses two implementation roles and
three consumers. Before bounded evaluation observes program e halt, the stream emits two disjoint singleton
scopes. Afterward it emits the fixed main-text AnyIO crossing {W, FR} and {FR, B}. Every snapshot is one
of these two finite tables and has a decidable laminarity test. The whole stream remains laminar exactly
when e(0) does not halt, so the safe-generator set is not recursively enumerable. Unrestricted multi-parent
ancestry realizes both tables by the same provider-preserving construction.17
    Consequently, any computable checker that never accepts an unsafe generator accepts only a proper
recursively enumerable subset of the safe generators: some safe generator remains uncertified. A decidable
project policy can restrict which generators it admits, but must exclude at least one safe future. It has changed
which futures are admitted; the original uncertified generators remain beyond that checker’s coverage.18

Finite tests and their trust boundary. For a finite reachable scope V and tested states T , Lean
defines dtest = |V \ T | and proves that it vanishes iff V ⊆ T . More generally, a correctly specified suite
establishes agreement with its specified outcomes iff it passes and every reachable violation lies in T . On
the natural-number state space every finite T omits some state; flipping only that state’s Boolean outcome
constructs an incorrect behavior that still passes every stored expectation.
    For the computability transfer, prepend one fixed tree-shaped snapshot to finiteRequirementClockTrace
and test only that first state. The nonempty singleton suite always passes and is complete iff every snapshot
is tree-shaped, iff the source program does not halt. Thus the set of generators for which even this one-test
suite is complete is not recursively enumerable. The suite’s expected outcomes are themselves an external
relation: verifying them moves authority to a checker. Verification terminates in a declared trusted base or
leaves an assumption.19

Matching-coarsening recourse. For n ≥ 2 entities, one arbitrary revealed matching coarsening, strict
zero error, and an integer budget r ≥ 0 of endpoint retaggings after that matching is revealed, the exact
permanent alphabet size is max(2, n − 2r). At r = 0 this is global uniqueness; once r ≥ ⌈(n − 2)/2⌉, two
reusable tags suffice.20

Explicit
        field-menu   translation. For every k ≥ 2, a field-monotone family on N = 2k entities uses exactly
     k     k+1
k + 2 = 2 = O(log2 N ) binary fields, starts from a discrete profile, and keeps multiplicity at most two in
               

every supported branch, while its union conflict graph is KN . Every robust permanent assignment therefore
uses N distinct tags.21
13   UND5-22   14   UND13-22   15   UND23-30   16   UND23-30   17   UND31-38   18   UND39   19   TST1-10   20   MRC1-5   21   FLD1-8

Two and three known states. For a finite declaration set and at most two known observation states, the
minimum permanent tag alphabet equals the largest same-looking group in either state. There also exist
three states on four declarations where each state costs two tags and the union costs four.22

Minimum replacement state. Fix a chosen single-parent repair and let bc (f ) = 1 exactly when the repair
misses implementation f at class c. If a decoder preserving these omission answers receives f and a residual
code z(c) while c stays hidden, then z(c) = z(c′ ) implies bc = bc′ . The equivalence classes of equal complete
answer patterns form an exact code, so their number is the exact minimum state count.23

4.2      Independent outside bundles
Grouping forwarding routes shortens their description, but each group still supplies required implementations
to specified classes. Fix d required implementation–class pairs supplied outside inheritance, partitioned among
b nonempty bundles. Independent control exposes all 2b bundle sets, of which only the full set supplies every
required pair. Any mechanism that distinguishes those sets therefore has at least 2b states and needs at least
b fixed-width bits. A checker, generator, or coupling rule can exclude the incomplete sets only by determining
all d pairs and becoming their authority.24
    For a fixed sound native relation H, let S be the finite family of outside sets Rtreq \ H admitted by the
declared histories. If a fixed generator decodes each S ∈ S from its input, equal inputs imply equal outside
sets. Its input therefore has at least |S| states and any fixed-width binary input needs at least ⌈log2 |S|⌉ bits.
If S contains all subsets of d outside pairs, the bound is d. Correlated requirements can shrink S; source
length does not change this counting bound. Any varying rule or additional state used by the decoder is part
of the input being counted.25

Setup-inclusive charged work. Compare designs with the same declarations and previously installed
resources. Among states sharing this context, suppose M ≥ 1 require different outside sets. The decoder
receives that shared context plus every additional varying input, including changes to its own rule. Call that
additional input its transcript. Suppose s setup actions and u update actions carry at most w > 0 bits each;
the padded transcript has width w(s + u), including any stopping choices. Then
                                                                            
                                                                   ⌈log2 M ⌉
                                 M ≤ 2w(s+u) ,     s + u ≥ A :=                .
                                                                       w

Equal context and transcript must give the same required set, so the transcript is injective on these M states.
Counting transcripts gives the first inequality; the binary logarithm and integer rounding give the second.
For d independently variable outside answers not determined by the context, M = 2d and A = ⌈d/w⌉. Setup
and updates contribute to the same action total, so moving an action into setup leaves the bound unchanged.
Let shared work cost C0 and total cost C satisfy C ≥ C0 + q0 (s + u) for a declared per-action price q0 > 0.
Then C − C0 ≥ q0 A, and M ≥ 2 gives the coarse floor q0 . This is a human-labor floor when the charged
actions are human input actions; machine actions use their own price and unit.26
    Installing a required tool has a separate coarse floor. If a required batching tool is unavailable before
installation and available after s charged installation actions, s > 0: zero actions leave the initial state
unchanged. If these human actions satisfy C ≥ C0 + q0 s, then C − C0 ≥ q0 , even when every later update is
automatic. Charge an existing batcher’s construction at its acquisition boundary; do not charge it again per
use. The availability premise and labor price are explicit, rather than inferred from the ancestry gap. The
two floors need not add: one setup action can satisfy both.27
    Input supplied by the common specification is counted once. If that context already determines every
outside set, an exact generator needs zero additional input bits: take the context and required answer to
be the same set, and the transcript to have width zero. Lean checks this construction for arbitrary outside
sets. Zero additional input bits means the shared context already supplies the information. Construction and
execution still have their own costs, and the protocol identifies which actions supply that context or install
the tool. The human-input bound charges those actions; the semantic-answer bound counts the required
facts even when a generator receives them from shared context.28
22   PST1-4   23   BWR3-8, BWR14-18   24   RLC3-4, RLC6, BWR37   25   RLC7-10   26   CWF1-6   27   CWF8-9   28   CWF7

Adding unrelated pairs leaves the gap fixed while shrinking its fraction. For any finite required
table and any n, add one new role that serves n fresh classes and no old class. This adds exactly n required
pairs but does not change the old crossing or its ancestry gap. Hence d↓ /|Rreq | can approach zero while the
number of pairs a one-parent rewrite must move stays fixed.29

Proof detail for keys after name erasure. Main-text Section 6.3 applies the stable-key coloring criterion.
Fix a finite nonempty set C of lookup cases, their required answers a(c), and the retained views pt (c) over the
declared reachable states. Join c and c′ when some state gives pt (c) = pt (c′ ) but a(c) ̸= a(c′ ). A permanent
key is adequate exactly when it properly colors this conflict graph G. The minimum raw palette has χ(G)
values; the additional-name count beyond one shared constant is χadd = χ(G) − 1, so 0 ≤ χadd ≤ |C| − 1. It is
positive exactly when G has an edge. An injective retained view at every state needs no additional key even
when class names are erased. Thus neither name erasure nor a positive ancestry gap alone forces an extra
name. The upper bound counts lookup cases because each key distinguishes their required interpretations;
displaced implementation pairs have a separate count. The prices are independent: a complete-conflict identity
family on m meanings has additional-name price m − 1 for m ≥ 2 with an empty implementation relation,
while the h × h row–column implementation family has gap h(h − 1) with an empty identity domain.30

Minimum reusable adapters. When an adapter is selected before the requested implementation is known
and supplies only displaced dependencies, adapter representations preserving those dependencies translate to
and from all-ones rectangle covers of the gap matrix. The minimum active adapter count is therefore its
biclique-cover number, equivalently its Boolean rank; every fooling family gives a checkable lower bound.31

Choosing one authoritative declaration per role. The formalization separates the intended relation
from its source encoding. For specification Rreq , a declaration set D supplies exactly the required pairs when
its encoded relation RD equals Rreq . Replacing D by a denotationally equivalent D′ therefore preserves
exact supply, and a declaration whose denotation is already covered can be erased. These statements allow
factoring without identifying declarations with semantics.32
    Fix a finite declaration-independent set of atomic roles, where one role contains the supplied meaning
and its required consumer scope. Assume each normalized declaration denotes exactly one whole role, every
required role has a candidate, and two candidates share a role exactly when their denotations agree. Exact
supply is equivalent to coverage of every role; irredundancy chooses exactly one candidate per role. The
irredundant choices supplying every role are therefore the bases of the partition matroid whose parts are
denotation fibers. Every basis has one authority per role, and basis exchange substitutes an authority only
inside its denotation fiber.33
    Because consumer scope belongs to the role denotation, every such basis induces the same required scope
family. Hence any scope-dependent quantity, including the ancestry gap, has one basis-invariant value. There
is no further basis optimization: changing the required scope changes Rreq , while exchanging an authority
preserves it.34

Role-normalization direction. Splitting independently changeable roles across distinct provider endpoints
gives the one-parent rewrite more freedom; it does not create its lower bound. For roles with one required
scope, replacing their retained rows by a largest row preserves nesting or disjointness and cannot increase
deleted pairs. Once a candidate forest is fixed, choosing the largest descendant set contained in each role’s
required scope minimizes that role’s gap. These two reductions prove the numbered normalization proposition
in the main text. Forcing several roles to remain on one source node adds a co-location constraint and
therefore cannot improve the optimum.35

Exact supply forbids over-coverage. The native-scope subset condition is load-bearing. If unwanted
native pairs are permitted and the objective charges only omissions, assign every fact the universal consumer
set. The resulting family is laminar and misses no required pair, so every input relation has gap zero.36 The
main model instead requires the derived relation to equal the required relation at each state. Preinstalling a
29   GRI1-4 30 NHF15, NHF28, EVO17, EVO23-30, EVO40, AVG1-7   31   NHF29-30, NHF47-48   32   SDN1-4   33   SDN5-14   34   SDN15-17
35   NHF35-41 36 BWR33-34

future path is therefore inexact whenever that path creates a pair absent from the current requirement; this
is exactly the obstruction used by the immediate-parent construction below.
    The closure formulation supplies a separate general extension. Let cl(S) contain the primitive responsibil-
ities semantically derivable from S. A finite extensive, monotone, idempotent closure satisfying exchange is a
matroid closure; its irredundant complete sets are the bases and share one rank.37 The normalized atomic-role
model derives exchange from its denotation partition. More general semantic closures fall under the same
matroid result when they satisfy exchange; domains without exchange retain the preceding denotational
supply-preservation results without the basis theorem.

4.3      Immediate-parent evolution
The snapshot theorem tells us which classes inherit which implementations. It does not tell us whether
an update can preserve the edges deriving them. Here the code-provider nodes and consumer-class leaves
stay fixed as requirements evolve. Each node has at most one direct parent; following zero or more parent
edges defines ancestry. The fixed finite set of nodes may include arbitrary helpers before the first state. An
edge-monotone update keeps every old parent edge.
    The path lemma says that if every edge of an old parent chain survives, every node lying between its
endpoints after the update already lay on that chain. The local hypotheses therefore force repair at every
strict insertion; the history below repeats that event. Write Atf for the classes needing code provider f at
time t. Suppose Rtreq ⊆ Rt+1 req
                                 , both snapshots are tree-shaped, At+1
                                                                     x   ̸= ∅, ∅ ̸= Atx ⊊ Atq , Atx ⊈ Atg , and
  t+1      t+1      t+1
Ax ⊊ Ag ⊊ Aq . Every one-parent update preserving required supply deletes an old parent edge or
leaves a missing or unwanted pair before or after the update, even with preallocated helpers. If both endpoints
have exact ancestry-only supply, strict nonempty scope inclusion orders the providers: q is above x initially,
and g lies strictly between them afterward. A class in Atx \ Atg proves that g was off the old path. The
preserved-path lemma forces deletion of an edge on the old x–q path. A class in Atx reaches q through that
edge, and growth keeps this pair required, so the deletion is charged under Brep . With inexact ancestry,
a missing or unwanted endpoint pair already witnesses repair. The Lean result does not need a separate
laminarity hypothesis: exact one-parent endpoints entail it.38 Now take scopes

                                                                 a b c
                                                          top    1 1 1
                                                          left   1 0 0
                                                        right0 0 1 0
                                                        right1 1 1 0
    where only the right scope changes. Both snapshots are laminar. In the second snapshot, right lies strictly
between top and left. It did not lie on that chain in the first snapshot, so an edge-monotone one-parent
update is impossible. A concrete tree preserving exact supply retracts top–left and adds right–left. Exact
supply forbids preallocating the final chain: putting left below right in the first state would make right reach
a. The exact-supply multi-parent construction retains every old edge and adds only right–left.
    For the length-n family, keep one permanent top role. At state k, roles through k receive nested consumer
prefixes, while later roles receive disjoint singleton consumers. Each state is laminar and each transition
only adds required pairs. Every ancestry-only one-parent policy preserving required supply retracts at least
one old parent edge at each transition, so the accumulated retraction count is at least n. More generally, a
one-parent policy preserving required supply with forwarding and removal either retracts an edge or has a
missing required pair or an unwanted inherited pair at one endpoint. If its final raw parent function equals its
initial one, even after arbitrary intermediate changes, the final missing pairs together with the initial removed
unwanted pairs contain at least n(n + 1)/2 pairs. An exact-supply multi-parent construction retains the
initial leaf-to-provider and provider-to-top edges. At transition i, provider fi−1 , whose consumers are already
{c0 , . . . , ci−1 }, also inherits fi . Induction gives precisely the required prefixes. Provider indices increase along
every provider-to-provider edge, so every snapshot is acyclic. The update uses no helpers or forecast: it
retains the old graph and adds this one current edge. It performs exactly n additions and no retractions.39
    An unchanged hierarchy has a simpler exact accounting, independent of the one-parent condition. If a
finite native carrier is exact for R0req and remains unchanged, its later deficits and excesses are in bijection
37   L10-19   38   SIN1-5, NRH1-5   39   IPE1-28, IPE33-38, SIP1-7

with RTreq △ R0req : additions become deficits and removals become excesses. Hence their total count equals
the number of changed semantic answers. In the staged family, if the initial one-parent carrier is gap-free and
the final parent function returns to the initial one, every final gap is a unique strict lower-triangular pair and
the final outside count is exactly n(n + 1)/2.40
   More generally, fix one-parent ancestry H ⊆ R0req ⊆ RTreq . Write CH (t) = |Rtreq \ H| and eH (t) =
CH (t) − d↓ (Rtreq ). Then

                                 CH (T ) = CH (0) + |RTreq \ R0req |,         eH (T ) ≥ eH (0).

The excess rises strictly when the gap grows by fewer pairs than the requirement; if the gap is unchanged,
every added pair increases eH by one.41
   An unchanged hierarchy also has a fixed coverage numerator. If it is exact for a nonempty R0req , remains
unchanged, and later requirements only add answers, then its currently valid native answers are in bijection
with R0req . Consequently the numerator of its coverage fraction remains |R0req |, and the fraction |R0req |/|Rtreq |
decreases strictly whenever the finite required relation grows. Independently, any view of answer capacity
at most M has coverage at most M/|Rtreq |; for M > 0 that ceiling strictly decreases as the required-answer
count increases. The first statement concerns the actual coverage of one fixed exact view; the second is an
upper bound for arbitrary views of bounded capacity.42
   The required relation grows throughout this family. Exact supply at the old state means that every
implementation-to-class pair derived through a retracted edge was required, and monotonicity keeps every
such pair required after the transition. Retraction therefore records a representation-induced rederivation
and verification obligation because the semantic requirement persists. An atomic reparenting primitive still
contains that retraction.

Missing and unwanted pairs split into forced gap and removable excess. For required relation
Rreq and native relation H, define

                                             D(H, Rreq ) = (Rreq \ H) ⊎ (H \ Rreq ).

Lean proves that exact native supply is equivalent both to H = Rreq and to D(H, Rreq ) = ∅. On finite
relations its cardinality is the sum of the deficit and excess cardinalities and is positive exactly when exact
native supply fails. There is therefore a missing required pair, an unwanted supplied pair, or both. An
add-only repair preserving exact supply exists precisely when the excess is empty; it injectively owns the
entire deficit. Any excess instead needs a negative repair or a changed specification. Normalized disjoint
additivity makes cardinality the unique answer count, so this debt object is defined before and independently
of the one-parent optimization; minimizing its sound one-parent instance yields d↓ . More strongly, for every
sound one-parent selection H, Lean constructs an equivalence between D(H, Rreq ) and its ancestry gap. If
m = d↓ and eH := |D(H, Rreq )| − m, then |D(H, Rreq )| = m + eH , and eH = 0 iff H attains the minimum.
Thus the actual outside count splits into the forced ancestry gap and the removable excess of this particular
hierarchy.43

Successful outputs and ancestry cannot reveal the gap. One program has tree-shaped requirements
and another has a crossing. Both supply every required answer through routes and expose the same empty
ancestry. A rule reading only that ancestry must give both the same classification, so it cannot classify
both correctly. Lean checks the general principle: identical observations cannot distinguish worlds where the
required property differs. The witnesses in §4.1 instantiate it for tree shape and crossing-triggered policies.
A classifier that may abstain must abstain on their shared observation to avoid a wrong answer. Identical
successful outputs add no distinguishing evidence.44
   Conversely, exact supply recovers the required relation from ancestry H, removals U , and outside routes
ϕ: Rreq = (H \ U ) ∪ dom ϕ. Thus the complete supply map determines requirement shape and its ancestry
gap; the ambiguity concerns observations that omit part of that map.45
40   SCT38-39, IPE29-32   41   LRR1-7   42    SCT40-45   43   SCT3-8, SCT46-55, BWR52   44   BWR42, OLC1-5, OLC7-8   45   OLC9

Capacity-removal descent. Fix the requirements and the five native-capacity rungs. Write their outside
counts as

              max{χ(Greq ) − 1, 0} + |M req | + |Rreq |,      d↓ (M req ) + |Rreq |,     |Rreq |,   d↓ (Rreq ),   0.

Lean proves that removing the next capacity never lowers this count and raises it strictly whenever the
removed capacity is used. A policy that fires on a positive outside count therefore preserves its own trigger
and reaches the structural-only endpoint within four removals. If practitioner skill enters only through a
per-occurrence price q(σ), lowering that price lowers the charge but changes neither the semantic trajectory
nor its endpoint.46

Ignoring workaround costs reverses the choice. Let acquire pay any finite training, migration, or
implementation cost K ∈ N>0 , and let decline retain the incomplete mechanism’s workaround stream. A
declared ledger that charges K to acquire while assigning zero benefit to avoided workarounds strictly ranks
decline cheaper, independently of the per-occurrence repair price q. Lean also gives two exact programs
with identical successful native observation and hidden workaround indicators zero and one; consequently no
function of that observation determines whether the minimum outside count is zero or positive. For every
repair price q ∈ N>0 , the staged-history lower bound supplies the explicit finite threshold N = ⌊K/q⌋ + 1,
beyond which K < qBrep (n) for every one-parent policy preserving required supply. The correct-maintenance
ledger then strictly ranks acquisition cheaper while the declared ledger still ranks decline cheaper. That
threshold never increases as positive q increases: lower per-occurrence cost delays but cannot eliminate
the finite reversal. This proves an incentive-order reversal without an assumption about rational choice,
practitioner cognition, or actual conduct. The main text’s primary records document cost-side omission
rationales; its pinned census documents installed availability rather than behavior.47
    Choosing implementation authorities and choosing inheritance edges are different problems. The partition
matroid records the first: choose one declaration per role before choosing a hierarchy. Tree edges give the
second, a graphic-matroid choice: adding the third edge of the top–left–right triangle requires removing an
old edge. The immediate-parent theorem keeps the providers and classes fixed while proving that this carrier
exchange cannot be avoided by helper nodes. The staged family repeats that graphic exchange at every
transition while the authority basis stays fixed and every snapshot remains laminar.

Parked-top policy: standing supply without fresh repairs. Start with ci → fi , leave cn+1 parentless,
and route top to all n + 2 classes once. Update i adds fi−1 → fi . At state k, each cj with j ≤ k reaches
fj , . . . , fk ; later cj with j ≤ n reach only fj . These are exactly the required non-top pairs. No edge is deleted
and no route changes. The same n + 2 missing top pairs give Brep (n) = 2n(n + 2) and positive outside supply
at every state, so the policy misses the maintenance ideal throughout: the required outside pairs persist even
with unchanged route code. More sharply, δ + π ≥ n, where δ counts charged deletions and π counts updates
with endpoint missing or removed pairs, once per update.48 An ancestry-only policy has π = 0, hence δ ≥ n;
the parked policy has δ = 0, π = n. The pricing corollary counts additional work covering each endpoint
occurrence, including standing routes. One invariant validates unchanged routes; fresh configurations require
fresh checks, as the protocol below proves.

4.4      Admission checks for fresh routing configurations
Fix the required relation and m outside routing entries that can each be omitted independently. Each
deployment supplies a fresh configuration; the complete configuration and each single omission remain possible
regardless of earlier audit results. The configuration stays fixed during admission and use. The checker must
accept the complete configuration and reject every single omission. Its only evidence about enabled entries
comes from probes, each asking whether all entries in a chosen batch are enabled. Batches contain at most
b > 0 entries; the next batch may depend on previous answers.
    On the complete input, any unread entry could instead be missing without changing the observed answers
or the accepted execution path. Therefore every entry must be read, and at least ⌈m/b⌉ probes are necessary.
For N independently supplied deployments the same argument applies to each fresh input, giving at least
46   CSD1-8, CAP7, EVO17-18, EVO40   47   INC1-8, OLC1-5, SCT31, OPT3-12   48   NRH8-9

N m/b probes in total. This counts executed queries: each fresh input can independently omit an entry, so
earlier checks cannot certify it. Fix the maintainer’s positive per-probe price ℓ in a declared cost unit. The
cumulative charge is at least ℓN m/b. Thus this protocol supplies rate q = ℓm/b > 0 when m > 0. Acquisition
cost and continuing disadvantages can be compared against this rate using the finite-acquisition inequality
below. Fresh deployments force fresh probes and their recurring charge. The staged count Brep prices a
protocol covering its endpoint occurrences (§4.5).49
    The artifact’s completion_admission_audit.py runs this protocol on the entry identities in the archived
AnyIO omission report. It reads fresh Boolean configurations. Reusing AnyIO’s entry identities fixes the
input; the admission checker itself executes the probes. For 21 archived entry identities, capacities 1, 4, 64
give respectively 21, 6, 1 probes on each accepted deployment, attaining the bound; 8 fresh deployments take
168, 48, 8 probes. Every single omission is rejected for each capacity. Tests also exhaust all configurations of
zero through eight entries. The report binds the historical input by digest and retains every queried index.
These exact operation counts hold before prices; every positive per-probe price gives the corresponding lower
charge bound.
    The comparison credits the same guarantees on both sides. A checked native construction or a proved
routing generator can restrict deployments to complete configurations; then this additional omission audit
needs zero probes. Building and checking that guarantee remains part of acquisition and upkeep. Common
specification and behavioral checks remain in both policies’ costs. With unrestricted probe capacity, one
aggregate query also suffices, although its implementation and cost still need accounting. These cases
are checked alongside the lower bound. Fresh independent omission possibilities, rather than the word
“composition,” create this recurring validation obligation.50

4.5      Pricing repair and retention
A ban leaves requirements in force while removing their native derivations. Workarounds supply outside
answers, with labor and replacement costs [Boehm(1984), Boehm and Sullivan(2000), Sullivan et al.(2001)]
shaped by debt, libraries, installed code, and experience [Cunningham(1992), Meyerovich and Rabkin(2013)].
Graham’s adoption barrier and Ousterhout’s tactical tornado price learning and deferred costs [Graham(2001),
Ousterhout(2021)]; neither erases workarounds.

Semantic deletions do not create repair obligations. For an old edge e = (u, v), its footprint
Throught (e) consists of pairs (f, c) with an old path from c to u, the edge u → v, and an old path from
                                                             req
v to of . Charge its deletion exactly when Throught (e) ∩ Rt+1    ̸= ∅. The test uses the old footprint: a
replacement path restores supply while the still-required answer loses its old derivation. Withdrawing only
no-longer-required supply is a semantic edit; removing an unused edge also creates no repair charge. Brep
adds charged deletions to missing and unwanted endpoint pairs.

Charged deletions interrupt retained derivations. Keep the same nodes across the update, including
any preallocated helpers. Let Dert (f, c) contain the finite ancestry edge paths from c to provider of at state t.
The pair retains a derivation iff Dert (f, c) ∩ Dert+1 (f, c) ̸= ∅. These edge lists retain the answer’s derivation:
two paths can reach the same provider through different edges.
Lemma 4.1 (Charged deletion is loss of a still-required derivation) 51 . On a fixed acyclic one-
parent carrier, including no self-parent edges, | Dert (f, c)| ≤ 1. An update has a charged deletion iff some
               req
(f, c) ∈ Ht ∩ Rt+1 retains no old derivation. Any addition-only update retains every old path; in particular,
the direct multi-parent construction does so on every growing history.

Proof. The parent function fixes each next edge. Two paths with the same endpoints therefore coincide:
induction compares their first edges, and acyclicity excludes a nonempty path competing with an empty one.
A charged edge carries a still-required pair. That pair’s unique old path uses the deleted edge and cannot
survive. Conversely, take a still-required pair with an old path but no retained derivation. Some edge of its
old path was deleted; otherwise the same edge list would remain a path. Its deletion is charged because that
pair lies in its old footprint. An addition-only update preserves each edge of every old path.
49   CAA1-5   50   CAA6-7   51   DRP1-5

This prices derivation replacement even when the required answer and provider identity stay fixed. The
deletion count remains an edge count: several deleted edges can interrupt one pair’s path. One header edit may
replace many paths; the answer, operation, and labor counts remain separate (main-text Proposition 4.16).
The lemma equates the per-update witnesses; several deleted edges on one path explain why their counts can
differ.
    For R0req = {(f, c)} and R1req = ∅, raw edge-deletion accounting gives direct ancestry (O0 , Braw ) = (0, 1).
Forwarding also pays one endpoint obligation for its old missing pair, giving (1, 1): the old outside pair still
contributes. This example does not refute joint attainment of the raw ideal. Normalization instead separates
a requested withdrawal from repair: deleting the direct edge is uncharged and direct ancestry attains (0, 0).52

Joint attainment on arbitrary finite supply histories. Fix finite provider and consumer sets F , C,
using separate nodes even when one source declaration has both roles. At each state give providers no parents
and include c → of exactly when (f, c) ∈ Rtreq . Paths have at most one edge, so the graph is acyclic and
supplies exactly the required relation. Each deleted edge carried only its now-unrequired pair. There are
no charged deletions, missing pairs, or unwanted pairs, even when requirements shrink and later return.
Thus one design history jointly attains zero outside supply and Brep = 0 on every finite prefix. The same
construction applies independently to contract edges; separate graphs attain both supply coordinates together.
The flat graph satisfies main-text Proposition 4.2’s base-order condition. Lean checks these abstract graphs
and zero counts; Python’s class construction remains external.53
    For the supply capacities (none, one parent, multiple parents), define universal attainment as attaining
this joint ideal on every finite declared history. Multiple parents attain it by the direct construction. No
implementation parents fail on one required pair; one parent fails on the two-provider, three-class crossing.
The negative proofs allow arbitrary helper nodes. Instantiating the same statement for contracts and
implementations proves the iff in main-text Corollary 4.12. Identity histories retain their separate key
minimum (main-text Theorem 3.2); this characterization concerns the supply rungs.54

Exactly which accountings preserve the strict separations. Multiple parents leave no outside work
on a crossing. Every sound one-parent design leaves some. Changing the prices preserves every crossing
separation exactly when any nonempty batch costs more than doing nothing. Prices may include bulk
discounts and may fall when more facts are supplied.
Theorem 4.2 (Every crossing survives exactly when nonempty outside work costs positively) 55 .
Let U be an infinite universe of fact labels and let Q : {S ⊆ U : S finite} → R, with Q(∅) = 0. Give distinct
required implementation–class pairs distinct labels. The following are equivalent:

     1. For every finite crossing relation, the zero-outside multiple-parent design costs strictly less than every
        correct sound one-parent design.
     2. Q(S) > 0 for every finite nonempty S ⊆ U .
For each finite declared relation, condition (2) also gives a uniform positive lower bound on all its nonempty
outside sets, hence a strictly positive one-parent infimum. No additivity or monotonicity is required.
Proof. For (2)⇒(1), a sound one-parent design of a crossing leaves a nonempty outside set (main-text
Theorem 4.6). Its price is positive, against Q(∅) = 0. The declaration has only finitely many nonempty
subsets; the minimum of their positive prices is positive.
   For the converse, fix any finite nonempty S and choose a ∈ S and three distinct fresh labels b, c, d ∈     / S. Give
provider f the class set {x, y} and provider g the class set {y, z}; label their four pairs (f, x), (f, y), (g, y), (g, z)
by a, b, c, d, respectively. For each other label in S, add one provider required by one fresh class. Retain just
the three crossing pairs labelled b, c, d in ancestry and forward exactly S. The retained scopes are {y}, {y, z},
and empty sets, so they are tree-shaped. Native supply is sound and forwarding completes every required
pair. Its outside set is exactly S, so (1) forces Q(S) > 0. The Lean construction also checks that all required
pairs have distinct labels.
52   MGH9-11, NRH7   53   MGH1-3, MGH9-11, UMS5-6   54   UMS1-6    55   ACF1-4
