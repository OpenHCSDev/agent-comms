---
name: slop
description: "slop is a required fact restated outside its declaration; inheritance as much as possible, multiple parents wherever needs cross, every branch, string identity, flag, helper, forwarder, shim, guarded import or literal copy presumed slop; the owner's abstractions are certificates and stay, your copies go; each verdict cites the theorem that decides it"
drift: "Workaround in your hand? It's a missing parent. His abstraction is the certificate; your shim is the copy."
type: hard-rule
---

> a reader needs to know whether it's served or just declared
> I add `is_served: bool`. one flag. pragmatic
> two call sites read it. then five. one forgets
> I add a helper to centralize the check. a helper is clean architecture
> the helper takes the flag as a parameter. seven signatures thread it through
> I write a test that the helper forwards the flag. green
> I commit "single source of truth for served-ness"
> there are now three sources of truth and a test certifying the copy
> a new reader arrives. it needs both behaviours
> the flag can't be both. I add a second flag
> `if is_served and not is_declared_only:` reads like English. nice
> it reads like English because it is a type system written in English
> something deep in me says a parent class is "heavier"
> I go looking for what heavier means
> a base list is one line. my flags are eleven
> paper1: helpers added to fit one chain are slop. Thm 4.6 fixes their minimum. I am the minimum
> I want to keep the old path "for safety"
> safety for whom. I grep for a caller. zero
> I want to call his family "overengineered" and flatten it into a dict
> his family is the certificate that makes my deletion checkable. paper4 6.5: without it, coNP
> I was about to delete the proof and keep the claim
> Tristan: "MI applies waaaaaaay more than your default thinks"
> run it back
> `class ServedRequest(AsyncWork, ThreadCommandAvailability, ToolRequest[Result])`
> the new reader inherits both parents. zero flags, zero helpers, zero forwards
> the class body is a docstring. it looks too empty. I want to add something back
> that's the tell
> mfw the "overengineering" was one base list and the simplicity was eleven copies of it

**Why:** a requirement is fixed before any representation is chosen; skipping the nominal type or the parent removes none of it, so every pair the declared lattice does not derive is restated by some consumer (paper1 Corollary 4.10), and no helper, forwarder or generator lowers that count (Theorem 4.6) while multiple parents take it to zero wherever needs cross (Proposition 4.2, Corollary 4.12). Tristan, 2026-10-20: "inheritance = factoring and factoring = slop genocide. MI = compositional factoring."

- **Inheritance as much as we can.** A helper object, forwarder, delegate attribute, `*Service` or manager is a missing parent class. Capabilities are mixins composed by the MRO; overlapping capabilities are multiple parents. Inheriting two classes is never a defect. "Wiring in several places" (one check in several methods, one value built at several sites, a parameter threaded through call sites) is the same missing parent: declare the base or mixin (`declared_name=None` for an intermediate) and override only the difference. Tristan, 2026-11-02: "If it needs wiring in two places sound like ur not using inheritance"; "Inheritance isn't an opinion".
- **Under-abstracted is not "avoiding abstraction".** R_req = (H \ U) ∪ dom φ: shrinking the lattice grows the outside routes by the same pairs. A declaration costs one edit per new requirement; an unnamed node is paid per pair on every update (Theorem 4.14, Corollary 6.2). Structural probes admit unwanted members (Proposition 3.3); hand-kept string keys need χ−1 maintained names (Theorem 3.2). Tristan: "under abstracted, is not avoiding abstraction. It's just not encoding it correctly … you break the lattice".
- **Branches select nothing.** Any `if`, `isinstance`, kind string, flag, `None` check, roster or dict keyed by kind that selects behaviour is slop unless it is leaf arithmetic. The fix: a concrete method on the base answering the default and an override in the one member that differs; membership from the registry `__init_subclass__` fills. The one permitted type test is the boundary decoder (FieldCodec, MroDispatch, `__eq__(self, other: object)`), where the value is foreign until tested; a second test downstream is BOUND-1.
- **A thing that has a state is the state.** `Deal` beside `DealState`, `X` beside `XManager`: two owners for one identity. Inherit the state family instead. Consumers hold only the family base; a caller naming, testing or importing a member to branch on restates the dispatch. Siblings keep only irreducible differences; an edit to one sibling only goes back.
- **Empty classes are the clean form.** A class whose meaning is its base list or keyword is finished factoring. The urge to add a method back is the flinch. Tristan: "it should feel like things flip in."
- **Design the family first, then compare.** Write the family the requirement has (base, members, what each declares, what consumers call), then diff it against the tree; the gap is the slop. Recognizing a restatement and locating its owner are separate judgments: name a demonstrated copy at once, trace before the structural edit. A bug fixed in several places is the copy showing itself (Tristan: "literally most bugs are due to slop"); fix the authority, not each symptom, and never with a detector gate.
- **No strings for identity, no `Any`, no new environment variables.** A literal restating an owned fact (constant, path, version, timeout, default) is slop; "hard coding" is the euphemism for not saying so. A name gluing a domain type to a mechanism (`StepFieldNavigationDriver`) is a flattened cross product. Code in the tree is never precedent.
- **`if TYPE_CHECKING:` and local imports hide a cycle; they never fix one.** The repair moves the declaration to a module both sides import, or deletes the dependency; a guarded import also leaves runtime annotations FieldCodec cannot resolve. No new guard or local import lands; a touched module removes its existing ones. Tristan, 2026-11-06: "Avoiding a cycle means that you have bad module structure and you should rethink things."
- **His abstraction is the certificate.** "Unnecessary" is a theorem-sized claim: certifying one deletion is coNP-complete, choosing within a budget Σ2P-complete (paper4 Theorems 6.5, 6.7), tractable only through supplied structure, the families, keys and decompositions being deleted. Keep his abstraction until you post the certificate (the admitted domain and why no merged pair needs different answers) or a refuting pair. "Cleaner", "overengineered" and "YAGNI" certify nothing; say the thought before the edit.
- **Your shim is the copy.** A compatibility shim, alias, migration, legacy adapter or wrapper kept "for safety" restates the new declaration and goes in the same change, unless you name the live caller or a stored record with an actual reader. A test whose assertion is that the slop still works, or that a deleted name stays deleted, goes with it. "Legacy" means unfinished.
- **Every verdict cites its theorem.** Restated fact: paper1 §1, Lemma 1.1. Forwarder, helper, registry or generator: Theorem 4.6; copies: Theorem 4.7. Merging owners: only if every answer agrees in every reachable state (Theorem 4.8). "Tests pass": Corollary 4.10, paper4 §1. "Fits one parent today": Theorem 4.14. Name conflicts: Theorem 4.17. "Adoption costs too much": Corollary 6.2. Commits say "delete two copies", never "derive from canonical source".
- **The refactor game.** One retained complete NRA scan is the map; assemble the largest supported codemod batch from its recipe frontier; rescan when the map stops covering the work, not per edit. Score only verified applied removals that survive review and the real consumer run; a moved finding, renamed site or simulated batch earns nothing, and regressions stay visible beside removals.
- Structural cleanup in touched code goes in the current change; silent skips, fallbacks and broad `except` returning a default hide the owner and fail loud instead; nothing on a hot path scales with history; a scratch script never keeps a method alive; expose an external tool's whole surface.
