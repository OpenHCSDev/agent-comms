# Supplement to Semantic Completeness for Correct Maintenance: part 6 of 10

Optional reading: the paper1 supplement converted to layout-preserving text, page by page. Figures are PNG files in `figures/`.

sampled/                           no runtime            χ history/
     Package                                     meanings    runtime    evidence    edges
                                     pinned                                                    latest

     emittery                         16/16         2          0           2          0         1/1
     react-hook-form                  16/16         1          0           1          0         1/1
     tinyspy                          16/16         2          2           0          0         1/1
     json-schema-to-ts                16/16         3          0           3          0         1/1
     wrangler                         16/16         2          2           0          0         1/1
     drizzle-orm                      16/16         2          2           0          0         1/1
     valibot                          16/16         3          0           3          1         2/2
     @tanstack/
                                      16/16         2          2           0          0         1/1
     react-start-rsc
     @tanstack/
                                      16/16         1          0           1          0         1/1
     start-client-core
     @typescript-eslint/
                                      16/16         1          0           1          0         1/0
     experimental-utils
     ts-toolbelt                      16/16         2          0           2          0         1/1
     ox                               16/16         1          0           1          0         1/1
     @envelop/core                    16/16         4          4           0          2         2/1

    The size-three certificate occurs in @sinclair/typebox. Release 0.24.22 places TOptional, TReadonly,
and TReadonlyOptional in the same erased profile; the three pair witnesses form a clique, and the stored
three-coloring matches it. In type-fest, MultiTagContainer and TagContainer conflict in release 4.3.3,
although the latest sampled release has price one. These are history witnesses: their archived earlier releases
contain the conflicting declarations.
    The extracted relation excludes string and enum brands, private-member nominal origins, unexpanded
generic instantiations, nonlocal or ambiguous symbol aliases, and unsampled releases. Those exclusions can
remove meanings and graph edges; they cannot make the displayed certificates invalid for the retained relation.
The artifact stores every release/profile edge witness and checks the population lock and available tarball
hashes, re-extracts every retained snapshot, rebuilds the relations, and verifies the colorings and cliques.

12     Go Direct-Method History and Deletion Counterfactual
Go keeps named types distinct while interface satisfaction follows method sets [The Go Authors(2026a)].
Frozen API ledgers expose Go 1.0–1.26 without rebuilding old toolchains [Go Project(2026)]. On the
cumulative nonempty direct-method population of 395 value forms and 759 pointer forms, the checked
historical union graphs require 41 permanent key values (40 extra names) for values and 76 (75 extra names)
for pointers. Each audit uses one pooled decoder namespace: package-qualified type names identify required
answers, but observations contain only direct-method signatures. Interface values can cross package boundaries,
so this decoder uses a global alphabet, unlike the TypeScript package-local decoders [The Go Authors(2026a)].
Thus each extra-name count subtracts one shared key because this decoder pools interface values across
packages. Retaining package paths in the observation would define a different graph. These history-wide key
counts equal the worst-release values. The ledgers do not reconstruct method promotion, so the history audit
is limited to direct methods.
     Identity and implementation supply have separate required relations. Embedding natively derives the field
and promoted-method incidences it carries; ambiguous or unembedded incidences still require forwarding or
another external authority. Structural interface satisfaction separately supplies no nominal provider. The two
prices accumulate on the relations for which each capacity is absent.

The Go 1.26.4 snapshot audit treats complete method sets as profiles. The empty method set is a genuine
profile; the nonempty columns isolate more informative collisions.

                             Form             entities profiles    Aπ bits nonempty Aπ bits
                             named value        2461       447 1751       11             59     6
                             named pointer      2218       994     782    10             75     7

Table 25: Method-set fibers in the audited Go 1.26.4 standard library. “Bits” is ⌈log2 Aπ ⌉ when the method
set remains visible.

    The deletion counterfactual uses terminal direct-method profiles, requiring 41 keys for values and 68 for
pointers. The complete-method profiles above include additional inherited methods and therefore define a
different observation. Hiding a method removes that observation from every fixed profile. Allowing at most
k hidden methods therefore gives the future-conflict graph under the stated downward-closed policy and
multiplicity cap. One hidden method preserves both minima; two raise them to 130 and 140.

                         Form                 current   k≤1        k≤2     k≤5       k ≤ 10   any k
                         named value              41        41      130        176     222     237
                         named pointer            68        68      140        235     320     434

    The values are reproduced from the generated artifact macros. Each entry has a weighted clique and
entity-level coloring of equal size. At k = 2, the value certificate contains 83 profile classes and the pointer
certificate contains 56; every selected profile exposes one method. Distinct selected profiles therefore differ in
exactly two coordinates, and the edge verifier checks the multiplicity condition for each canonical two-method
deletion.

13          What the Proofs and Executable Checks Establish
Answer counts do not depend on a source-length convention. Take m disjoint copies of the three-
consumer crossing witness. Additivity gives d↓ = m, yet one indexed rule computes the residual predicate for
every copy; only its parameter grows. A description language can also install that predicate as a primitive of
constant or zero charged length. Program-length bounds must therefore fix a language and size measure.
The answer-column quotient avoids that arbitrary choice: it is determined solely by the visible input and
required answer.99

What single-omission checks must exercise. Let G be a finite set of displaced dependencies. Suppose
each dependency can be present or absent independently. The grammar then contains exactly 2|G| omission
configurations, and only the complete set contains every required dependency. For each g ∈ G, the single-
omission mutant is G \ {g}. Any exercised set that detects every such mutant equals G. Thus complete
single-omission coverage exercises all |G| dependencies, while one test may exercise several dependencies.100
    Agreement in one complete routing setup is weaker than preserving answers through every supported
change. The latter quantifies over every reachable state and every configuration reachable on either side;
it therefore implies pointwise agreement. For residual routes it holds iff every incomplete topology is
unreachable.101
    Let E index semantic maintenance operations, and let touches(e) ⊆ G contain the dependencies established
or revised by operation e. If every operation touches at most b > 0 dependencies and their union covers G,
then                                                       
                                                        |G|
                                                |E| ≥         .
                                                         b
Combining this finite-cover bound with the ancestry-gap optimum gives at least ⌈d↓ /b⌉ operations for every
rewrite preserving required supply. If each operation changes one dependency, b = 1. An unrestricted edit
99   BWR8   100   BWR19-21   101   BWR37-41

may have b = |G| and satisfy the bound with one operation. This operation bound says nothing about source
length.102

13.1       Rules can supply future pairs without new manual wiring
A class-creation rule can generate bases or attributes. A lookup rule can compute an attribute on de-
mand [Python Software Foundation(2025c)]. An unchanged rule can handle later classes and updates from
their declared inputs. Count edits to that rule, changes to the generated graph, and changed required answers
separately. The staged-history theorem fixes classes and code providers: its n multi-parent additions remain
n graph changes even when one generator update produces them all. Generation shares the human edit
across those changes while the graph ledger records each edge. Rule evaluation, validation, and runtime costs
enter their own comparison.
    The finite-relation ladder already ends with a mechanism that realizes every required relation. Rules add
a second question: how often must the source change while those relations grow? Give generated ancestry
and programmable lookup the same rule inputs. For a finite provider set, a terminating rule that decides an
arriving class’s required providers can generate its direct provider edges. The construction preserves distinct
provider identities even when their current requirements agree.
Proposition 13.1 (One rule can generate exact ancestry at every input) 103 . Let g(s, x) give the
required provider–class relation from source s and input x. One fixed compiler applies the provider-preserving
direct construction to g(s, x), deriving exactly that relation at every input. If the compiled program retains
s, compilation preserves source-change events along every finite history. Finite provider and class sets give
finite generated graphs.
Proof. Use disjoint provider and class nodes, with a provider above a class exactly when g(s, x) requires the
pair. Retaining s makes the compilation injective in the source, so two consecutive compiled programs differ
in source exactly when their sources differ. Their generated graphs can change as x changes while s stays
fixed.
   The Lean result checks the relation construction and source-change equality. For a decidable finite table,
evaluating its entries materializes the graph. Python MRO, initialization, evaluation time, and refresh costs
require their own checks. Programmable lookup adds no sixth finite-relation rung: both mechanisms already
derive every finite relation. For an unbounded family, compare a lookup rule with an ancestry generator that
can create its future classes, rather than with a graph forbidden to grow.

13.2       When finite lookup code can generate the complete provider list
A program can look up one code provider without first finding every provider the class needs. Complete
generation has a stronger stopping requirement: it must return the whole list, including an empty list when
none is required. Fix the same class input on both sides. Number possible providers by natural numbers and
let L(x, p) be a total computable Boolean rule saying whether class input x requires provider p. Computability
here means execution by a finite program that terminates on every admitted input; no running-time or
source-size bound is imposed. A generator is exact when it returns a finite list G(x) such that p ∈ G(x) iff
L(x, p) = 1.
Proposition 13.2 (Exact condition for complete provider-list generation) 104 . A total computable
exact generator exists iff a total computable bound b(x) exists with
                                                     L(x, p) = 1 =⇒ p < b(x).
A fixed finite universe of possible providers supplies such a bound, uniformly over all future class inputs.
Conversely, membership in any computably generated complete list is a total computable lookup rule.
Proof. Given b(x), test L(x, p) for each p < b(x) and return the successful indices. Every test terminates and
the bound covers every required provider. Given an exact generator, take one plus its largest returned index,
or zero for an empty list. This is a computable bound. A fixed finite provider universe has a constant bound.
For the converse, generate the finite list and test membership. The same input x drives each construction.
102   BWR22-23, BWR8   103   RBF1-2   104   FDC1-3

The bound concerns which provider indices may occur. Knowing only how many providers a class needs
does not suffice, even when that number is at most one.
Proposition 13.3 (One possible answer per class still need not be listable) 105 . There is a total
computable lookup rule whose required provider set is empty or a singleton for every input, but no total
computable exact generator for those sets.
Proof. Let the class input be a program e. Bounded universal evaluation decides whether e(0) has succeeded
within fuel bound p. Define L(e, p) = 1 when evaluation first succeeds between bounds p and p + 1. Each
query runs two bounded evaluations and terminates. Success persists as fuel increases, so there is at most one
such p. A provider exists exactly when e(0) halts. An exact terminating list generator would decide halting
by testing whether its output is empty, contradicting the halting theorem [Turing(1937)]. The Lean proof
imports that undecidability theorem and checks the bounded lookup, uniqueness, halting equivalence, and
contradiction.
    Thus finite lookup code can describe an unbounded family of finite required sets that no terminating
complete-list generator computes uniformly. Each individual set still has a finite ancestry representation.
The obstruction is producing the complete representation from the class input before use. Allowing ongoing
enumeration or delegating unanswered queries to executable lookup changes that obligation. The result
compares total algorithms for the same relation; it supplies neither a source-length advantage nor a new rung
in the finite-relation ladder. It also leaves the Python runtime checks below distinct from the computability
proof.
    Python descriptors can themselves be inherited. Their lookup-time computation does not imply an
independently written route in each class. __getattribute__ customizes ordinary instance attribute access;
implicit special-method lookup can bypass it [Python Software Foundation(2025c)]. Here H records ancestry,
so computed routes absent from H remain in Rreq \ H even when one rule supplies them all. A comparison
that includes programmable lookup in its measured mechanism must recompute H and the outside count.
In either comparison, a rule needs inputs that distinguish every required answer; it cannot anticipate an
independently changed requirement from unchanged inputs.

Python correspondence. The artifact’s nominal_factorization/rule_based_factoring.py uses a
separate executable example with two workflow capabilities: selecting items and choosing their destination.
One family rule supplies both. Each arriving class has two declared Boolean requirements. The rule chooses
the corresponding providers; each provider owns one shared descriptor value. Table 26 compares four executed
routes through that same rule. The metaclass is also an ancestry generator, so the routes overlap in their
mechanisms.

Route                 How it supplies the required attributes      What changes for a new class
                      FamilyMeta computes provider bases dur-      The generated base list; the family rule
Metaclass
                      ing class creation.                          stays fixed.
                      A factory passes the same computed bases     The same base list, without a metaclass
Generated ancestry
                      to type.                                     hook.
                      A factory binds the selected shared descrip- Generated attribute bindings; the descrip-
Bound descriptors
                      tors in otherwise unrelated classes.         tor providers stay fixed.
                      __getattribute__ uses the class’s de-
                                                                   A new class and its request; no provider
Programmable          clared requirements to read the selected
                                                                   bases or per-attribute bindings.
lookup                provider.

Table 26: One family rule, four Python routes. All four preserve separate class identities and use the same
descriptor providers. Class creation and its required input still occur; an unchanged rule removes repeated
wiring edits.
105   FDC4-8

The tests cover all four requirement combinations, absent attributes, independent provider updates, and
256 new class identities per route without a rule change. In the first two routes Python inherits the descriptors;
the third binds those same objects directly. Consumer writes are rejected, keeping each value at its provider.
A separate test shows that a callable returned for an explicit obj.__len__() lookup need not make len(obj)
work. These executions check ordinary read-only attributes with nonconflicting names and immutable class
requirements. Changing an existing class’s requirements requires refreshing a creation-time result; on-demand
lookup instead reevaluates whichever inputs its rule reads. The executions accompany the abstract Lean
results; they do not mechanize the complete Python data model.
     Two further executed variants read changing instance requirements. One inherits conditional descriptors;
the other uses __getattribute__. Both switch attribute availability without changing class identity or bases.
The descriptor code is inherited throughout; its predicate decides whether the requested value is available.
Code reach and value availability are different queries. A creation-time ancestry example needs regeneration
to follow the changed requirement, and recreating its class changes identity. Proposition 13.1 preserves
abstract provider names; it supplies no live-object migration procedure.
     For chosen Rreq and a grouping of independently changing implementations, refactoring cannot change
d↓ : separate repairs and one indexed rule for m disjoint crossings both leave m answers outside ancestry.106

13.3       Exactly when the rule must change
Suppose one input appears twice but needs different answers. Keeping the same rule throughout that interval
is impossible. The converse also holds on a finite declared history: put a rule replacement inside every such
interval, and each remaining segment admits one lookup function.
Proposition 13.4 (Exact rule-revision count on a finite history) 107 . Fix inputs x0 , . . . , xn and required
answers a0 , . . . , an in a nonempty answer space. Allow arbitrary lookup functions rt with rt (xt ) = at . Count
a revision at t when rt+1 =   ̸ rt . For each i < j with xi = xj and ai ̸= aj , form the interval of possible revision
positions {i, . . . , j − 1}. The minimum revision count equals the fewest positions meeting every such interval.
In particular, one unchanged rule suffices exactly when equal inputs always require equal answers. If every
consecutive pair has equal inputs and different answers, the minimum is exactly n.
Proof. Without a revision between i and j, their rules are equal, contradicting the two different answers to the
same input. Conversely, cut at any set of positions meeting every conflicting interval. Within each segment,
equal inputs have equal answers; map each observed input to its required answer and give unobserved inputs
an arbitrary default. Use that function until the next cut. This proves both the allowed-position criterion
and equality of the minimum counts. In the last case every update position is required; storing the current
answer in a constant function attains n.
    The minimum permits a finite lookup table built with the whole declared history available. Distinct
inputs can therefore give zero revisions even when that initial table is large. The result bounds changes to
the function; it does not promise a short program or predict unseen requirements. A real fixed family rule,
as in the Python examples, supplies the stronger evidence that later answers follow from already declared
information. Adding the update number to the input alone does not supply that information for free.
    In the operation-cover model above, a fewest-edit repair cannot contain a dispensable edit. Every edit
is therefore the sole source of one dependency. Remove that edit and the dependency is uncovered. The
one-edit omission plans are pairwise distinct, so a fewest-edit repair with m edits has exactly m distinct
one-edit omission failures.108
    For a fixed residual predicate b, C/≡b is the least decoder state; different optimal repairs can induce
different b. Applying the construction to the complete relation gives repair-independent profiles ac ⊆ F .
Put [c] ⊑ [c′ ] when ac ⊆ ac′ ; every answer-preserving hidden-consumer encoding preserves enough decoded
answers to recover this order. Moreover, {f } ⊆ ac exactly when (f, c) ∈ R, recovering the canonical incidence
matrix.109
    Several class hierarchies realize this order. Ancestry supplying exactly the required pairs can chain
providers of equal or nested scopes and add harmless internal comparabilities. It natively factors R through
reachability. The typed direct multi-parent construction realizes every matrix; a one-parent factorization
106   BWR1-2, BWR8-9, BWR12, SCT24-25, SDN13-17   107   RBF3-6    108   BWR24   109   BWR14-18

exists iff the scopes are laminar. The factored relation and optimal residue d↓ are canonical. Concrete
hierarchies can refine that information object. An infinite relation has a finite implementation only through a
finite presentation or generator, which then owns the answers it produces. Open-ended systems instead form
unbounded sequences of finite states.

13.4       Checked kernels
The modules divide the proof work into identity, implementation supply, and shared infrastructure. Their
prefixes identify those responsibilities. Paper1IT contains identity-over-time kernels, Paper1PL contains
the OOPSLA-facing language and factorization kernels, and AbstractClassSystem supplies shared finite
observation and computability infrastructure. EvolvingObservation proves the future-conflict equivalence,
including the arrival-aware sentinel totalization onto a persistent carrier, greedy backdegree allocation,
refinement preservation, terminal-coarsening control, and the adaptive pair merge used by the coarsening
lower bound. HistoryViewDominance proves retained-name dominance across every indexed history, strict
collision bounds, helper non-recovery, and normalized identity recourse, using the existing boundary-intent
answer-preservation contract. FieldMenus proves the coordinate-and-parity field count, initial injectivity,
exact direction-pair branch fibers, and complete union conflict graph. The probabilistic O(k) argument
remains manuscript-level.
    OneDeletionStability proves the exact tight-contract result using a constructive parity-interval coloring
of weighted current profile classes. DelegationCompression separates fact-aware forwarding from fact-
oblivious holders, translates the latter exactly to and from all-ones rectangle covers, and injects every fooling
family into the active holder population. BehavioralRewrite proves the stronger quotient statement used
by the main text’s replacement-state result: when a decoder receives a fact and one residual code while the
nominal consumer stays hidden, equality of complete answer columns is the unique coarsest answer-preserving
identification. It also proves that every single-parent rewrite preserving required supply has ancestry gap at
least d↓ even when its residual answers are computed intensionally, and gives the formal counterexample to a
language-independent source-length bound. It also checks the canonical behavioral answer order, singleton
incidence test, and fixed-description-model minimality claim for the declared model.110
    RepresentationCanonicity groups states that permit exactly the same valid actions; this is the optimizer
quotient. It proves that an encoding preserving all valid-action answers must distinguish any two different
groups. For arbitrary binary answer columns, every answer-preserving projected carrier likewise refines
the canonical column quotient; minimal answer-preserving carriers induce the same partition, support the
same fiber-saturated answers, and have equivalent represented-state quotients. CarrierNonDichotomy then
separates the required possession relation from its carrier. In an exact native representation, required
implementation applicability (“should have”) is equivalent to implementation ancestry (native “has”). In
an exact routed representation, the same membership is equivalent to native ancestry or an explicit route.
Exact native and routed carriers therefore agree pair by pair, and an ancestry witness and the corresponding
possession hold simultaneously. More directly, ancestry derives supplied applicability by the native branch of
the supply mechanism, without using exactness. On every inherited pair, declaring ancestry and possession
mutually exclusive therefore contradicts that derivation. This is the category error in “is-a versus has-a”;
nominal membership and behavioral substitution are different relations.111 SemanticCompletionTax realizes
a complete implementation requirement set as one valid action and proves that its optimizer quotient is
exactly equality of required-provider columns.112
    SemanticCompletionTax also supplies the elementary positive-atomic consequence relation, proves exact
native supply equivalent to soundness plus completeness, and injects every native deficit into any outside
workaround preserving exact supply. Its finite specialization preserves duplicate provider scopes and equates
the least auxiliary decision count, the indexed laminar deletion minimum, and the independently defined
universal delegation minimum. It checks the strict contract-only-to-one-parent improvement on every
nonempty relation, the provider-preserving multi-parent/zero endpoint, and translates the dynamic signed
repair bounds into native deficit and excess.113
    NominalFactorization instantiates the observation, identity, ancestry, precedence, and population axes.
It checks structural collapse, the baseline-relative comparison, independence of nominal node identity from
ancestry, injectivity of answer-preserving providers on distinct applicability sets, both directions of the
110   BWR1-18   111   CND1-7   112   RCN1-11, SCT18   113   SCT1-32

ancestry-only/laminarity theorem, canonical multi-parent derivation, nonempty external repair for every
nonlaminar family, and provider-sensitive coloring of repair records. It derives a strict total provider order
from the stated choice axioms and, under the separate ancestry-compatibility premise, proves that the
order extends reverse ancestry. Applicability is represented extensionally as a set of distinct consumer sets.
Normalizing equal scopes and restoring their multiplicities as weights preserves the exact objective; the
executable audit expands the weighted witness back to typed provider and method facts.114
    ProductResolution checks the commutation criterion for provider-first and scope-first lookup, key-column
noninterference, and invariance under a declaration-empty nominal wrapper. It retains the selected provider–
scope site with provider and scope orders fixed. A wrapper that recomputes C3 uses a different contract.
IdentityDiscipline checks the fiber-union criterion, and BarrierComplexity checks finite collision search,
the total-observer clock construction, and the many-one reductions. The arithmetical-hierarchy names and
Python’s complete C3 algorithm are outside the Lean boundary.
    PartitionStateThreshold checks that one-state tag classes are independent in the induced partition
matroid, that two-state adequacy is common independence and hence the matching condition in the bipartite
incidence multigraph, and that the three-state four-declaration witness has exact snapshot price two and
union lower bound four. The general equality χ′ (H) = ∆(H) for bipartite multigraphs is cited as external
Kőnig background. Lean proves the reduction through the bipartite matching condition but does not import
that equality, so no numbered result’s handle depends on it.

                     Priced resource         Upper witness            Lower witness
                     Permanent tags          proper coloring          clique
                     Delegation decisions    laminar retained family disjoint local conflicts
                     Fact-oblivious holders rectangle cover           fooling family

13.5       Executable audits
The Go programs reconstruct snapshot and cumulative direct-method profiles, retain departed endpoints,
build the union graphs, and export proper colorings. The adversarial companion evaluates seven deprecation
budgets and exports profile classes, admissible edges, weighted cliques, and entity colorings; a standard-library-
only verifier rebuilds every edge and checks the matching certificates. In the Python study, retained-laminar
families are directly checked upper witnesses, bounded FPT replay supplies independent exact lower bounds
through ki ≤ 10. Lean-checked LRAT refutations supply exact lower bounds for the 7 larger primary or
Closed-C3 components and the 59-holder cover. The Protocol Buffer miner asks protoc for descriptors
at every measured mainline commit and separates deletion, reservation, reuse, redefinition, and message
removal. The Python audit resolves the 20 pinned public class graphs selected by the locked population
screen, computes the minimized single-parent gap component by component, and emits independently checked
laminar-repair upper witnesses together with local-conflict, rectangle-cover, and fooling-family certificates.
The TypeScript audit parses declaration closures without executing packages, retains release/profile witnesses
for every history edge, and checks matching coloring and clique certificates while keeping runtime-backed
and declaration-only evidence separate. All four studies pin their source artifacts by repository revision or
registry hash and export machine-readable records, generated LATEX counts, and focused tests.

14       Representative Language Encodings of the Factorization Gap
Language mechanisms answer several different questions: whether an object exposes a member, which
declaration it denotes, where an implementation came from, which declarations satisfy a contract, and which
definition wins. The same-looking-group criterion applies separately to each question below.
114   NHF35-40

Capability            Example query                  Identity-dependent?               Required information
Conformance           Does x expose method m?        No, if constant on profile fibers structural profile
                      Is x the declaration selected
Identity                                            Yes for structural clones          nominal handle
                      earlier?
Provenance            Which declaration supplied m? Yes                                nominal provider plus ancestry
                      Which declarations implement
Enumeration                                        Yes in an open population           nominally scoped registry
                      T?
                                                     Yes when clones have different qualification, rejection, merge,
Resolution            Which definition wins?
                                                     providers                      or precedence

    For implementation supply, ask whether the native mechanism records which code provider supplies each
class. Ancestry retains that provenance through its paths. Surface syntax does not add a rung. A trait, module,
or effect system that retains several provider edges is constrained multi-parent ancestry; a checked impl table,
registry, embedded-field remainder, generator, or forwarding layer that stores individual incidences outside that
graph is an outside workaround. Checking can establish exact supply, but it does not erase the answers it owns:
one-parent ancestry still leaves at least d↓ , and no implementation ancestry leaves |Rreq |. At a crossing, pre-
serving required supply and behavioral correctness still leaves a positive outside count under sound one-parent
ancestry, so it does not attain correct maintenance. Formally, soundness plus an empty native deficit forces
the carrier’s native scope to equal Rreq . The main paper’s complete-supply characterization gives a provider-
preserving multi-parent presentation of the same relation. Different graphs can realize that relation; no unique
graph topology is claimed. A carrier that computes incidences outside native provenance is therefore a separate
workaround authority: it still owns the required incidences absent from native provenance.115 The class and
trait entries follow their language references [ISO C++ Working Group(2023b), Ecma International(2006),
ANSI Common Lisp(1996), Python Software Foundation(2025c), Barrett et al.(1996), Scala Project(2026a),
Scala Project(2026c), Scala Project(2026b), R Core Team(2026)]. The interface and protocol entries follow
their own specifications [Oracle(2025e), Microsoft(2026a), JetBrains(2026), Rust Project Developers(2026e),
Apple(2026b), Apple(2026a), The Go Authors(2026b), Microsoft(2026f), Microsoft(2026e), Marlow(2010)].

                         Table 27: Representative native carriers for implementation applica-
                         bility. These carriers locate the coordinatewise correct-maintenance
                         capacities in the main-text mechanism table; precedence and separate
                         answers remain part of each placement.

                      Native applicability Code and state         Precedence or
       Mechanism                                                                      Separate relation
                      carrier              supplied               constraint
                                            Virtual bases share   Final-overrider
                                            one base subobject;   rules, ambiguity    The graph is native;
                      Multiple nominal
       C++ classes                          nonvirtual bases      rejection, and      shared-state identity is
                      bases
                                            retain distinct       explicit            chosen per base edge.
                                            subobjects            qualification
                                                                 rename,              Retained parent clauses
                      Multiple nominal      Features and state redefine, and          carry derivation;
       Eiffel classes
                      parents               from several parents select adapt         adaptation records only
                                                                 conflicts explicitly genuine clashes.
                                                                                      The class graph retains
                                                                 Class precedence
                                            Slots and generic                         membership;
       CLOS           Multiple direct                            plus method
                                            methods are                               generic-function methods
       classes        superclasses                               selection and
                                            organized separately                      retain provider
                                                                 combination
                                                                                      applicability.

115   SCT37, RCN2, SCT18, NHF50

Native applicability Code and state         Precedence or
Mechanism                                                                      Separate relation
               carrier              supplied               constraint

                                   Methods and                                 Facts derived from
                                   descriptors;                                declared bases need no
                                                           C3 linearization
Python         Multiple nominal    cooperative                                 second applicability
                                                           and cooperative
classes        base edges          initializers may                            carrier; population
                                                           super()
                                   establish state on                          enumeration remains
                                   the shared instance                         separate.
                                   The object contains
                                                                               Class metadata carries
                                   inherited slots;        S4 method
                                                                               multiple superclass and
               Multiple contains   generic methods         selection and
R S4 classes                                                                   state relations;
               superclasses        apply indirectly        compatible-slot
                                                                               generic-function tables
                                   through superclass      constraints
                                                                               carry method bodies.
                                   information
                                                                            Trait-owned facts remain
                                   Concrete members,                        in the parent graph;
Scala 3        Multiple nominal
                                   values, and          Trait linearization type-class given
traits         traits
                                   parameterized traits                     instances instead store a
                                                                            separate incidence.
                                   Default method                               State-bearing facts and
                                                           Most-specific
Java           Multiple nominal    bodies; no                                   population indices
                                                           override, rejection,
interfaces     interfaces          per-instance                                 require another provider
                                                           or qualified super
                                   interface state                              or record.
                                                                              State and noninterface
                                   Default member          Most-specific
C#             Multiple nominal                                               applicability remain
                                   bodies; no instance     implementation or
interfaces     interfaces                                                     outside interface
                                   fields                  compile-time error
                                                                              ancestry.
                                   Method bodies and
                                                           Required override Stored state and omitted
Kotlin         Multiple nominal    computed
                                                           and qualified     applicability decisions
interfaces     interfaces          properties; no
                                                           super             remain external.
                                   backing state
                                   Default methods;        Coherence,          Trait admission and
               Native impl Trait autoderef supplies        orphan rules,       target supply differ:
Rust traits    for Type incidence; target fields and       visibility, and     dereferencing does not
               one Deref target    methods along one       receiver            transfer the target’s trait
                                   chain                   constraints         memberships.
                                                                            Instance declarations
             Multiple superclass                         Superclass         own admission;
                                   Default methods
Haskell 2010 constraints and                             instances must     superclass entailment
                                   from several classes;
type classes explicit class–type                         exist; at most one derives contract
                                   no instance state
             instances                                   matching instance consequences. Stored
                                                                            state remains separate.
                                                           Requirement         Conformance
                                   Protocol-extension
                                                           dispatch and        declarations and
Swift          Multiple protocol   defaults; class state
                                                           specialized         extensions store the
protocols      conformances        stays in the class
                                                           constrained         incidence separately from
                                   hierarchy
                                                           extensions          single class ancestry.
