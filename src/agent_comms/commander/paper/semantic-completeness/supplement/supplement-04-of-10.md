# Supplement to Semantic Completeness for Correct Maintenance: part 4 of 10

Optional reading: the paper1 supplement converted to layout-preserving text, page by page. Figures are PNG files in `figures/`.

8       Lookup Order, State Sharing, and Initialization
Established inheritance semantics. Cook and Palsberg model a class by a generator G from self to a
method record, and a subclass by a wrapper W taking self and super [Cook and Palsberg(1994)]:

                                             (W ▷ G)(s) = W (s)(G(s)) ⊕ G(s).

Here ⊕ is left-biased record merge; the object is the least fixed point of the combined generator. Their
equivalence proof connects this model to method lookup. Passing the same self to both generators explains
the self-call default below; our result counts departures from that default. For supply, a single parent gives
each code provider a descendant subtree, so the class groups nest or do not overlap. The ancestry gap prices
required pairs that no such tree derives (main-text Theorem 4.6).

Supply and winner selection count different answers. Fix a required coverage relation, a conflict set
of size k, and its winner policy P . Every carrier preserving the specified lookup answers realizes the same k
winner answers. Multi-parent derivation adds no outside coverage answer, so its combined coverage–resolution
cost is k under any resolver preserving those answers. Every one-parent repair preserving the required
coverage and lookup answers retains those k answers and adds at least d↓ outside coverage answers, for cost
at least k + d↓ . At k = 0, no precedence mechanism is needed: multi-parent coverage costs zero while every
positive-gap one-parent rewrite still pays d↓ . Candidate supply does not appear in the resolver theorem
below; precedence is an independent mechanism, and MRO is its ancestry-derived instance.78

One graph can couple otherwise separate relations. When one parent graph carries contract
membership and implementation reach, the union of their native row families must be laminar. Restricting
any joint repair to either relation remains admissible, and the omitted-pair counts add; hence the joint
optimum is at least the sum of the independently chosen optima. For a strict witness, let the contract relation
have the single row {W, FR} and implementation reach the single row {FR, B}. Each relation alone has gap
zero, while their shared graph has gap one. If one declaration itself carries required sets A and B for the two
roles, one sound native descendant set lies in A ∩ B. Retaining that intersection attains the exact outside
floor |A △ B|.79

Self-use reverses the native winner default. For a fixed declared self-call site set, open recursion
defaults each site to the derived implementation; a closed wrapper defaults it to the base implementation. A
repair preserving the specified self-call winners must materialize every site where the required winner differs
from that default, and the canonical repair materializes exactly those sites. The subclass therefore has ledger
(0, |P −1 (base)|), while a wrapper around a provider exposing E has (|E|, |P −1 (derived)|). If E is nonempty,
the wrapper never dominates on both coordinates. The subclass dominates exactly when its base-winner
count is no larger; otherwise the ledgers are incomparable.80
     The comparison requires a declared self-use contract. In Bloch’s counting-set example, a base-winner
addAll→add site costs the subclass one exceptional winner answer; the wrapper avoids that answer but routes
every exposed member, so neither ledger dominates. If add is instead a declared extension callback, the derived
winner is native to the subclass. When self-use is undeclared, a base revision may add sites whose required
winners were never stated; that is the fragile-base-class boundary [Bloch(2018), Kiczales and Lamping(1992),
Mikhajlov and Sekerinski(1998), Aldrich and Donnelly(2004)].

Coherent resolution is one precedence order. Let a resolver select one member of every finite nonempty
provider set and preserve its choice whenever the selected provider survives removal of rejected candidates.
Define a ≺ b when the resolver selects a from {a, b}. Pairwise choice gives irreflexivity and totality. Choice
preservation gives transitivity and places the selected member of any finite set before every rejected member.
The resolver therefore selects the first member of one strict total order. If every ancestor–descendant pair
selects the descendant, this order extends reverse ancestry.81
    Configuration lookup can order both providers and context scopes. Searching providers first need not
select the same declaration as searching scopes first. The criterion below says exactly when the loop orders
78   NHF53-58, NHF64-68, NHF10, NHF19   79   CPL1-4   80   SLF1-7   81   NHF10, NHF19

agree. Fix one semantic key k, let Dk ⊆ T × S be the nonempty set of sites that define it, and order provider
types T and scopes S from most to least specific. Write t⋆ and s⋆ for the coordinate minima. Provider-first
and scope-first lexicographic resolution select the same provenance site exactly when (t⋆ , s⋆ ) ∈ Dk . If their
choices agree, each loop order forces one coordinate of the common site to its minimum; conversely the joint
minimum is first under both orders.82
    Changing other key columns leaves Dk fixed. With both orders and all existing declaration sites fixed,
inserting a provider that declares no keys also leaves provider-first resolution unchanged.83 A source-language
wrapper that recomputes C3 can change the provider order and lies outside the latter claim. For the crossed
pair {(t0 , s1 ), (t1 , s0 )}, the two loop orders disagree; the absent joint minimum is exactly why axis priority
becomes observable semantic policy.

8.1      State sharing and cooperative initialization
Two routes may be required to see the same state object or distinct objects. Constructors may be required to
initialize one component before another. Every representation must preserve the required state sharing and
initialization order; its native rules determine which requirements need additional code. Fix a class c. A state
access (f, p) means code provider f ’s code accessing provider p’s state in an instance of c. A partial map σc∗
assigns same or distinct to specified unordered pairs of accesses. A finite relation ≺∗c specifies which state
components must initialize before which others. Preserving these sharing and ordering requirements requires
                                           ∗
the realized sharing to Pagree on ∗dom σc and P  the realized schedule to contain ≺∗c . Unspecified pairs are not
counted. Write kσ = c | dom σc | and k≺ = c | ≺∗c |.
Theorem 8.1 (Specified state answers survive answer-preserving refactoring). Every representation
preserving the specified state answers supplies the same kσ + k≺ specified state answers, with multiple parents,
one parent, or no inheritance. When the specified lookup answers are also preserved, the combined count is
k + kσ + k≺ .84
Proof. Answer preservation fixes each specified answer; the specification fixes the questions. Sharing queries
and initialization queries are separate types in the checked model, so no query belongs to both and their
counts add.
   The specification fixes these required questions; source edits and input bits depend on how their answers
are encoded. The native and outside parts can differ. A state requirement can constrain feasible supply
designs, so joint attainment of the unconstrained supply minimum needs a compatibility argument.
Lemma 8.2 (Cooperative initialization follows and reverses the MRO) 85 . Suppose each participating
initializer terminates normally and executes pre; super().__init__(); post, calling the next participating
initializer exactly once. The pre segments run in MRO order; the post segments run in reverse order. For
components s, t in distinct providers, each placed once in its provider’s pre or post segment, s initializes before
t iff:
     1. s is pre and t is post;
     2. both are pre and s’s provider precedes t’s;

     3. both are post and t’s provider precedes s’s.
Proof. Induction on the participating MRO suffix gives the trace pre(L) ++ post(reverse L). A provider at
rank r in a chain of length n has pre rank r and post rank 2n − 1 − r. Comparing these ranks gives the three
cases.

   Lean checks this list model and rank comparison. Python’s correspondence is external: super()
starts searching after the calling class in the instance’s MRO [Python Software Foundation(2025b),
Barrett et al.(1996)]. Initializers that skip, repeat, or conditionally select calls need a different execution
model. The replay script proofs/checks/state_coordination_replay.py checks all four placements and
both provider orders in Python.
82   NHF16   83   NHF17-18   84   SCI1-3, SCI11, NHF53-58   85   SCI4-5

Proposition 8.3 (A shared-state default prices distinct-state exceptions). Restrict to specified queries
whose two accesses name one common provider. Suppose accesses through ancestry share that provider’s
state. If a query
             P requires distinct state, at least one access must depart from that default. Thus at least
kσ,distinct = c |{q ∈ dom σc∗ : σc∗ (q) = distinct}| sharing answers are determined outside that default. A
field-only composition baseline with no native sharing rule for these queries determines all kσ answers through
construction and reference passing.86

Proof. Two native accesses would give same, contradicting the required distinct. The distinct-query set is
therefore contained in the outside-default query set. If no query has a native answer, that set is the whole
specified domain.
    Python’s ordinary instance namespace does not create a separate subobject for each diamond
route. C++ shares a common virtual base subobject; repeated nonvirtual bases can instead be dis-
tinct [Python Software Foundation(2025c), ISO C++ Working Group(2023b)]. The lower bound follows
from the shared default; nonvirtual C++ arrangements can derive distinct state directly. Sharing queries
and implementation pairs have separate counts; attaining kσ,distinct jointly with a supply optimum requires
a separate compatibility argument. Language-native delegation or state-sharing mechanisms are not the
field-only baseline.
Proposition 8.4 (Lookup and initialization can require one outside answer). Let distinct providers
L, R both define method m and own a state component. Two classes c1 , c2 both require L’s method. The first
requires L’s state before R’s; the second requires the reverse. Fix each provider’s placement as pre or post
under Lemma 8.2. Over both base orders and all four shared placements, the minimum number of lookup or
initialization answers supplied outside the defaults is exactly one.87
Proof. Without an outside lookup answer, first-definition lookup puts L before R in both classes. Every
shared placement then gives both the same initialization order, so at least one required answer needs repair.
Lean checks every placement, base order, and subset of the four possible lookup/initialization repairs. For
attainment put both components pre, use c1 (L, R) and c2 (R, L), and declare m = L.m in c2 . This one lookup
answer restores the required winner while the two MROs derive opposite initialization orders.
    The Python replay checks this attaining design. Exhausting this finite witness establishes its exact
minimum; computing the minimum for other inputs needs their own search. Composition can state the orders
and lookup winners separately. C++ likewise separates its base-construction order from first-definition lookup:
direct bases initialize in declaration order, virtual bases in traversal order, and genuinely ambiguous member
lookup requires explicit resolution [ISO C++ Working Group(2023a), ISO C++ Working Group(2023b)].
Corollary 8.5 (State requirements preserve capacity containment). If adding multiple parents
preserves every old design’s supply, sharing, initialization, and lookup answers, every feasible one-parent or
composition design and its outside-answer ledger remains feasible. Adding capacity cannot worsen the optimum
with the state specification fixed. Existing strict crossing witnesses survive by taking the state specification
empty; inheriting one parentless provider into one class saves one supply answer against no-inheritance supply
without adding a state answer.88

Proof. The conservative embedding retains old feasible designs and their complete answer ledgers. The
main-text capacity-monotonicity lemma gives monotonicity. The existing crossing and nonempty-supply
constructions give strict witnesses.
   State requirements therefore add answers while capacity containment retains every old feasible design. A
ban’s justification still requires the derived exclusion tested in the main-text language-design discussion.

Measurement                  Fixed input                                          Result and boundary
                                                                        preserves the observed provider-bundle map;
                             source-closed classes with unique concrete excludes external bases, descriptors, ambigu-
Python provider gap
                             providers                                  ous providers, custom MROs, and runtime
                                                                        mutation
                                                                         pair costs do not equal source declarations;
                             same extracted map, with method roles, pair
Python sensitivities                                                     method-level and removal sensitivities retain
                             removal, or tolerated over-reach
                                                                         their stated solver intervals
                                                              its 71 conflict sites and 139-pair exact gap
SQLAlchemy Closed- fully local 92-provider, 183-consumer sub-
                                                              are independently certified; bases outside the
C3                 component with 1,173 pairs
                                                              parsed wheel remain excluded
                                                                    measures recurrence under that policy; the
SQLAlchemy longitudi- six pinned releases under the source-declared
                                                                    source nodes can change, while the growth
nal workaround map    first-parent policy
                                                                    theorem fixes its nodes
                                                                                  does not recover semantic scopes, forward-
History screen               direct-base syntax in 54 commits                     ing, registries, generated code, or continuity
                                                                                  across renames

                     Table 8: Fixed inputs, evidence, and boundaries of the inheritance audits.

9      Additional Audit Detail
9.1      SQLAlchemy’s largest independently certified component
This audit establishes two different minima: missing implementation–class pairs over every sound one-parent
hierarchy, and reusable forwarding bundles for one recorded optimal hierarchy. The second does not minimize
jointly over all optimal hierarchies.
    The component contains 223 implementation providers and 4,831 required dependencies. Its exact
optimum for the extracted provider relation is 520, spread across 61 providers and 256 classes. The optimized
gap has 80 distinct answer patterns; the complete SQLAlchemy repair has 90. For the pinned repair H,
its gap matrix has exact Boolean rank, equivalently minimum biclique cover bc(Rreq \ H), 59 [Theis(2011)].
The report enumerates all 109 maximal all-ones rectangles, solves their exact set-cover problem, stores the
selected cover, and verifies that the objective and solver bound both equal 59. Each holder is selected before
the requested implementation is known and supplies an entire rectangle of missing pairs. Minimizing those
holders groups the 520 missing implementation–class pairs without reducing their count.
    Every holder in a minimum cover supplies a pair no other holder supplies; otherwise it could be removed.
These private pairs distinguish all subsets of enabled holders, even when the holders overlap elsewhere. Thus
a minimum cover of size b gives 2b distinct workaround maps, and only the full subset covers the gap. Lean
checks the private-pair argument, injectivity, count, and unique complete subset. Applied to this fixed repair,
b = 59.89 All these maps are reachable only if the program permits every holder subset. The cover certificate
establishes the minimum cover. Reaching every holder subset additionally requires independent controls; a
generator can exclude incomplete configurations. The 59-holder minimum fixes H and does not optimize
holder count over alternative hierarchies.
    Three large scopes provide a compact crossing witness. ColumnOperators, Executable, and FromClause
share 53 consumers while separately reaching 71, 61, and 17 consumers. Their pair-only regions are all zero.
Hence every pair intersects, and each provider also reaches a class the other does not; no pair of scopes nests.
    The protocol selected the first five displaced dependencies in lexicographic provider–consumer order.
The table lists every concrete method attributed to the provider at that consumer. A direct forwarding
replacement must name these targets or invoke a mechanism that derives the same answer.

86   SCI6-8   87   SCI9-10   88   OPT20, OPT17-19, CSD1-8, SCT23, SCT30-34   89   HCC1-4

Table 9: Five source-anchored displaced dependencies from the pinned
                      SQLAlchemy repair.

  Implementation provider             Receiving class                    Inherited methods
                                      sqlalchemy.dialects.oracle.
  sqlalchemy.dialects.oracle.                                            _cx_oracle_outputtypehandler,
                                      cx_oracle._OracleBINARY_
  cx_oracle._OracleNumeric                                               bind_processor
                                      DOUBLE
                                      sqlalchemy.dialects.oracle.
  sqlalchemy.dialects.oracle.                                            _cx_oracle_outputtypehandler,
                                      cx_oracle._OracleBINARY_
  cx_oracle._OracleNumeric                                               bind_processor
                                      FLOAT
  sqlalchemy.dialects.                sqlalchemy.dialects.
  postgresql.named_types.             postgresql.named_types.            _check_for_name_in_memos
  NamedType                           DOMAIN
  sqlalchemy.dialects.sqlite.         sqlalchemy.dialects.sqlite.        __init__, format_is_text_
  base._DateTimeMixin                 base.DATE                          affinity
  sqlalchemy.dialects.sqlite.         sqlalchemy.dialects.sqlite.
                                                                         format_is_text_affinity
  base._DateTimeMixin                 base.TIME

    Each row shows what a direct encoding inside the optimized one-parent repair must supply: every method
in the rightmost cell to the receiving class in the middle cell, with the leftmost class remaining its authoritative
implementation source. An adapter or generated shim can supply the same bindings, and then that mechanism
owns their continued presence. The rows come from the bundled repair; SQLAlchemy itself is unchanged.
    Each row is regenerated from the wheel source and the pinned repair. The report stores both declaration
paths and line anchors; its independent verifier reparses the pinned wheel and rejects any changed pair,
method set, or source digest. Under the explicitly independent configuration policy, the 520 displaced
dependencies admit 2520 possible subsets. This count describes the replacement boundary’s worst-case state
space. The independently checked feasible repair and the 80 answer patterns remain the measured quantities.
An independently generated LRAT refutation, checked by Lean, proves that no smaller repair exists.

9.2    Workaround changes under the declared first-parent policy
This audit follows one concrete policy: keep each class’s source-declared first parent and supply its missing
pairs outside ancestry. It reconstructs that hierarchy at 6 pinned SQLAlchemy releases, from 2.0.0 through
2.0.52. All 5 adjacent transitions edit the workaround map. Across transitions it contains 511 outside-pair
additions or deletions, including 94 on pairs required on both sides of the release boundary. The 511 edit
total and the equal first-release gap below are independently reconstructed quantities. Required pairs grow
from 5,515 to 5,813, outside pairs from 3,418 to 3,591. Separately, the exact snapshot gap grows from 511 to
547. Removable arrangement excess therefore changes from 2,907 to 3,044.
    The lock records every wheel digest, recovered relation digest, optimum witness, and adjacent set difference.
Its verifier reparses all six wheels, reconstructs every map, checks each witness, and rejects any changed count
or digest. This measures recurrence under one source-declared policy. It does not infer why any edit occurred,
and its source carrier changes, whereas the main paper’s fixed-carrier corollary holds that carrier fixed.

9.3    Preregistered strict-insertion history audit
The protocol fixes pip and aiobotocore, the two projects with zero gap when methods are grouped by declaring
class, before extraction. It also fixes aiobotocore’s separate-method input. For each project it selects every
non-prerelease, non-yanked release with a universal pure-Python wheel uploaded in the 365-day window
ending at the population’s pinned wheel, and no version later than that endpoint. When several wheels
qualify, the filename’s lexical order selects one. This gives 8 pip releases ending at 26.2.1 and 22 aiobotocore
releases ending at 3.9.0.
    The registration was committed before extraction at 47cdbf76. strict_insertion_protocol.lock.json
records the UTC date, window, wheel URLs and SHA-256 hashes, analyzer fingerprint, and Python interpreter

binary hash. Its SHA-256 is c37dd3975b5886822f2b5337905dc961aa71263b66cb3b99efa3599e93d45787.
No additional projects or components were admitted after seeing results.
    Extraction reuses the population audit’s MRO-invariant projection. Provider rows and classes match
by fully qualified name, without rename heuristics. Only provider rows present in both versions enter a
transition’s search; their class groups are restricted to declarations present at both versions. Both full
recovered snapshots must first be tree-shaped. The search records every triple satisfying the mixed-update
conditions in §4.3, including a nonempty new lower group; growth is not required. It flags any triple touching
a class whose extraction eligibility changes. The relation contains resolved declarations only; unresolved
program requirements must be recovered before their gap can be classified.
    All registered snapshots are tree-shaped. The 7 pip transitions and 21 aiobotocore transitions contain
zero triples, under both registered aiobotocore inputs. The null result therefore supplies no strict-insertion
witness in these histories. It does not classify other histories or rule out repair caused by other updates.
    python_strict_insertion/report.json retains every recovered snapshot, shared carrier, unmatched
declaration, eligibility change, and triple list. Its derived audit.md gives the per-transition table and wheel
hashes. An independent verifier reparses the raw hashed wheels, recomputes tree shape and exhaustive triples
without importing the producer’s search, and agrees on all snapshots and transitions. With no measured
triples, there are no measured witness declarations to add to the claim map; the general mixed-update result
is checked by SIN4 and SIN5.90

9.4      Pinned pyapp-kit portfolio
The portfolio rule selects all nine libraries named on the pyapp-kit landing page at commit cdd24545f564.
Each repository is pinned before the class graph is parsed. The MRO-invariant projection resolves local bases
and retains only a unique concrete implementation provider. Multi-parent classes with an unresolved direct
base remain in the syntax count and in the external boundary column; they cannot contribute implementation
dependencies to the recovered local implementation–class relation.

                  Project     classes syntax MI external MI impl.-MI dependencies gap
                  magicgui       171          41           19         11           386    19
                  superqt         74          11            7          0            24     0
                  psygnal         61           5            3          2            40     0
                  in-n-out         5           0            0          0             0     0
                  app-model       59          13           13          0            19     0
                  fieldz           6           1            1          0             0     0
                  scenex          95          20            6          0            82     0
                  cmap            12           0            0          0             0     0
                  ndv            118          10            7          2            61     6

Table 10: All libraries on the pinned pyapp-kit landing page. “External MI” counts multi-parent classes
with at least one unresolved direct base. The exact gap prices the source-local provider map and therefore
excludes those external branches.

   Across 461 Python files and 601 classes, the screen finds 101 syntactic multi-parent declarations, including
56 at an external boundary. The source-local relation has 15 implementation-MI classes, 612 dependencies,
and exact gap 25. An independent rerun checks every repository commit, rebuilds every class graph, and
byte-compares the report.
90   SIN4-5

9.5    History screen for MI-to-SI rewrites
The history study searches for a directly visible change: a class drops from multiple declared bases to at most
one. Its protocol selects the five largest positive provider-bundle gaps from the locked Python population,
then follows the mainline history to the pinned release. A candidate keeps the same qualified class name and
path while its direct-base list falls from at least two entries to at most one. The scan records the commit,
subject, line anchors, and both base lists.

                         Project          candidates commits parse failures fallback
                         SQLAlchemy                82          41            401         0
                         redis                      9           4                9       0
                         anyio                      6           4               0        0
                         fonttools                  2           1               98       0
                         azure-identity             8           4                0       0

Table 11: Locked mainline syntax screen for the five largest positive provider-bundle gaps. The scan finds 13
relocated nominal factors in total; the table reports candidate, commit, parser-failure, and fallback counts by
project. Historical files outside the parser model remain counted as parse failures.

   The scan finds 107 candidates in 54 commits, including 13 relocated nominal factors, and records 508
parse failures. The candidates are evidence about the observable class syntax. Hidden forwarding, registry,
and generated-code rewrites are not measured by this base-list screen. The report is pinned to the protocol
digest and every repository commit; the independent structural check recomputes the project totals, candidate
counts, and source-path constraints.
   A separate SQLAlchemy fix changed collection instrumentation to follow Python’s MRO after unpredictable
subclass choices [Bayer(2013)]. The case separates the two resources used in the main analysis: ancestry
supplies implementations, while MRO orders overlapping providers.

9.6    An end-to-end AnyIO refactoring decision
This worked application starts with public clients, fixes which code provider changes must reach them, and
uses the analyzer to choose a preserving refactoring. It uses AnyIO 4.14.2 at commit c384f99687c64c59ed8a
11c3a0f11a2d57daff71; the controlled source copy supplies refactoring evidence while the population still
counts AnyIO once. The artifact file practical_impact/ANYIO_REFACTORING.md supplies commands and
source anchors. The runner checks the commit, unchanged tracked sources and tests, and the source actually
imported by Python.

Clients and requirements. FileReadStream reads bytes, seeks, and reports its file position;
FileWriteStream writes bytes. Their common _BaseFileStream owns initialization, closure, and typed
file metadata. BufferedByteReceiveStream can wrap an in-memory receiver and has its own buffer and
lifecycle code. It shares concrete async-iteration methods with the file reader through ByteReceiveStream.
The unchanged upstream suite exercises these APIs; the added application client writes a temporary file,
reads and iterates it, checks its metadata and closure, and iterates a separate memory stream.

                Required provider                        File reader   File writer   Buffered reader
                File initialization, closure, metadata        yes         yes                no
                Byte-receive iteration                        yes          no                yes

   The two exclusions have operational meanings. The write-only stream must not gain the receive/iteration
API. The memory-backed buffered reader must not acquire an independent file handle or execute the file
provider’s lifecycle. It may still forward metadata from a wrapped file; that does not make it the provider
of a separate file. Effective method lookup, including inherited methods and overrides, confirms every

cell. The extraction compares actual source-member identities, so a selected base counts only through the
implementation it supplies.
     The maintenance contract retains the three public class names and their distinct meanings, public
receive/send membership, factories, instance APIs, exceptions, typed metadata, and each selected imple-
mentation’s source and receiver. File-lifecycle and byte-iteration bodies may change independently while
retaining their member names and descriptor kinds. Replacement happens before client construction. Private
file-base identity tests, existing-instance migration, custom MROs, and cooperative-super extensions lie
outside this application; the transformer rejects methods with __class__ closures. New member names
require regeneration and validation.

What the analyzer changes. All three crossing regions have size one. The analyzer returns |Rreq | = 4
and d↓ = 1, checked by a feasible repair and a matching conflict-packing certificate. The two-role formula
gives the same minimum. Its implementation-only objective permits several optimal repairs; the application’s
public membership obligation selects one that retains ByteReceiveStream natively. Removing the private
file provider from the reader leaves three native pairs and one outside pair. The artifact checks this chosen
repair against the original relation, independently of which optimum the solver first returns.
     The following shows the changed declaration. The original factory, receive, seek, and tell method
objects remain unchanged; the executable transformer derives the forwarding members from the provider’s
own declarations.
# Before : FileReadStream ( _BaseFileStream , ByteReceiveStream )
class FileReadStream ( ByteReceiveStream ):
    __init__ = live_member ( _BaseFileStream , " __init__ " )
    aclose = live_member ( _BaseFileStream , " aclose " )
    extra_attributes = live_member ( _BaseFileStream , " extra_attributes " )
    # Original factory , receive , seek , and tell retained unchanged .

live_member reads the current provider member at lookup time and binds the original stream as its receiver.
Its property variant retains read-only data-descriptor behavior. No provider body or file state is copied. Both
providers remain independently owned, and updates to existing members need no additional route edit. A
captured-method assignment is tested separately: it retains the old function after replacement, while live
forwarding uses the new one. The generator supplies the one missing provider–reader pair. This realizes the
selected relation with one-parent reach; other AnyIO classes and providers remain unchanged.

Executable checks. Separate processes run the full unchanged upstream suite on the native and forwarded
reader. Each passes 3,934 cases, skips the same 57, and has the same 5 expected failures. The runner
compares every individual outcome. The 16 added tests check the exact relation, attaining repair, method
sources and receivers, public membership, property write protection, real file/memory clients, and independent
provider updates. All four combinations of file and iteration body replacement are exercised under both
representations, with identical affected client sets.
    The omission check executes all eight subsets of the three generated bindings. Only the full subset passes
the same read/metadata/close client; the other seven fail construction, abstract-method, or metadata checks.
These three member bindings implement one provider–client pair. The report keeps those units separate and
saves source hashes, test logs, JUnit outcomes, and an independently verifiable gap certificate. The finite
relation minimum is exact. Source checks and executions support the stated Python behavior; Lean certifies
the relation minimum.

9.7    Displaced-dependency mutation study
The mutation study uses the smallest archived wheel by byte size whose  provider-bundle gap lies between 10
and 25, a nontrivial window capping two-route omission cases at 25
                                                                    
                                                                  2 = 300: AnyIO 4.14.2, with gap 21. The
denominator contains every single provider-consumer omission in the independently certified repair. For each
omission, the runner shadows every uniquely attributed method with a MutationError stub and runs the
complete pinned test suite. A marker distinguishes a suite that reached the mutation from infrastructure
failure before exposure. Before the retained campaign, one pilot exceeded seven baseline-suite durations; the

amendment discarded that pilot campaign and reran every mutant with a 60-second per-test and 600-second
whole-suite cutoff. Project selection, denominator, mutation operator, and completed pilot outcomes were
unchanged.

                                       Outcome                    count
                                       Killed                        21
                                       Timed out after exposure       0
                                       Survived                       0
                                       Unloadable                     0
                                       Infrastructure failure         0

Table 12: AnyIO mutation outcomes for all 21 generated single omissions. The baseline passed 3,934 tests,
and the pinned suite detected all 21 omissions.

    All 21 mutations were detected, and no mutation survived. Omitting any one measured displaced route
causes a detected test failure in the pinned test configuration; this does not estimate the cost of maintaining
those routes.
    The joint-omission sensitivity selects one marker-confirmed test for each of the 21 routes, runs both
selected tests for every unordered pair, and independently verifies the raw logs. It detects all 210 of 210
pairs. Both omission markers are reached in 169 pairs; at least one is reached in all 210. Thus no tested
pair masks both routes. Triple omissions, the full suite under each pair, omissions during a refactoring, and
future program states require separate evidence. The artifact stores the baseline, mutant, witness, and pair
logs; typed frozen records own their schemas, shared base classes own common execution and verification,
and subclasses supply only semantic hooks. Independent verifiers check every digest, configuration, outcome,
marker, denominator, and summary.
    The packaged pytest_counts_replay.json ledgers reparse the retained logs without changing the
historical reports. Four single-omission logs gain one previously uncounted error each; detection totals are
unchanged, and no new executions are recorded. The pairwise ledger needs no count corrections.

9.8         Python population selection and extraction protocol
Omitted requirements can raise or lower the gap. With two implementations and three classes,
Theorem 4.5 gives d↓ (A, B) = min{N10 , N01 , N11 }. For A = {1, 2} and B = {1, 2, 3}, the region counts are
(0, 1, 2), so the gap is zero. Dropping (B, 1) leaves B = {2, 3}: all three counts become one, so the gap rises
to one. Conversely, start with A = {1, 2} and B = {2, 3}, with gap one. Dropping (A, 1) leaves A = {2} ⊆ B,
with counts (0, 1, 1) and gap zero. A crossing needs all three regions nonempty, so two implementations and
three classes are minimal. Both directions are machine-checked, up to renaming classes and exchanging the
providers.91
     The Python factorization study screens a locked population before measuring the repair outcome. The
locked ranking is hugovk/top-pypi-packages commit e935455: of its first 150 non-yanked universal pure-
Python wheels, 65 have at least 50 locally parsed classes, 31 have a cross-branch class, and 20 have at least
four. The source lock contains the ranking snapshot, wheel URL, release, and SHA-256 digest for every
candidate. Each selected project has at least 50 locally parsed classes and at least four consumer classes
that inherit surviving concrete methods from two or more direct-base branches. Marker and abstract-only
bases, shared diamond ancestors, unresolved bases, competing concrete providers, and direct overrides fall
outside the selection rule. When incomparable most-derived declarations contain exactly one concrete body,
that body is attributed: coverage is forced even though no precedence answer is available or needed. For
source-class grouping, overriding one method does not remove the provider if another survives: if C overrides
P.f but inherits P.g, the method relation keeps (P.g, C) and drops (P.f, C); the grouped relation retains
(P, C). The primary threshold of four selects 20 projects. Repeating the complete analysis for the retained
91   DEC7

threshold-three screen selects 23, of which 20 have positive gap; it externalizes 747 of 14,249 pairs (5.24%), or
200 of 8,436 (2.37%) after excluding SQLAlchemy. The artifact exports and verifies all 23 result bundles.
   After the primary audit, a census extension scores every size-qualified project with at least one screened
cross-branch class, without changing the primary lock or the 20-project ladder. Of these 31 projects,
24 have positive recovered gaps. The 11 newly scored projects below use the same pinned wheels and
extraction rule; their optima are checked by independent direct replay; all their component gaps are at
most 10. The remaining 34 size-qualified projects remain unscored: their unmeasured gaps remain unknown.
cross_branch_census.json binds each certificate’s bytes to the original lock; counts and this table are
generated from those certificates.

                          Project           Cross-branch classes Required pairs Gap
                          boto3                               1              34     2
                          urllib3                             2              36     0
                          setuptools                          2             114     1
                          Pygments                            3            1240     0
                          pytest                              3             104     6
                          filelock                            3              25     2
                          jsonschema                          2              17     0
                          pyasn1                              2             305     0
                          virtualenv                          2             158     3
                          google-api-core                     2              74     2
                          beautifulsoup4                      1              59     0

Table 13: Census extension to the eleven projects below the primary selection threshold. All are scored,
including zero-gap projects.

9.8.1     Public-name preservation contract
This audit asks what the library must retain under a separate, reproducible contract: preserve the original
concrete provider resolution of every selected public member on every statically exposed public class. It
neither infers documented intent nor changes the primary twenty-project selection.
    A public member has no leading underscore or is a dunder __x__; this includes __enter__, __iter__,
__eq__, and __init__. Single-underscore names _x and mangled __x without trailing double underscores
are private. A public class is exposed in a module path with no underscore-prefixed segment, or publicly
re-exported there. A literal __all__ governs a module’s exports; otherwise non-underscore class and imported-
symbol names are candidates. Explicit re-exports resolve their named bindings; star re-exports follow the
target module’s export list. Aliases retain declaration identity and do not multiply pairs. Dynamic export
lists and conflicting static targets are omitted rather than guessed. Table 14 records dynamic-list exclusions;
the source extractor’s other exclusions remain in force.
                                                                                          req
    Filter the existing MRO-invariant concrete-method lookups by these rules. Define Rpub     to contain (f, c)
exactly when a selected public lookup on c resolves to provider f . Each pair retains a witness member,
public class alias, and original provider in the archived source-derived report. A design preserving these
specified resolutions must supply every such pair: apply lookup preservation to the witness member, then
lookup-to-supply implication. This argument allows forwarding equally with inheritance. Lean checks the
implication from the declared preservation contract. Source extraction supplies its public-member witnesses,
which can then be exercised by clients.92
    The artifact binds this relation to the original pinned wheels and to plans/public-api-contract-audit.
md. It independently replays each component with gap at most ten; 2 further components have feasible repairs
92   APC1, APC2
