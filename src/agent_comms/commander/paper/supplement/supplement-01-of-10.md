# Supplement to Semantic Completeness for Correct Maintenance: part 1 of 10

Optional reading: the paper1 supplement converted to layout-preserving text, page by page. Figures are PNG files in `figures/`.

Supplementary Material
Semantic Completeness for Correct Maintenance: A Strict Hierarchy
                  of Typing and Inheritance
                                          Anonymous Author(s)
                                          Anonymous Institution

                                             October 15, 2026

Notation
Read this table in order to build the notation from classes and code providers to the bounds; the plain-language
column ties it to the main-text AnyIO example.

      Symbol                           In words                                    Precise meaning
        C              The classes: W (writer), FR (file reader),      Declared classes; for identity questions,
                       and B (buffered reader) in the main-text                 persistent meanings.
                                    AnyIO example.
        F              Independently changing shared code: file       Independently changing implementations.
                     lifecycle and byte iteration in the main-text
                                   AnyIO example.
        of               The class declaring the shared code:           The distinct code-provider declaration
                      _BaseFileStream or ByteReceiveStream.                 containing implementation f .
        FR               The file reader that needs both code        The file-reader class in the main-text AnyIO
                       providers; the other two classes need one     example; W is the writer and B the buffered
                                         each.                                           reader.
        Af            Who needs the code: file lifecycle {W, FR};     Classes requiring implementation f : from
                                 iteration {FR, B}.                      code provider to consumer classes.
       Rreq           The complete list of implementation needs,     Required implementation–class pairs; Rtreq is
                       written as (code, class) pairs: four here.              the relation at state t.
        req
       Rpub          The separate audit contract keeping selected      Pairs witnessed by selected public-name
                      public lookups, including Python special                lookups under the separate
                                      methods.                        resolution-preservation contract, including
                                                                                       dunders.
       M req         Which classes must satisfy which interfaces;       Required class–contract memberships,
                     this is separate from receiving their code.        separate from implementation supply.
        H            The needs inheritance supplies: all four with    Pairs supplied by ancestry; sound ancestry
                      multiple parents, at most three with sound                    has H ⊆ Rreq .
                               one-parent ancestry here.

Symbol                         In words                                      Precise meaning

    φ         Which classes receive required code through       Outside supply routes; dom φ lists the pairs
              forwarding instead of inheritance: one pair in                   they supply.
               the best one-parent design here; generated
                            routes count too.
    →            One forwarding step from a class to a           One routing edge; ancestry is the separate
                destination; ancestry follows separately                         order ⪯.
                         declared parent edges.
    Γ         The forwarding steps through which code or        Routing graph whose edges are →; distinct
                     tooling supplies outside pairs.                         from ancestry.
   →∗         Follow no routing step, one step, or several      Reflexive–transitive routing closure: zero or
                       steps to the destination.                            more routing edges.
    U           Unwanted inherited pairs removed in the         Removed ancestry pairs in the relaxed-supply
               relaxed model; superclass promises remain                 model (main-text §4.4).
                                binding.
   RD         Everything supplied by either route: exactly         Pairs design D supplies; exact supply
              the four required pairs in a design supplying                 requires RD = Rreq .
                         all and only those pairs.
  sound       Inheritance gives no class an implementation          Sound ancestry satisfies H ⊆ Rreq .
                            it does not need.
tree-shaped   Any two need-groups nest or do not overlap. For all providers f, g, Af ⊆ Ag , Ag ⊆ Af , or
                The main-text AnyIO example instead                       Af ∩ Ag = ∅.
               crosses: the groups share FR and neither
                           contains the other.
    d↓        The fewest pairs code or tooling must supply Fewest required pairs outside any sound
               outside sound one-parent inheritance: one one-parent ancestry preserving the providers.
                                  here.
    eH         Outside pairs beyond the gap: zero in the Removable excess above the ancestry gap for
                best hierarchy here. Refactoring removes        a fixed sound hierarchy H.
              excess; every sound one-parent hierarchy still
                              leaves the gap.
D(H, Rreq )   What ancestry misses, together with what it           Missing required pairs together with
                          supplies wrongly.                              unwanted supplied pairs.
    dλ        Allow unwanted inherited pairs too, charging        Relaxed-supply minimum with removal
               λ to remove each one; minimize that cost                         charge λ.
                         plus missing pairs.
  Ot (D)      How many required type identities, interface      Outside-answer counts for design history D
              memberships, and implementation–class pairs        at state t; infima are taken separately for
                are left to code or tooling at this state.                     each coordinate.
 Brep (n)     Broken links still carrying required code, plus      Over n updates: deleted parent edges
                  pairs supplied or removed at updates.         carrying still-required pairs, plus missing or
                  Unchanged pairs count again because           removed pairs at both endpoints, including
                required supply persists even when route                       unchanged pairs.
              code stays unchanged. The main-text AnyIO
                         example has no updates.
    δ         Count old inheritance links broken while they Charged parent-edge deletions in main-text
                        still carry required code.          Theorem 4.14: each cuts a still-required pair.

Symbol                             In words                                    Precise meaning

         π              Count updates with required pairs still        Updates with missing or removed pairs at
                       supplied outside inheritance or unwanted        either endpoint, counted once per update;
                        pairs removed, even if no route changes.         δ + π ≥ n in main-text Theorem 4.14.
         Π               All new implementation needs in the          Added pairs {(fi , cj ) : 0 ≤ j < i ≤ n} in the
                            constructed every-step history.            staged growth history; |Π| = n(n + 1)/2.
   Throught (e)       Which pairs use an old inheritance edge. Its Pairs whose old ancestry paths use edge e; its
                       deletion is charged only if one remains        deletion is charged only if at least one
                                       required.                            remains required afterward.
    Maint∗ (H)        The best score on each count. A semantically         Coordinatewise outside-answer and
                      complete language supplies mechanisms that        repair-obligation infima; complete supply
                         reach all these supply and repair scores     jointly attains zero on every finite declared
                       together, even when requirements shrink or      supply history (main-text Corollary 4.12).
                                          return.
Reacht (D) ⊆ Validt   Every state the program can reach satisfies        Safety: every reachable state is valid.
                                   the specification.
         X               The cases a program must handle; for           Interface inputs or cases in the decoder
                       example, a client using an AnyIO stream.                 equation (main-text §2).
       Vans              The results a case may be required to           Required-answer space in the decoder
                         produce, including a complete set of                  equation (main-text §2).
                                  permitted outputs.
         E            The representations from which the decoder Encoding space: e : X → E and r : E → Vans .
                       must recover each case’s specified result.
       a(x)            The specified result for a case; a test checks The answer required for case x; e encodes it
                      its observed output against this requirement.               and r decodes it.
       Greq           Join meanings that can look identical while     Graph joining look-alike cases that require
                       needing different handling. This identity                  different answers.
                        question is separate from the main-text
                                AnyIO supply crossing.
     χ(Greq )          The fewest labels keeping joined meanings      Fewest stable key values; one shared value
                       distinct; one shared label leaves this many         leaves χ − 1 additional values.
                               minus one additional labels.
         P            Which code provider wins when definitions         Required lookup map choosing among
                      share a name. Every design preserving the         same-name contenders; k counts these
                       specified member lookups supplies these                        choices.
                                      choices.
         k                How many same-name conflicts the             Number of required lookup choices in the
                       specification resolves; every representation             fixed lookup map P .
                        preserving the specified lookups supplies
                                     those decisions.
         S             Which state accesses must see the same            Specified state-sharing questions; the
                      object or distinct objects; required in every   state-coordination supplement defines their
                                     representation.                                    domain.
     kσ,distinct      How many distinct-state requirements depart Specified distinct-state answers on common
                           from a native shared-state default;    providers in the supplement’s sharing-default
                      implementation pairs are counted separately                  proposition.
                             in the required supply relation.

Symbol                             In words                                  Precise meaning

           I            Which state component must initialize before    Specified initialization-order questions;
                         another; required in every representation.  representations preserving the specified state
                                                                     answers resolve the same |S| + |I| questions.
           q                Each repair obligation handled by this     Derived bound q = q0 /b > 0: operations cost
                           procedure costs at least this much, even     at least q0 and cover at most b obligations,
                                   when work is grouped.                with all standing-route endpoints covered
                                                                                 (main-text Corollary 6.2).
          Ka             The one-time cost of adopting a mechanism     Finite adoption cost for plan a; Ea (n) is its
                                          or tool.                         additional cost through n updates.
         Ea (n)             The additional cost after setup under       Additional cost through n updates under
                            adoption plan a, through n updates.          adoption plan a; the pricing corollary
                                                                                 assumes Ea (n) ≤ ra n.
           ra           The adopted mechanism’s recurring cost per Upper bound on adoption plan a’s additional
                        update; the threshold compares it with the       cost per update: Ea (n) ≤ ra n.
                                workaround’s lower bound.
         Kc (n)         Costs both compared policies share; subtract     Costs common to the compared policies,
                                  them before comparing.                subtracted before comparing adoption and
                                                                                    recurring repair.
           b            How many answers one operation can change        Maximum required answers changed or
                        or entries one probe can read, in the stated     entries read per operation, in the stated
                                           model.                                    operation model.

1       What Each Result Counts
Each missing capacity forces an irreducible semantic obligation. The main-text economics corollary
prices strict-insertion repair; its arity example shows equal outputs with unequal answer counts.1
    The identity results ask how many identity keys remain sufficient as visible records change. Table 2 records
which meanings persist or arrive, what the decoder can observe, and which future changes the allocator
knows. The second table separates implementation supply from lookup and self-calls.

1.1      Cost units
The main paper counts one maintained semantic answer at a time and keeps source artifacts, operations, and
developer time separate. Table 4 records the exact conversions and the hypotheses each conversion requires.

2       Required Pairs, Inheritance, and Workarounds
First fix which implementation each class needs. Then ask which of those pairs inheritance derives and which
code must supply. We define semantic consequence by its usual requirement: every model satisfying the
declarations must satisfy the consequence. For the positive atomic declarations below, this proves that the
consequences are exactly the declared pairs. The later minimum then equals both the fewest deletions needed
to obtain tree shape and the fewest required implementation–class pairs supplied by forwarding. Selecting
among competing method bodies remains separate.
    Fix code providers F , consumer classes C, and an independently declared required relation Rreq ⊆ F × C.
An applicability model M assigns a truth value to every atomic proposition f @c. It satisfies Rreq when every
pair in Rreq is true. Define semantic consequence in the usual all-models form:

                          Rreq |= f @c    ⇐⇒    M |= Rreq implies M |= f @c for every M .
1   Lean: OLC1-5, CSD1-8, CAP7, SCT31, INC1-9

Meanings
Result                                        Visible evidence              Future changes and information
                         present
                         persistent, ar-      arbitrary equivalence         realized trace or declared state family; zero
Future-conflict price
                         rivals allowed       states                        recourse
                         structural then
Clone-upgrade re-                                                           normalized tag change; additive and re-
                         independent       one observational clone
course                                                                      placement calibrations
                         identity evidence
                         persistent, ar-
Refinement price                              arbitrary equivalences        causal allocation; refinements only
                         rivals allowed
                         persistent, ar-
Coarsening gap                                arbitrary equivalences        online versus offline; monotone coarsening
                         rivals allowed
                                              one matching of pair          matching revealed after commitment; at
Matching recourse        fixed n
                                              merges                        most r migrations
                                              Boolean pres-                 downward-closed coordinate deletions; of-
Deprecation pair test fixed population
                                              ence/method sets              fline robust price
One-deletion stabil-                          Boolean pres-                 at most one safe coordinate deletion; tight
                         fixed population
ity                                           ence/method sets              multiplicity
                         fixed Fk2 popula-                                  full menu followed by literal field deletion;
Field menu                                 Boolean linear fields
                         tion                                               offline robust price
Two-state threshold      fixed population     two arbitrary partitions      both states known offline

Table 2: Scope of the identity-capacity results. Only the last four rows use coordinate-set profiles; the earlier
rows range over abstract observation equivalences.

The canonical model makes exactly the pairs in Rreq true. Hence, for this positive atomic theory,

                                           Rreq |= f @c     ⇐⇒      (f, c) ∈ Rreq .

The canonical model proves this equivalence from the all-models definition.
    Native ancestry supplies a relation H ⊆ F × C, as in the main text, with scope H(f ) = {c : (f, c) ∈ H}
for each provider. We abbreviate native reach by

                                                          c ∈ H(f )
                                                                    .
                                                          H ⊢ f @c

Thus the representation is sound when H(f ) ⊆ Rreq (f ) for every f , and complete when every semantic
consequence is derivable. Define exact native supply by

                                H ⊢ f @c       ⇐⇒     Rreq |= f @c        (f ∈ F , c ∈ C).

Exact native supply is therefore equivalent to soundness plus completeness. Validity is fixed before any
hierarchy, resolver, or tie-breaking policy is chosen.
    The native deficit and excess are

                        ∆+
                         H =R
                              req
                                  \ H,          ∆−
                                                 H =H \R
                                                         req
                                                             ,          D(H, Rreq ) = ∆+    −
                                                                                       H ⊎ ∆H .

                                           −
Completeness is ∆+H = ∅; soundness is ∆H = ∅. An add-only outside workaround preserving exact supply
adds a relation E such that (H ⊢ f @c) ∨ E(f, c) agrees with semantic validity. Every member of ∆+    H must
then map injectively to a distinct supplied decision. The canonical repair of sound ancestry takes E = ∆+   H
and attains the bound.
   Copies supply the same implementations to the same classes, but their authoritative source matters. For
any grouping of declarations into roles, either all declarations in a role mean the same thing or two of them

Result                    Native mechanism        Workarounds                Assumptions
                          contract-only, one-
                                                  none in the capacity theo- arbitrary required role–consumer
Derivation capacity       parent, or multi-
                                                  rem                        relation; distinct provider endpoints
                          parent ancestry
                          raw native reach
                                                  forwarding of every        unwanted supply forbidden; arbitrary
Ancestry gap d↓           supplies only re-
                                                  missed required pair       reparenting and helpers allowed
                          quired pairs
                                              forwarding plus one neg-
                          raw native rows can                                incidence cost; source declarations
Paid-removal gap dλ                           ative pair decision per
                          over-reach                                         can compress several pair decisions
                                              unwanted native pair
                                                                           monotone scope growth; persistent
                          one fixed finite car-   forwarding and unwanted-
Dynamic retraction                                                         providers, consumers, and preallo-
                          rier across states      supply removal decisions
                                                                           cated helpers
Coupled contract                                  workarounds supplying      the two row families must be lami-
                          one shared one-
and implementation                                missed memberships and     nar jointly; separate-graph minima
                          parent graph
graphs                                            implementation pairs       remain lower bounds
                                                  winner-preservation costs both preserve the specified winners;
                          one fixed winner
Lookup choices                                    remain separate from the their update costs are priced sepa-
                          contract P
                                                  semantic gap              rately
                          open-recursive sub- exceptional winner deci-
                                                                           requires a declared self-use site set
Self-call choices         class or closed wrap- sions; wrapper also routes
                                                                           and winner at each site
                          per                   every exposed member

Table 3: Scope of the derivation results. Paid removal relaxes exact supply without changing the required-only
inheritance theorem or permitting promise-blocking.

witness a distinction. Lean checks both cases. Keeping the distinction splits the role and changes the required
relation. When the declarations agree, choosing any one as that role’s authority gives the same required class
sets and ancestry gap.
    For any such authority choice, let H contain only pairs reached from those authorities and let E contain
all pairs supplied outside that ancestry, including through other declarations or forwarding. Exactness gives
E \ H = Rreq \ H. In the replica-only case, E is the replica-supplied relation. Lean identifies these outside
decisions with the native deficit, identifies their indexed cardinality with the selected ancestry gap, and
proves that every such count is at least the global laminar minimum. A replica can cover many consumers
syntactically; the relation records the applicability answer it supplies at each consumer. Independently writable
replicas additionally expose precisely the disagreement states excluded by deterministic exact expansion.2

2.1      Exact semantic factoring
Two classes may merge only if no requirement distinguishes them throughout their supported evolution.
If only their required implementations agree throughout that evolution, share the code and retain their
identities. If their required implementations differ, keep separate implementations.
    Lean checks these three cases using one table of answers across reachable states. Its canonical quotient
groups entities with identical complete answer histories; every answer-preserving encoding may merge
only within a group. Splitting the table into meaning and implementation questions gives full equality,
implementation equality alone, or implementation inequality. Every minimal exact implementation encoding
must share equal implementation columns; distinct full meanings still require distinct full-answer codes.
A two-entity witness has the same implementation column under both a mergeable and a distinct full
specification, so no implementation-only rule decides which case holds. More generally, Lean lets a structural
profile contain any data, including a complete inheritance graph, and proves that no decoder through that
2   SCT56-59, BWR58-62, SDN17-21

Quantity                  Unit                                        Assumption for a lower bound
Permanent-key mini-
                          names                                       declared future states
mum (§3)
Ancestry gap (§4)         implementation-to-class pairs               exact one-parent representation
                                                                      relaxed exact supply; cancel unwanted
                          forwarded pairs plus λ times removed
Paid-removal gap (§4)                                                 implementation supply while preserving
                          pairs
                                                                      superclass promises
                                                                      monotonically growing requirements; ex-
Hierarchy change (§4)     deleted old direct-parent edges             act consecutive states on one fixed finite
                                                                      node set
External-map minimum
                          states or bits                              fixed ancestry view
(§4)
Repair lower bound (§4)   operations                                  one operation covers at most b > 0 pairs
                                                                      each priced resource has a stated positive
Total-cost model (§4)     declared project cost units
                                                                      marginal cost

Table 4: The paper keeps distinct quantities in distinct units. Source lines, human time, and comprehension
enter only when a stated model measures them.

profile is exact when it maps two meanings with different required answers to the same profile value. For
finite answer sets A, B, the symmetric-difference triangle inequality proves
                                           |A △ B| ≤ |A △ X| + |B △ X|,
and X = A attains equality. Thus their symmetric difference is the exact correction floor for a forced shared
answer set: it counts present disagreements even before any future edit is chosen. Any current encoding
that merges two distinct columns must change at least one of their code assignments to become exact. The
changed semantic answer is required by every representation; this recoding is the merger’s additional distance
from an exact factoring.
    Shared parent combinations obey the same rule. For finite sets J of later parent additions and C of classes
declared to receive all of them, direct repetition stores the product J × C. A shared factor stores J once and
expands it across the already inherited class set C. Lean proves that both representations derive exactly
J × C, while their edit counts are |J||C| and |J|. The exact saving is |J|(|C| − 1); it is positive exactly when
J is nonempty and at least two classes share the factor. For a finite migration charge K, every later addition
count |J| > K makes K + |J| < |J||C| when |C| ≥ 2. A separate two-state witness gives the same parent
lists initially but common evolution in one specification and divergent evolution in the other. Thus current
structural equality cannot decide whether the shared class is semantically exact.3

2.2      Minimum one-parent workaround count
Multiple parents can supply the whole required relation, H = Rreq . The canonical provider-preserving multi-
parent graph realizes that relation, so every required relation has a sound and complete native representation.
One-parent ancestry restricts the family {H(f )} to be tree-shaped. Consequently, a sound and complete
one-parent representation would force the required family {Rreq (f )} itself to be tree-shaped.
   The main-text AnyIO relation gives the explicit counterexample to completeness: file-lifecycle code is
required by W and FR, while iteration code is required by FR and B. The two required scopes cross.
Therefore every sound one-parent native representation omits some required provider–class pair; which pair is
omitted may depend on the chosen tree. External delegation can derive the missing pair behaviorally, but
then the derivation is no longer native.
   For finite indexed families, minimizing |∆+   H | over all sound one-parent syntaxes gives exactly d↓ . The
index retains distinct providers even when their required scopes are equal. Minimizing the decision set of
3   SEF1-25

every add-only repair preserving exact supply gives the same value, as does independently minimizing explicit
forwarding over every provider-preserving one-parent-plus-delegation representation. That representation may
choose its nodes, helpers, provider and leaf placements, ancestry, delegation graph, and partial forwarding
map, and supplies exactly the required relation. Contract-only syntax has H = ∅ and leaves |Rreq | answers
outside; whenever Rreq is nonempty, retaining the incidences at one consumer already makes the optimal
one-parent outside-answer count strictly smaller. Unrestricted multi-parent syntax pays zero. The one-parent
optimum is zero iff the indexed required scopes are tree-shaped, and positive on every crossing family. At each
                                             −
state, ∆+H lists missing required pairs and ∆H lists unwanted supplied pairs. Their cardinalities count what
this hierarchy misses or supplies wrongly. The staged deletion-or-compensation theorem and its quadratic
endpoint bound therefore count these same pairs.4

3      Proofs for the Finite Identity Policies
These proofs compare ways to retain identity as records change. The first two paragraphs compare adding
identity evidence after a collision with retaining names from the start. The remaining proofs fix particular
kinds of record change and compute their identity-key or renaming costs.

Adding identity evidence after a collision. Count zero changes when the persistent tag function stays
fixed and one when it changes. This is the normalized identity-change unit. If a finite extension contains two
distinct meanings with the same structural profile, every adequate continuation of a structural discipline has
cost one: retaining the old tag function would retain the collision. One global nominal discipline is adequate
before and after with cost zero. For the separate one-artifact calibration, let s be the structural declaration
and n the independent identity evidence, and use semantic symmetric-difference distance. The direct path
∅ → {n} costs one. Retaining s gives ∅ → {s} → {s, n} at cost two; replacing it gives ∅ → {s} → {n} at
cost three. Clone extension forces the positive recourse. Keeping s attains the two-addition path, so deleting
it is an extra operation charged only on the three-change replacement path.5

Retaining names cannot increase extra-key demand. Index every reachable state through every
supported evolution of requirements. Let pt be the observed profile and at the required answer; one
permanent extra key must support a resolver recovering the specified answers at every state. Retaining
(pt , σ) instead of pt preserves every adequate key assignment. An injective nominal name σ needs no
extra key values. If one reachable state merges profiles whose required answers differ, every structure-
only repair needs at least two extra-key values; helpers computed from the profile cannot separate them.
A checked two-state history starts with distinct profiles and then merges them: nominal identity works
unchanged, while structural identity needs extra evidence or recourse. This comparison covers every supported
evolution of requirements, requiring no forecast of a particular collision.6 The existing structural Protocol
observer factors through its required-member view, ignoring nominal ancestry. This checks the observation
property; PEP 544 and the ABC documentation specify Python’s explicit and implicit admission rules.7
[Levkivskyi et al.(2017), Python Software Foundation(2025a)]

Keys when later observations only distinguish more cases. Suppose later refinements only split
groups of same-looking meanings. Every earlier meaning that can conflict with a new meaning v must already
look like v when it arrives. That group, or fiber, only splits later. The number of earlier neighbors (the future
backdegree) is therefore at most B − 1, so greedy gives the B-tag upper bound. A fiber of size B is a clique
and gives the matching lower bound.

Renaming when nonoverlapping       P pairs become indistinguishable. With m initial tag classes of
sizes si , an adversary can match i ⌊si /2⌋ ≥ (n − m)/2 monochromatic pairs. Each needs one change, so
m ≥ n − 2r, and a possible pair also needs two tags. Conversely, use m = max(2, n − 2r) tags, put n − m + 1
declarations in one class, and leave the others singleton. Any matching has at most r monochromatic edges;
changing one endpoint of each restores adequacy.
4   NPS1-9, SCT1-35, BWR43-49   5   IMR1-5   6   HVD1, HVD3, HVD4, HVD7, HVD8, HVD11, HVD12   7   PRT2

Monotone-coarsening online–offline gap. For part (a), color each final coarsening class separately with
B tags. A fiber of size B proves exactness. For part (b), each fixed pair-merge trace has a two-tag assignment
and contains a fiber of size two, so its offline price is exactly two. Online, first present n distinct profiles.
Reusing a tag fixes a pair that an oblivious adversary can compute from the common prefix and merge in one
later revision. For a randomized allocator, the finitely many pair-collision events cover the sample space, so
one fixed pair collides with positive probability; merging it violates probability-one adequacy. Global names
attain n.

Direct downward-closed deprecation graph. Any deletion D that merges x and y must hide every
observation on which they differ. Downward closure admits the canonical subset Dxy = Mx △ My , whose
finer partition preserves the multiplicity bound. Conversely, hiding Dxy merges the pair.

Tight one-deletion stability. Collapse each current fiber to a vertex of weight w(u). Single-deletion
conflicts join profiles whose cardinalities differ by one, so the quotient conflict graph is bipartite by the parity
of |Mu |, the number of observations in the profile. Fiber weight plays no role in that parity. Give the even
side its first w(u) colors and the odd side its last w(u) colors from a palette of size A. Admissibility gives
w(u) + w(v) ≤ A on every edge, so the two intervals are disjoint. A current fiber of size A gives the lower
bound.

Two deletions can raise the price. Use Boolean presence profiles x = ∅, y = {a, b}, and z = {b, c}.
Their symmetric differences give the explicit odd cycle
                                              {a,b}    {a,c}    {b,c}
                                           x ←→ y ←→ z ←→ x.

Each label is the pair of fields hidden to merge its endpoints. Two spare declarations share profile {d, e, f, g},
so the current price is two. The triangle requires three colors, and the checked three-color assignment handles
every safe deletion of at most two fields.

Two versus three known states. For two states, largest fibers give the clique lower bound. In the
bipartite state-incidence multigraph H, a tag class is a matching, so permanent tags are edge colors and ∆(H)
is the largest fiber. Kőnig’s theorem gives χ′ (H) = ∆(H). For three states, use the three k = 2 branches
retaining x1 , x2 , and x1 ⊕ x2 . Each state consists of two size-two fibers, while their conflict matchings cover
K4 and therefore require four colors.

4    Supply, Behavior, and Update Proofs
The proofs below separate three questions: whether the representation preserves required answers, whether
executions satisfy behavioral contracts, and how much outside supply or change remains. The pricing
arguments then state the operations and costs used to convert answer counts into work.

Relaxed-supply proof. One-parent descendant sets are tree-shaped. For any feasible family {Bf }, exact
supply requires forwarding every pair in Af \ Bf and removing every pair in Bf \ Af . Doing exactly this
attains its stated cost; minimizing over feasible families gives dλ .
   A sound optimum, with Bf ⊆ Af , is feasible and has no removal term, so dλ ≤ d↓ . If λ ≥ d↓ , any family
with an unwanted pair costs at least λ ≥ d↓ , while every sound family costs at least d↓ by definition. Hence
equality holds at that threshold. For λ = 0, taking every Bf = C gives a tree-shaped family of cost zero.
   If A is nonlaminar, Bf = Af for all f is infeasible. Every feasible family therefore differs in at least one
pair, costing at least min{1, λ} ≥ 1 when λ ∈ N and λ > 0. Thus dλ > 0. The equalities and removal-charge
threshold are checked by BLK1-10; the full declaration ledger appears in Section 15.

One answer-preservation condition across the models. As in main-text §2, an intent on cases X
and answers Vans is a function a : X → Vans . A visible representation e : X → E with resolver r : E → Vans
preserves the required answers when r(e(x)) = a(x) for every x. Equality of required answers induces an
equivalence on X. The visible equivalence refines it iff an answer-preserving resolver exists on the reachable
visible values. Schema adequacy instantiates this result with e = (p, σ). Implementation reachability takes
X = F × C, lets a(f, c) assert (f, c) ∈ Rreq , uses the represented membership bit as e(f, c), and fixes r to the
identity. The last choice preserves the positive/negative orientation and makes answer preservation equivalent
to RD = Rreq : each named pair keeps its truth value, whereas equal induced partitions also permit swapping
truth values. No finiteness or decidability instance is assumed by this shared layer.8

4.1      Behavioral substitution and exact applicability
For each required pair q, let αq map reachable provider states or histories into the observation space of the
consumer contract, and let Validq be the permitted observations. Lean proves the three equivalent forms

                 αq (Reachq ) ⊆ Validq ,     αq (Reachq ) \ Validq = ∅,       ∀Q, Q|Validq ⇒ Q|αq (Reachq ) .

The final form is the semantic property-preservation requirement: the reverse implication to set inclusion uses
membership in Validq as Q. Thus the converse assumes an assertion language containing that predicate; in a
weaker language, inclusion still implies preservation but the reverse need not follow. Taking the observation
type to be traces incorporates history constraints into Validq without adding object layout, initialization, or
state-aliasing semantics to the ancestry model.
    Two checks are needed: inheritance must supply exactly the required pairs, H = Rreq , and each required
pair q ∈ Rreq must satisfy the behavioral condition above. Finite witnesses show that neither check implies
the other. Passing both is necessary for correct maintenance, but does not establish its minimum. Combining
them with the one-parent lower bound shows that a behaviorally valid crossing still has gap at least d↓ > 0.
    Any sound proof method may establish the behavioral condition; the resulting statement does not depend
on which certificate was accepted. Any two sound and semantically complete methods certify exactly the
same pairs. Incomplete methods may reject valid pairs but cannot change the boundary. The mechanization
identifies that semantic boundary; it does not mechanize Liskov and Wing’s particular invariant, method,
constraint, or extension rules.
    Checking each pair’s behavior cannot choose the required pairs. A bijection that preserves every pair’s
behavioral condition also preserves the result of all those checks. Lean constructs such a bijection between two
four-pair relations in which every pair’s behavior is permitted. The first family is laminar and has minimum
outside-answer count zero; the second crosses and has strictly positive minimum. Hence no criterion checking
only each pair’s behavior, including behavioral substitution, can determine or discount the ancestry gap.
Concretely, use the main-text AnyIO classes W , FR, and B, with s naming file-lifecycle code and p iteration
code. The laminar relation is
                                    L = {(s, W ), (s, FR), (p, W ), (p, FR)},
and the crossing relation is
                                           R′ = {(s, W ), (s, FR), (p, FR), (p, B)}.
The checked bijection fixes the two s-pairs and maps (p, W ) to (p, FR) and (p, FR) to (p, B). All four
behavioral fibers are universally valid in both relations, while the ancestry gap changes from zero to positive.9

Valid overrides keep promised calls available. The specialization MethodContract fixes accepted
inputs D and permitted results V (i). An implementation is m : I → Option(O): none denotes an unavailable
operation. Permitted exceptional results belong to O and appear as some results, so the promised call remains
available. Its behavioral boundary keeps every i ∈ D reachable and observes (i, m(i)). Lean proves

                             Substitution(m)      ⇐⇒      ∀i ∈ D, ∃o ∈ V (i), m(i) = some(o).

Thus a replacement preserves substitution when it keeps all promised calls available with permitted results.
If m(i) = none for one i ∈ D, substitution fails, regardless of all other calls. A present method also fails if
8   INT1-7   9   BAP1-19
