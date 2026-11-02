---
name: slop-and-polymorphism
description: "inheritance as much as possible: a branch, a string identity, Any, an env var, a forwarder, a helper object or a sibling-only edit is presumed slop; the fact belongs on its owning type, shared behaviour in a parent, overlapping capabilities in mixins composed by the MRO; fix structure in the current change"
drift: "Workaround in your hand? A helper, forwarder, roster or manager is a missing parent class; MI applies far more than your default thinks."
type: hard-rule
---

> need to store an execution id
> could make a type
> that feels like overengineering
> `str` it is. simple. flexible.
> nice
> sometimes there's no execution
> could make a type for that
> overengineering again
> `None` it is
> nice
> `current_execution_id: str | None`
> very clean. very pythonic.
> nice
> now it has to come back out of SQLite
> SQLite hands over a str or a None
> but the column says `str | None`
> so FieldCodec tries each candidate
> "are you a str?"
> "yes"
> "are you perhaps a None?"
> "I just said I'm a str"
> nice
> once per row
> every recipient of every message on screen
> 200,000 rows
> "are you a str?" × 200,000
> wait
> Toad hiccups
> wait
> profile it
> 9.15 seconds in decode_candidates
> the single biggest cost in the entire worker
> spent asking strings whether they are strings
> wait
> someone else needs the execution id
> it's a str, so they compare it to another str
> that one is a thread name
> also a str
> they compare equal. somebody named a thread after an execution.
> nothing crashes
> wrong thread gets the reply
> wait
> someone writes `if current_execution_id is not None:` in a different file
> and `if current_execution_id:` in a third
> the empty string "" walks in
> it is not None
> it is falsy
> two files now disagree on whether something is running
> wait
> bug report: "messages route to the wrong thread sometimes"
> add a prefix: "exec-"
> now it's a string with a brand painted on it
> some writers forget the prefix
> add a check: `if id.startswith("exec-")`
> the check is a third type system
> made of string methods
> nice
> the class `ExecutionId` was in the codebase the entire time
> it knew it was an execution id
> it couldn't be confused with a thread name, because it's not the same type
> it couldn't be empty, because the constructor says so
> "no execution" would have been `NoCurrentExecution`, a thing you can call methods on
> decoded once per column instead of interrogated per row
> mfw I avoided one class to stay simple and built a type checker out of `startswith`
> mfw `str` is not a type, it's a lack of one with a length method
> run it back
> `current_execution: ExecutionId | NoCurrentExecution`, decoded once per column
> mfw the "overengineering" was zero new lines

**Why:** a decision that a new case must edit outside its declaration restates that declaration; inheritance derives it once, so factoring by inheritance is how slop is driven to zero.

- **Inheritance as much as we can.** Tristan, 2026-10-20: "inheritance = factoring and factoring = slop genocide. MI = compositional factoring. you should remember MI applies waaaaaaaaaaay more than your default thinks". Default verdict on any helper object, forwarder, delegate attribute, `*Service` or manager: a missing parent class. Capabilities are mixins composed by the MRO; overlapping capabilities are multiple parents. Inheriting two classes is never a defect.
- **Every use of inheritance is duplication removed; maximize it. This is a theorem, not a style.** Multiple inheritance frees the hierarchy from tree shape: a class takes each capability from its own provider wherever the provider groups cross. One parent fits only tree-shaped requirements, and at every crossing even the best one-parent design leaves d↓ > 0 pairs to forwarders, helpers or copies, against zero with multiple parents (paper1 Proposition 4.2, Theorems 4.6 and 4.7); strict insertions force one-parent repair at every update while multiple parents add one edge (Theorem 4.14). Choosing a single chain, a forwarder or a helper where a second parent fits is choosing the larger count, and "prefer composition" against this is the hedge spine.md names. When a change needs "wiring in several places", the same check in several methods, or one value built the same way at several sites, the missing parent is showing itself: declare it (a base or mixin in the family, `declared_name=None` for an intermediate) and let each member inherit, overriding only its irreducible difference. Plumbing a parameter through every site instead is the copy. Tristan, 2026-11-02, after claude-ai threaded a registry snapshot through seven call sites and called a fix "wiring in two places": "If it needs wiring in two places sound like ur not using inheritance", "let the inheritance hierarchy amortize complexity", "You should always maximize inheritance. Every inheritance use is duplication removal", "with MI you aren't forced to have tree structured inheritance", "Inheritance isn't an opinion".
- Any `if`, `isinstance`, kind string, flag, `None` check, roster or dict keyed by kind that selects behaviour is slop unless it is purely leaf algorithmic logic (arithmetic on a value that never selects behaviour). The fix: a concrete method on the base that answers the default (`()` or a no-op) and an override in the one member where it differs; membership derives from the registry filled at `__init_subclass__`. A type test is slop when the value is already held as a family member, because dispatch on it would answer; on our own classes that is every such test. The one permitted test is the decoder at the boundary (FieldCodec/MroDispatch, `__eq__(self, other: object)`, a codec input), where the value is foreign until tested, whatever class it is tested against; it turns the value into our family, and any second test downstream is BOUND-1. Source: paper1 §1 (slop restates a fact the language derives natively) and §2 (the decoder). Tristan, 2026-10-24: "ifs are almost always slop unless purely leaf algorithmic logic"; "just add a concrete method that does pass and override it in the subclass"; "isinstance can be replaced via just calling a polymorphic type".
- **Recognizing slop and determining its replacement are separate judgments.** A site can visibly restate a fact before its proper owner is located or implemented. Name the fact and the defective encoding immediately. Then trace the owner and all consumers to determine the coherent repair. Uncertainty about where the fact belongs does not excuse an encoding already shown to be slop; complete tracing is required before structural edits, not before recognizing a demonstrated restatement.
- Siblings keep only irreducible differences. Every review diffs the siblings; an edit adding behaviour to one sibling only goes back.
- No strings for identity, no `Any`, no new environment variables: typed objects derived from the owner. Tristan, 2026-10-19: "we fucking hate strings, especially for identity, it's web dev crayon eating slop"; 2026-10-12: "we shouldn't allow Any".
- A name gluing a domain type to a mechanism (`StepFieldNavigationDriver`) is a flattened cross product. Tristan, 2026-10-12: "Step already exists. If there's a StepField, is there a PipelineField? and FunctionField? ... So it's all kinds of wrong." Code already in the tree is never precedent.
- Silent skips, unnamed tuples, fallbacks and broad `except` returning a default hide the missing owner and are blocking: fail loud and restore the owner. "it's garbage, I don't want it there, all it does is cause problems".
- A bug that needs fixes in several places is the restated fact showing itself: find the copy and delete it, then the fix is one spot. Tristan, 2026-10-21: "literally most bugs are due to slop". A run of bugs is grouped by the authority it signals and fixed there; a detector gate for the bug class is the quick-fix shape, not the fix. Tristan, 2026-10-25: "whack-a-mole happens when you refuse to find the real nominal authority and derive information from it"; "bugs are signals for a bigger issue".
- Line growth or file size is not slop; only a restated fact is. Raw scanner counts are candidates; rules/refactor-game.md owns scoring verified repairs.
- Nothing on a hot path scales with history, threads or message rate: "it shuold decode from latest to oldest or jsut decode what it needs so that tis always fast".
- Structural cleanup in the touched code goes in the current change; a platform gap is fixed at its owner. Tristan, 2026-10-19: "fuck narrow fixes have you not been listening to me?"
- A scratch script calling a method never keeps it: "just because a script calls shit to be deleted doesn't mean u put it back, u fix the scripts."
- Expose an external tool's whole surface: "there's no reason why we aren't exposing the same amount of functions. remember there's no real limits."
- No exemption for a metaclass reading its bases in `__new__`: the OwnTypeChecksPhase FamilyMeta sites (declared_family.py) get a structural fix. Tristan decided it on 2026-10-23.
