# Supplement to Semantic Completeness for Correct Maintenance: part 7 of 10

Optional reading: the paper1 supplement converted to layout-preserving text, page by page. Figures are PNG files in `figures/`.

Native applicability Code and state       Precedence or
     Mechanism                                                                   Separate relation
                   carrier              supplied             constraint

                                                                                 Embedding natively
                   Native                                                        carries field and method
                                          Promoted methods   Ambiguous
                   embedded-field                                                composition; interface
     Go                                   from embedded      selectors remain
                   edges; structural                                             conformance supplies no
                                          values             unpromoted
                   interface satisfaction                                        nominal provider
                                                                                 identity.
                                         Interface                               Brands, mixin inputs,
                   One implementation declarations are       Explicit class or   registries, or adapters
     TypeScript    superclass; multiple erased; mixins       mixin order and     retain identity and
                   structural interfaces generate            user resolution     applicability outside
                                         implementation                          structural compatibility.

Source sections for every main-table cell. Stateful supply follows instance fields; static storage supplies
shared state with a different ownership relation. Each placement below concerns the named mechanism;
precedence, visibility, and admission constraints remain in force.
Python. Identity and nominal membership follow class declarations and bases; code and state follow class
    and instance attribute lookup [Python Software Foundation(2025c), Custom classes; Class instances].
C++. Named class declarations establish identity; base classes establish membership and supply both
   functions and data members [ISO C++ Working Group(2023b), [class.mi], [class.virtual]].
Scala. Classes and traits establish nominal identity and membership; concrete trait methods, val-
     ues, and parameters establish multiple code and state providers[Scala Project(2026a), Classes;
     Traits][Scala Project(2026c)].
Java. Class declarations and implemented interfaces establish nominal identity and membership. Default
     methods supply code from multiple interfaces; instance fields follow one superclass chain [Oracle(2026),
     §§8.1, 8.1.4–8.1.5, 8.3] [Oracle(2025e), §§9.1.3, 9.3–9.4].
C#. Class declarations establish identity and one state-bearing base; implemented interfaces establish multiple
    nominal memberships and default bodies [Microsoft(2026b), §§15.2, 15.2.4, 15.5] [Microsoft(2026a),
    §§19.2, 19.4].
Rust. Struct and enum declarations establish nominal types; explicit impls establish multiple trait
    memberships and default bodies [Rust Project Developers(2026f), Rust Project Developers(2026e),
    Rust Project Developers(2026b)]. The reference’s Deref rules, field autoderef, and method candidate-
    receiver search establish one target chain for field and method supply [Rust Project Developers(2026d),
    Rust Project Developers(2026a), Rust Project Developers(2026c)]. Mutable access needs DerefMut;
    visibility and receiver constraints still apply. One target declaration supplies the target’s member set,
    so individual fields need no forwarding declarations.
Go. “Type identity” establishes named types; “Interface types” and “Uniqueness of identifiers” establish
     structural admission and package-scoped unexported method keys. “Struct types” and “Selectors”
     establish multiple embedded-field and promoted-method supply[The Go Authors(2026b)]. Embedding
     can propagate an unexported-method key: a seal is not an absolute prohibition on implementers outside
     the declaring package.
TypeScript. “Type Compatibility” and “Symbols” establish structural identity and retained brands; in-
    terfaces are structural and erased [Microsoft(2026f), Microsoft(2026e)]. “How Does A Mixin Work?”
    composes class-expression factories along one superclass chain. “Alternative Pattern” copies prototype
    descriptors and requires separate type declarations [Microsoft(2026d)]. Descriptor copying creates
    replicas; class-expression factories instead retain their superclass edges. Provider-preserving crossings
    and replica disagreement remain distinct questions.

Haskell 2010. Sections 4.2.1 and 4.2.3 establish nominal data types; Sections 4.3.1–4.3.2 establish superclass
    constraints, explicit instances, and default methods, without data inheritance[Marlow(2010)].

Supplying versus using a native carrier. For Haskell, use type constructors as consumers and type-
class constraints as contracts. An instance declaration establishes a class–type membership answer; the
superclass DAG then entails its superclass constraints. Each required superclass instance must already exist:
entailment uses that declaration. Default class methods supply code when an instance omits the corresponding
binding. Admission, superclass entailment, and implementation supply therefore have distinct authoritative
declarations, just as membership and supply do in the main model [Marlow(2010), §§4.3.1–4.3.2].
    Implementing a sound native carrier is itself engineering work. Retrofitting one into a runtime whose verifier,
layout, dispatch, reflection, serialization, ABI, and interlanguage contracts assume a technical limitation can
be harder than supplying it initially. The CLR design notes make this distinction concrete: runtime support is
what makes inheritance type-safe and interoperable across languages, while field-bearing multiple inheritance
was not standardized because languages give it different semantics [Microsoft .NET Runtime Team(2026)].
This engineering cost belongs to implementing the carrier; once supplied, the carrier derives the same required
relation regardless of that setup price.
    The compensation ledger can aggregate this boundary. Let KL (T ) collect the platform-specific cost of
implementing, standardizing, and maintaining the carrier through horizon T , and let project j otherwise pay
cumulative workaround burden qj Bj (nj ). Ignoring terms shared by both choices, supplying the carrier is
cheaper once                                            X
                                              KL (T ) <     qj Bj (nj ).
                                                        j

The inequality does not assert that a platform has reached that threshold. It states where the costs belong:
KL (T ) prices the mechanism’s implementation; Bj prices the downstream answers left to each program while
it is absent. Neither term changes its derivation capacity after it is supplied.

Documented industrial artifacts. The primary sources below record shipped mechanisms and API
behavior. The last column classifies each documented fact under the paper’s definitions; it does not infer why
the platform adopted it or estimate prevalence. The tooling law fixes the accounting: a registry, adapter,
generator, or forwarding workaround preserving required supply with one-parent ancestry owns at least the
d↓ pairs missing from ancestry. Native multi-parent ancestry can leave no such deficit, so that workaround
layer need not exist. Retaining overreach instead needs unwanted-supply removal or admits invalid states.
    These mechanisms show that a diamond is a topology. Shared state and provider conflicts require separate
policies. C++, Eiffel, CLOS, Python, and Scala carry broad implementation applicability in ancestry.
Java, C#, Kotlin, Rust, and Swift carry useful behavior incidence through interfaces, traits, conformance,
and default implementations while keeping other resources in separate carriers. Java and C# added these
subsets later: Java 8 introduced default methods for interface evolution and binary compatibility, and C# 8
introduced default interface members [Oracle(2014), Microsoft(2026c)]. Kotlin interfaces supplied method
bodies from 1.0 [JetBrains(2016), Isakova(2020)]. PHP 5.4 traits also supply properties, adding state to their
method supply [PHP Development Team(2012), PHP Documentation Group(2026)]. Java and C# move
method bodies into multiple nominal providers while leaving instance state elsewhere. They add behavior
carriers without adding full state-bearing multiple class ancestry. Go and TypeScript make the separation
especially visible. For any declared mapping from a concrete construct to provider–consumer judgments,
the canonical-quotient proposition determines from Rreq which consumers need different providers and must
remain distinguishable. The table does not assert that conformance, behavior, and state use identical role
mappings across languages. Memory layout, compilation, dispatch latency, comprehensibility, and testing
remain separate coordinates.

Pinned availability snapshot. The artifact records the July 2026 TIOBE top ten and the classification
boundary in experiments/language_capacity_census. Python, C++, and R expose the state-bearing
multi-parent coordinate; seven entries do not, including C and SQL, which have no class-inheritance
mechanism on this coordinate [TIOBE Software BV(2026), R Core Team(2026), Ecma International(2026),

Artifact               Primary-source fact                                Classification in this model
                                                                          Multiple nominal contracts became
                       Both platforms later added method bodies to
                                                                          native implementation providers. The
Java 8 and C# 8 in-    nominal interfaces; their specifications resolve
                                                                          provider edges move method answers
terface defaults       or reject competing defaults [Oracle(2014),
                                                                          into a checked carrier; instance state
                       Microsoft(2026c), Microsoft(2026a)].
                                                                          remains elsewhere.
                                                                          A companion one-parent class supplies
                       Oracle calls it a “skeletal implementa-
                                                                          implementation that the interface does
                       tion” that minimizes the effort to im-
Java                                                                      not. The optional-operation exception
                       plement Collection; its default add
AbstractCollection                                                        fulfills the documented contract, so its
                       throws UnsupportedOperationException
                                                                          implementation keeps the superclass
                       [Oracle(2025b)].
                                                                          promise.
                                                                          Contract membership alone does not
                                                                          derive the constructor or operation
                       The interface documents constructor conven-
                                                                          applicability relation. Each imple-
                       tions it cannot enforce and marks operations
Java Collection                                                           mentation or permitted exceptional
                       optional; unsupported implementations throw
                                                                          result still needs a provider; disabling
                       an unchecked exception [Oracle(2025c)].
                                                                          a promised operation would violate
                                                                          the contract.
                       Inherited Hashtable.put admits non-String          The parent exposes behavior outside
                       keys or values even though property en-            the narrower valid relation. The doc-
Java Properties        tries are specified as strings; later store,       umented failing states arise because
                       save, propertyNames, or list calls can fail        inherited operations admit invalid val-
                       [Oracle(2025d)].                                   ues.
                                                                          Implementation reuse and substi-
                       Oracle states that the Date edge denotes im-
                                                                          tutability are different relations. One
                       plementation rather than type inheritance and
Java Timestamp                                                            parent edge carries the first while ex-
                       documents asymmetric equality between the
                                                                          posing an invalid instance of the sec-
                       two classes [Oracle(2025a)].
                                                                          ond.
                       IAdapterManager maintains a registry of fac-
                                                                          The registry is an outside workaround
                       tories; plug-ins can register adaptable-class–
Eclipse adapter reg-                                                      authority. It owns the applicability
                       adapter pairs in code or through an XML
istry                                                                     pairs that class ancestry does not de-
                       extension point [Eclipse Foundation(2026a),
                                                                          rive.
                       Eclipse Foundation(2026b)].

Table 28: Primary-source instances of the carrier, replacement, overreach, relation-splitting, and external-
authority cases. The classification is a consequence of the fixed-relation model: shipped behavior determines
the case, and the platform’s reasons for choosing it are a separate historical question.

Microsoft(2021)]. This census locates available native capacity: projects can keep a relation in ancestry only
where that capacity exists. Usage and adoption ask which available designs projects actually choose.

Related-Work Synthesis
Opdyke and Johnson extract abstract superclasses by checked refactorings; Godin and Mili, Moore, Dicky et
al., Snelting and Tip, Tip and Sweeney, and KABA construct, insert into, specialize, or refactor par-
ticular hierarchies [Opdyke and Johnson(1993), Godin and Mili(1993), Moore(1996), Dicky et al.(1996),
Snelting and Tip(2000), Tip and Sweeney(2000), Streckenbach and Snelting(2004)]. Their objectives include
maximal factoring, minimal member distribution, object size, and client-relative behavior preservation.
Balatsouras and Smaragdakis instead complete a partial subtype graph, give separate single- and multiple-
inheritance algorithms, decompose Java’s class and interface constraints, and prove minimum strata for
their single-inheritance construction [Balatsouras and Smaragdakis(2013)]. Tip et al. and constraint-based
work construct selected behavior-preserving repairs [Tip et al.(2003), Steimann(2018)]; Kegel and Steimann
replace inheritance with delegation [Kegel and Steimann(2008)].
    Those objectives do not minimize the required implementation–class pairs left outside every sound
program repair with one ancestor chain per class. Chen et al. optimize a fixed table [Chen et al.(2006)]; main-
paper Theorem 4.6 identifies that table optimum with the program-level floor despite arbitrary delegation,
forwarding, reparenting, and infinitely many helpers. Main-text Theorem 4.14 shows why fitting a tree
at each version does not avoid repair as requirements grow. Trait work identifies qualitative reuse and
conflict problems in single, multiple, and mixin inheritance and proposes a compositional mechanism
[Schärli et al.(2003), Ducasse et al.(2006)]; we count the required pairs a mechanism leaves outside.
    Empirical inheritance studies count uses and conflict patterns [Tempero et al.(2008), Tempero et al.(2013),
Ducournau et al.(2009)]; design-pattern, mixin, family-polymorphism, and C3 work organizes composition
or orders candidates [Gamma et al.(1994), Bracha and Cook(1990), Ancona and Zucca(1998), Ernst(2001),
Barrett et al.(1996)]. Behavioral subtyping checks the behavior of selected pairs; it does not choose Rreq
[Cardelli(1988), Liskov and Wing(1994)]; Witsenhausen gives identity’s coloring law, and main-text Section 3
adds history [Witsenhausen(1976)].

15      Full Lean Handle Ledger
This supplement provides the complete Lean handle ledger cited by the manuscript. Unused numeric IDs are
intentionally omitted. IDs are stable and sparse. It includes every handle identifier, declaration name, and
source module path.
                                           Table 29: Lean handle index.

 ID           Lean Handle / Source
              Ssot.AccountingCharacterization.positive_sets_preserve_crossings
 ACF1
              Paper1PL/AccountingCharacterization.lean
              Ssot.AccountingCharacterization.finite_declaration_positive_floor
 ACF2
              Paper1PL/AccountingCharacterization.lean
              Ssot.AccountingCharacterization.every_nonempty_set_is_crossing_residual
 ACF3
              Paper1PL/AccountingCharacterization.lean
              Ssot.AccountingCharacterization.preserves_all_crossings_iff_positive
 ACF4
              Paper1PL/AccountingCharacterization.lean
              Ssot.AncestryGapProbability.zeroGap_iff_regionMinimum_zero
 AGP1
              Paper1PL/AncestryGapProbability.lean
              Ssot.AncestryGapProbability.mass_total
 AGP2
              Paper1PL/AncestryGapProbability.lean
              Ssot.AncestryGapProbability.exact_zero_gap_probability
 AGP3
              Paper1PL/AncestryGapProbability.lean
              Ssot.AncestryGapProbability.zero_gap_bounds
 AGP4
              Paper1PL/AncestryGapProbability.lean
              Ssot.AncestryGapProbability.zero_gap_isTheta
 AGP5
              Paper1PL/AncestryGapProbability.lean

ID      Lean Handle / Source

        Ssot.AncestryGapProbability.zero_gap_tendsto_zero
AGP6
        Paper1PL/AncestryGapProbability.lean
        Ssot.AncestryGapProbability.mass_complement
AGP7
        Paper1PL/AncestryGapProbability.lean
        Ssot.PublicApiContract.public_pair_has_client_witness
APC1
        Paper1PL/PublicApiContract.lean
        Ssot.PublicApiContract.public_resolution_preservation_forces_supply
APC2
        Paper1PL/PublicApiContract.lean
        Ssot.NativeCapacityLadder.fourBindings_threeNativeSlots_leave_oneOutside
ARI1
        Paper1PL/NativeCapacityLadder.lean
        Ssot.AverageCaseAncestryGap.twoOwner_crossing_iff_regions_nonempty
AVG1
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.twoOwnerRegionMinimum_le_gapCost
AVG2
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.twoOwnerGap_exact
AVG3
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.RowColumnRepair.no_big_double
AVG4
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.rowColumnGap_lowerBound
AVG5
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.canonicalRowColumnRepair_gapCost
AVG6
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.AverageCaseAncestryGap.rowColumnGap_exact
AVG7
        Paper1PL/AverageCaseAncestryGap.lean
        Ssot.BehavioralApplicability.BehavioralBoundary.behavioralSubstitution_iff_reachableInvalid_eq_
BAP1    empty
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.BehavioralBoundary.behavioralSubstitution_iff_forall_reachable_
BAP2    valid
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.noProductViolation_iff_productCorrect
BAP3
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.behavioralCorrect_does_not_imply_exactApplicability
BAP4
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.exactApplicability_does_not_imply_behavioralCorrect
BAP5
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.minimumGap_le_behaviorallyValidExactSingleParentSystem_gap
BAP6
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.behaviorallyValid_crossing_still_has_positive_ancestryGap
BAP7
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.BehavioralBoundary.subset_iff_preserves_every_property
BAP8
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.BehavioralBoundary.behavioralSubstitution_iff_preserves_every_
BAP9    valid_property
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.noProductViolation_iff_exactApplicability_and_propertyPreservation
BAP10
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.noProductViolation_of_exactApplicability_and_certificates
BAP11
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.SoundBehavioralProofMethod.certifies_iff_behavioralSubstitution
BAP12
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.SoundBehavioralProofMethod.complete_methods_certify_iff
BAP13
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.fiberLocalCorrect_iff_of_equiv
BAP14
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.hidden_laminar_crossing_requiredIncidence_card_eq
BAP15
        Paper1PL/BehavioralApplicability.lean

ID      Lean Handle / Source

        Ssot.BehavioralApplicability.hiddenRequiredPairEquiv_preserves_required
BAP16
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.hiddenRequiredPairEquiv_preserves_behavioralSubstitution
BAP17
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.fiberLocal_behavior_cannot_determine_ancestryGap
BAP18
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.fiberLocal_behavior_same_result_different_ancestryGap
BAP19
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.MethodContract.substitution_iff_preserves_promised_calls
BAP20
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.MethodContract.contract_preserving_override
BAP21
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.MethodContract.disabling_promised_call_violates_substitution
BAP22
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.MethodContract.forbidden_result_violates_substitution
BAP23
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.MethodContract.changing_result_allowed_disabling_not
BAP24
        Paper1PL/BehavioralApplicability.lean
        Ssot.BehavioralApplicability.excluded_parent_promise_forbids_subtyping
BAP25
        Paper1PL/BehavioralApplicability.lean
        Ssot.BlockingRepair.canonicalRepair_decisionCost
BLK1
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.forwardPairs_subset
BLK2
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.blockPairs_subset
BLK3
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.paidBlockingCost_le_decisionCost
BLK4
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.bruteForcePaidBlockingGap_spec
BLK5
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.paidBlockingGap_le_blockerFreeGap
BLK6
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.zeroWeight_paidBlockingGap_eq_zero
BLK7
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.positiveWeight_paidBlockingGap_pos_of_not_laminar
BLK8
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.FiniteBlockingRepair.decisionCost_pos_of_not_laminar
BLK9
        Paper1PL/BlockingRepair.lean
        Ssot.BlockingRepair.paidBlockingGap_eq_blockerFreeGap_of_gap_le_weight
BLK10
        Paper1PL/BlockingRepair.lean
        Ssot.BoundaryPropertyInstances.DeclaredBoundary.zeroError_iff_hasReachableResolvers
BPI1
        Paper1IT/BoundaryPropertyInstances.lean
        Ssot.BehavioralRewrite.ExactIndexedSingleParentBehavioralRewrite.residual_of_gap
BWR1
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.minimumIndexedLaminarGapCost_le_every_exactSingleParentRewrite_
BWR2    ancestryGap
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactBoundaryEncoding.equal_joint_state_implies_equal_required
BWR3
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactBoundaryEncoding.requiredView_factorsThrough_jointState
BWR4
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactDecisionEncoding.column_factorsThrough_code
BWR5
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactDecisionEncoding.code_eq_implies_quotient_eq
BWR6
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.canonicalDecisionQuotient_card_le_code_card
BWR7
        Paper1PL/BehavioralRewrite.lean

ID      Lean Handle / Source

        Ssot.BehavioralRewrite.exists_zero_length_exact_description
BWR8
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.strictDerivationalLoss_of_nonlaminar
BWR9
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.multiParent_strictlyDominates_exactSingleParent_on_chargedAxes
BWR10
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.dominanceRespectingPreference_prefers_multiParent
BWR11
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.same_native_ancestry_supports_distinct_required_relations
BWR12
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.shorter_correct_factorization_excludes_shortest
BWR13
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.behavioralImplication_iff_profile_subset
BWR14
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.mutualBehavioralImplication_iff_sameDecisionColumn
BWR15
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactDecisionEncoding.reachableCodeImplication_iff_behavioralImplication
BWR16
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.singletonOwner_subset_profile_iff_required
BWR17
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.distinctSingletonOwners_incomparable
BWR18
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.independentResidualTopology_card
BWR19
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.detectsEverySingleOmission_iff_fullCoverage
BWR20
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.existsUnique_exactIndependentResidualTopology
BWR21
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.boundedSemanticEditCapacity_lower_bound
BWR22
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.minimumIndexedLaminarGapCost_ceilDiv_le_semanticEdits
BWR23
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.minimumSemanticEditPlan_singleOmission_failures
BWR24
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.exactSingleParent_architectureCost_lower_bound
BWR25
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.singleParent_choice_includes_pricedGap
BWR26
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.equalOtherCost_multiParent_strictlyCheaper
BWR27
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.pricedSemanticEdit_lower_bound
BWR28
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.allIndependentResidualTopologies_not_zeroError_of_nonempty
BWR29
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.checkedCompleteResidualTopology_zeroError
BWR30
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.unrestrictedDuplicateOwners_not_zeroError_of_distinct
BWR31
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.checkedSynchronizedDuplicateOwners_zeroError
BWR32
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.universalNative_laminar
BWR33
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.universalNative_misses_no_required
BWR34
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.universalNative_violates_exactReach_of_unrequired
BWR35
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.universalNative_exactReach_iff_requiredUniversal
BWR36
        Paper1PL/BehavioralRewrite.lean

ID      Lean Handle / Source

        Ssot.BehavioralRewrite.zeroErrorOverReachableResidualTopologies_iff_subset_complete
BWR37
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.exploit_unreachable_iff_zeroError
BWR38
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.residualOnly_expressive_and_zeroError_iff_invalid_unreachable
BWR39
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ZeroErrorPolicyEquivalent
BWR40
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ZeroErrorPolicyEquivalent.pointwiseAgreement
BWR41
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.same_empty_native_ancestry_exact_for_laminar_and_crossing
BWR42
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.representationState_ne_of_requiredRelation_ne
BWR43
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.residual_ne_of_scope_ne_of_native_eq
BWR44
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.bundledResidualAnswer_iff_of_same_bundle
BWR45
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.exactBundledResidualTopology_iff_eq_univ_of_surjective
BWR46
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.independentBundledResidualTopology_card
BWR47
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.existsUnique_exactBundledResidualTopology_of_surjective
BWR48
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.zeroErrorOverReachableBundledResidualTopologies_iff_subset_complete
BWR49
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.disjointAdditiveCost_eq_sum_singletons
BWR50
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.uniformDisjointAdditiveCost_eq_card_mul_unit
BWR51
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.normalizedDisjointAdditiveCost_eq_card
BWR52
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.positiveSingletonDisjointAdditiveCost_pos_of_nonempty
BWR53
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.semanticEditDistance_eq_zero_iff
BWR54
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.normalizedDisjointAdditiveCost_changedAnswers_eq_semanticEditDistance
BWR55
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.semanticEditDistance_ceilDiv_le_edits
BWR56
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.reachableInvalidResidualTopologyCount_eq_zero_iff_zeroError
BWR57
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactValidExpansion.valid_iff_mem_range
BWR58
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactValidExpansion.range_expand_eq_setOf_valid
BWR59
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.ExactValidExpansion.unrestricted_excess_eq_invalid
BWR60
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.replicatedExpansion_range_eq_agreement
BWR61
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.unrestrictedReplicatedExcess_eq_disagreement
BWR62
        Paper1PL/BehavioralRewrite.lean
        Ssot.BehavioralRewrite.pricedRealization_cost_floor
BWR63
        Paper1PL/BehavioralRewrite.lean
        Ssot.CompletionAudit.uninspected_omission_has_same_verdict
CAA1
        Paper1PL/CompletionAudit.lean
        Ssot.CompletionAudit.detects_single_omissions_reads_every_entry
CAA2
        Paper1PL/CompletionAudit.lean

ID     Lean Handle / Source

       Ssot.CompletionAudit.exact_audit_operation_lower_bound
CAA3
       Paper1PL/CompletionAudit.lean
       Ssot.CompletionAudit.independent_releases_operation_lower_bound
CAA4
       Paper1PL/CompletionAudit.lean
       Ssot.CompletionAudit.priced_independent_release_audits
CAA5
       Paper1PL/CompletionAudit.lean
       Ssot.CompletionAudit.unbounded_batch_checks_all_in_one_probe
CAA6
       Paper1PL/CompletionAudit.lean
       Ssot.CompletionAudit.proved_complete_inputs_need_no_completion_probes
CAA7
       Paper1PL/CompletionAudit.lean
       Ssot.NativeCapacityLadder.oneContractHierarchy_iff_membershipLaminar
CAP2
       Paper1PL/NativeCapacityLadder.lean
       Ssot.NativeCapacityLadder.minimumOneContractCompletion_iff_membershipGap
CAP3
       Paper1PL/NativeCapacityLadder.lean
       Ssot.NativeCapacityLadder.oneContractNative_has_no_unwantedMembership
CAP4
       Paper1PL/NativeCapacityLadder.lean
       Ssot.NativeCapacityLadder.addOnlyCompletion_cannot_cancel_nativeMembership
CAP5
       Paper1PL/NativeCapacityLadder.lean
       Ssot.NativeCapacityLadder.multipleContracts_complete
CAP6
       Paper1PL/NativeCapacityLadder.lean
       Ssot.NativeCapacityLadder.jointOutsideAnswers_card
CAP7
       Paper1PL/NativeCapacityLadder.lean
       Ssot.CarrierNonDichotomy.implementation_ancestry_iff_contract_requires_implementation
CND1
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.ancestry_supplies_required_implementation
CND2
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.routing_supplies_required_implementation
CND3
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.exact_ancestry_and_routing_agree_on_required_implementation
CND4
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.ancestry_and_required_possession_cooccur
CND5
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.ancestry_derives_supply
CND6
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CarrierNonDichotomy.ancestry_and_supply_not_mutually_exclusive
CND7
       Paper1PL/CarrierNonDichotomy.lean
       Ssot.CoupledEdges.independentGaps_le_coupledGap
CPL1
       Paper1PL/CoupledEdges.lean
       Ssot.CoupledEdges.coupledGap_strict_witness
CPL2
       Paper1PL/CoupledEdges.lean
       Ssot.CoupledEdges.symmDiff_le_dualRoleGap
CPL3
       Paper1PL/CoupledEdges.lean
       Ssot.CoupledEdges.dualRoleGap_inter_eq_symmDiff
CPL4
       Paper1PL/CoupledEdges.lean
       Ssot.ContractRelativeSoundness.conformance_does_not_determine_inheritedBehavior
CRS3
       Paper1PL/ContractRelativeSoundness.lean
       Ssot.ContractRelativeSoundness.inheritedBehavior_does_not_determine_inheritedInvariant
CRS4
       Paper1PL/ContractRelativeSoundness.lean
       Ssot.CapacityDescent.outsideAnswers_mono_lowerOne
CSD1
       Paper1PL/CapacityDescent.lean
       Ssot.CapacityDescent.outsideAnswers_strict_lowerOne_of_used
CSD2
       Paper1PL/CapacityDescent.lean
       Ssot.CapacityDescent.simplifyingStep_eq_lowerOne_of_positive
CSD3
       Paper1PL/CapacityDescent.lean
       Ssot.CapacityDescent.simplifyingStep_rearms
CSD4
       Paper1PL/CapacityDescent.lean
       Ssot.CapacityDescent.lowerOne_four_steps
CSD5
       Paper1PL/CapacityDescent.lean

ID      Lean Handle / Source

        Ssot.CapacityDescent.simplifyingPolicy_four_steps
CSD6
        Paper1PL/CapacityDescent.lean
        Ssot.CapacityDescent.structuralOnly_outsideAnswers
CSD7
        Paper1PL/CapacityDescent.lean
        Ssot.CapacityDescent.maintenanceCharge_mono_of_unitCost_le
CSD8
        Paper1PL/CapacityDescent.lean
        Ssot.RelationCarrier.chargedTranscript_bits_lower_bound
CWF1
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.chargedTranscript_actions_lower_bound
CWF2
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.chargedTranscript_incremental_cost_lower_bound
CWF3
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.chargedTranscript_actions_pos
CWF4
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.chargedTranscript_incremental_cost_pos
CWF5
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.independentRelation_acquisition_actions_lower_bound
CWF6
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.sharedSpecification_zero_acquisition
CWF7
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.unavailableBatcher_setup_pos
CWF8
        Paper1IT/RelationCarrier.lean
        Ssot.RelationCarrier.unavailableBatcher_setup_cost_floor
CWF9
        Paper1IT/RelationCarrier.lean
        Ssot.Decidability.adequate_decidable
DEC1
        Paper1PL/Decidability.lean
        Ssot.Decidability.finiteIndexedLaminar_decidable
DEC2
        Paper1PL/Decidability.lean
        Ssot.Decidability.bruteForceGap_spec
DEC3
        Paper1PL/Decidability.lean
        Ssot.Decidability.gap_computable
DEC4
        Paper1PL/Decidability.lean
        Ssot.Decidability.contractOnly_gap_eq_total
DEC5
        Paper1PL/Decidability.lean
        Ssot.Decidability.finite_native_price_chain
DEC6
        Paper1PL/Decidability.lean
        Ssot.Decidability.dropped_requirements_gap_incomparable
DEC7
        Paper1PL/Decidability.lean
        Ssot.DerivationRetention.Path.unique
DRP1
        Paper1PL/DerivationRetention.lean
        Ssot.DerivationRetention.derivations_subsingleton
DRP2
        Paper1PL/DerivationRetention.lean
        Ssot.DerivationRetention.charged_deletion_iff_unretained
DRP3
        Paper1PL/DerivationRetention.lean
        Ssot.DerivationRetention.direct_growth_retains_derivations
DRP4
        Paper1PL/DerivationRetention.lean
        Ssot.DerivationRetention.derivations_mono
DRP5
        Paper1PL/DerivationRetention.lean
        encoding_location_gap
ENC1
        AbstractClassSystem/Extended.lean
        Ssot.EvolvingObservation.behaviorAdequateThroughout_iff_properColoring
EVO17
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.conflictGraph_observedWhen_adj_iff
EVO18
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.behaviorAdequateThroughout_of_observe_injective
EVO23
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.tag_injective_of_constant_observation_of_behavior_injective
EVO24
        Paper1IT/EvolvingObservation.lean

ID      Lean Handle / Source

        Ssot.EvolvingObservation.overlappingPairTrace_union_not_cluster
EVO25
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.behaviorAdequateThroughout_of_tag_injective
EVO26
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.exists_behaviorAdequateThroughout_card
EVO27
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.behaviorKeyPrice_range
EVO28
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.card_le_palette_of_constant_observation_of_behavior_injective
EVO29
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.exists_behaviorAdequateThroughout_one_of_observe_injective
EVO30
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.adequateThroughout_precomp
EVO38
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.conflictGraph_precomp_le
EVO39
        Paper1IT/EvolvingObservation.lean
        Ssot.EvolvingObservation.sum_palette_eq_sum_additionalNameCount_add_card
EVO40
        Paper1IT/EvolvingObservation.lean
        Ssot.FiniteRuleDescriptions.exact_generator_iff_computable_provider_bound
FDC1
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.fixed_finite_universe_has_exact_generator
FDC2
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.exact_computable_generator_has_computable_lookup
FDC3
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.firstSuccess_primrec
FDC4
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.firstSuccess_unique
FDC5
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.firstSuccess_exists_iff_halting
FDC6
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.singleton_fibers_do_not_suffice_for_generation
FDC7
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FiniteRuleDescriptions.firstSuccess_no_computable_provider_bound
FDC8
        Paper1PL/FiniteRuleDescriptions.lean
        Ssot.FieldMenus.parityField_card
FLD1
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.full_profile_injective
FLD2
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.profile_tagFeasible_two
FLD3
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.profile_eq_of_sub_eq
FLD4
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.parity_menu_conflictGraph_eq_top
FLD5
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.parity_menu_each_branch_tagFeasible_two
FLD6
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.parity_menu_needs_global_uniqueness
FLD7
        Paper1PL/FieldMenus.lean
        Ssot.FieldMenus.profile_eq_iff_eq_or_eq_sub
FLD8
        Paper1PL/FieldMenus.lean
        Ssot.GapRatioIndependence.bruteForceGap_appendDisjointRow
GRI1
        Paper1PL/GapRatioIndependence.lean
        Ssot.GapRatioIndependence.appendDisjointRow_total
GRI2
        Paper1PL/GapRatioIndependence.lean
        Ssot.GapRatioIndependence.appendDisjointRow_gap_and_total
GRI3
        Paper1PL/GapRatioIndependence.lean
        Ssot.GapRatioIndependence.appendFinRow_gap_and_total
GRI4
        Paper1PL/GapRatioIndependence.lean
