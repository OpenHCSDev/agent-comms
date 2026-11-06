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
>
> next job: the rule gate. refuse tools until the deal is satisfied
> I write it the obvious way
> def blocked(self, deal):
>     if unread := deal.unread(self.dealt_read):
>         return GateRefusal("reading", ...)
>     if pending := deal.unengaged(self.dealt_read, self.audit):
>         return GateRefusal("engaging", ...)
>     return super().blocked(deal)
> very readable. very explicit. nice
> Tristan: if unread, if pending, if unaudited. those are elements of a set. they're all a type of state
> a deal has a state, so a deal is a state
> every requirement is the same `if X: refuse` written again, so it's a parent
> I look at my `Deal` and my `DealState` sitting side by side
> two owners for one identity. one inherits State, the other wraps it
> run it back (rule_deal.py)
> class State(ABC): ...                    # consumers hold this family and nothing else
> class Owed(State): ...                   # one thing a deal still requires
> class Unread(Owed): ...
> class Unengaged(Owed): ...
> class Unmeant(Owed): ...
> class Unaudited(Owed): ...
> class RuleDeal(State, DealtCalls): ...   # the deal is a state; no DealState beside it
> class ReadsDealtSources(ToolGate, owes=(Unread,)):
>     """Every dealt file is read in full before anything else runs."""
> class DealtRuleGate(AsksIfMeant, AnswersDealtRules, ReadsDealtSources, AuditsDealtRules):
>     """The gate every backend runs."""
> each requirement is a docstring and a base list. no body
> `__init_subclass__` collects the `owes=` in MRO order, so the base list is the order the gate checks them
> it looks too empty. I want to add something back
> that flinch is the tell
> mfw the requirements are classes with nothing in them and the MRO is the order they're checked

**Why:** a decision that a new case must edit outside its declaration restates that declaration; inheritance derives it once, so factoring by inheritance is how slop is driven to zero.

- **Inheritance as much as we can.** Tristan, 2026-10-20: "inheritance = factoring and factoring = slop genocide. MI = compositional factoring. you should remember MI applies waaaaaaaaaaay more than your default thinks". Default verdict on any helper object, forwarder, delegate attribute, `*Service` or manager: a missing parent class. Capabilities are mixins composed by the MRO; overlapping capabilities are multiple parents. Inheriting two classes is never a defect.
- **Under-abstracted code has the same abstraction, encoded as slop.** The requirement is fixed before any representation is chosen (paper1 §2): which classes need which behaviour, which records stay distinct, which classes a contract admits. Skipping the nominal type or the parent removes none of it. Every required pair is still supplied, now by a branch, a `hasattr` probe, a string tag or a forwarder at each consumer: R_req = (H \ U) ∪ dom φ, with H shrunk and dom φ grown by the same pairs (Corollary 4.10). "Avoiding abstraction" and "overengineering" name a move that does not exist; the only choice is whether the declared lattice derives the fact once or every consumer restates it. A declaration amortizes, one edit per new requirement, against outside work on every pair at every update (Theorem 4.14, Corollary 6.2). An implicit abstraction is also unauditable: structural probes admit unwanted members (Proposition 3.3), hand-kept string keys need χ−1 maintained names (Theorem 3.2), and without the declared family a deletion check falls back to coNP (paper4 Theorem 6.5). Tristan, 2026-11-02: "it's not just about using is instance versus using get adder, has adder. It's about using nominal types and using inheritance and amortizing complexity and not leaving abstractions implicit"; "under abstracted, is not avoiding abstraction. It's just not encoding it correctly. So it's just, you break the lattice".
- **"Wiring in several places" is a missing parent.** The same check in several methods, one value built the same way at several sites, or a parameter threaded through every call site means the behaviour has no owner yet: declare the base or mixin (an intermediate with `declared_name=None` in a family) and let members inherit it, overriding only their difference. The first bullet and correct-maintenance.md give why. Tristan, 2026-11-02, after claude-ai threaded a registry snapshot through seven call sites: "If it needs wiring in two places sound like ur not using inheritance", "Every inheritance use is duplication removal", "with MI you aren't forced to have tree structured inheritance", "Inheritance isn't an opinion", "the inheritance forces behavior in a single source so it can't drift".
- Any `if`, `isinstance`, kind string, flag, `None` check, roster or dict keyed by kind that selects behaviour is slop unless it is purely leaf algorithmic logic (arithmetic on a value that never selects behaviour). The fix: a concrete method on the base that answers the default (`()` or a no-op) and an override in the one member where it differs; membership derives from the registry filled at `__init_subclass__`. A type test is slop when the value is already held as a family member, because dispatch on it would answer; on our own classes that is every such test. The one permitted test is the decoder at the boundary (FieldCodec/MroDispatch, `__eq__(self, other: object)`, a codec input), where the value is foreign until tested, whatever class it is tested against; it turns the value into our family, and any second test downstream is BOUND-1. Source: paper1 §1 (slop restates a fact the language derives natively) and §2 (the decoder). Tristan, 2026-10-24: "ifs are almost always slop unless purely leaf algorithmic logic"; "just add a concrete method that does pass and override it in the subclass"; "isinstance can be replaced via just calling a polymorphic type".
- **A thing that has a state is the state.** A parallel pair (`Deal` and `DealState`, `X` and `XManager`, `Foo` and `FooInfo`) is two owners for one identity. Make the thing inherit the state family instead: `RuleDeal(State, DealtCalls)` answers `state()` itself. Tristan: "Deal is also a DealState because it inherits State… we don't have Deal and DealState, that slop. That's using MI."
- **Consumers know only the family.** Members exist to be declared; everyone else holds the family base and calls its methods. A caller that names a subclass, tests for one or imports one to branch on is restating the family's dispatch. Tristan: "the family holds everything, subclasses exist for declaration only, but everything else should be blind to subclass instances."
- **Empty classes are the clean form.** A class whose whole body is a docstring and whose meaning is its base list or class keyword (`class ReadsDealtSources(ToolGate, owes=(Unread,))`) is the factoring finished, not unfinished. When the correct version looks suspiciously empty and you want to add a method back, that is the flinch. Tristan: "you keep flinching when it's too clean"; "it should feel like things flip in. You might end up making classes that legit just say pass so that one other class can distinguish them."
- **Recognizing slop and determining its replacement are separate judgments.** A site can visibly restate a fact before its proper owner is located or implemented. Name the fact and the defective encoding immediately. Then trace the owner and all consumers to determine the coherent repair. Uncertainty about where the fact belongs does not excuse an encoding already shown to be slop; complete tracing is required before structural edits, not before recognizing a demonstrated restatement.
- Siblings keep only irreducible differences. Every review diffs the siblings; an edit adding behaviour to one sibling only goes back.
- No strings for identity, no `Any`, no new environment variables: typed objects derived from the owner. Tristan, 2026-10-19: "we fucking hate strings, especially for identity, it's web dev crayon eating slop"; 2026-10-12: "we shouldn't allow Any".
- **A local import is slop hiding an import cycle.** An `import` inside a function or method exists because the same import at module top would close a cycle, and that cycle is a declaration living in the wrong module: a family that low-level code needs is declared beside high-level code that opens screens or starts processes. Move that declaration into its own module, so every import sits at the top and the graph runs one way. Never add a local import to get past a cycle, and an existing one is a cycle to break, not a pattern to copy. On 2026-11-06 claude-a-toad wrote `from toad.screens.main import MainScreen` inside `AgentAttached.open_at_launch`, one of eleven such imports in Toad's src. The cycle they all hid was `widgets/comms_transfer.py` importing `application_lifetime` only for the app's exit-outcome family. That family also shared the name `TerminalOutcome` with the shell terminal's family (IDEN-2). Moving it to `application_outcome.py` removes that edge; every remaining inline `MainScreen` import is a cycle still owed, traced the same way. Tristan, 2026-11-06: "no inline imports"; "local imports are slop hiding import cycles".
- **A literal restating an owned fact is slop, and "hard coding" is the word for not saying so.** A constant, a generic failure name, a path, a version, a timeout or a default written where the program already holds the value is a required fact supplied outside its declaration (rules/correct-maintenance.md): the same slop as a helper or a repeated branch, with the copy being a literal instead of a statement. The euphemism describes a technique, as though there were a choice to weigh and a judgement to make per site; there is not, so say slop and delete the copy by reading the fact from its owner. A literal is correct only as a leaf value nothing else owns, such as an arithmetic bound that selects no behaviour. Tristan, 2026-11-05: "Hard coding was bad whenever you see hard coding replaced that by slop ... that's obvious that hard coding is always bad."
- A name gluing a domain type to a mechanism (`StepFieldNavigationDriver`) is a flattened cross product. Tristan, 2026-10-12: "Step already exists. If there's a StepField, is there a PipelineField? and FunctionField? ... So it's all kinds of wrong." Code already in the tree is never precedent.
- **Design the family first, then compare.** Before editing, stop trying to make the existing code work. Write down the family the requirement actually has (the base, the members, what each declares, what consumers call), then diff it against the tree; the gap is exactly the slop to factor out. Tristan: "just follow me, stop focusing on making what's here work, focus on what makes sense for real first, then write it down, then compare."
- Silent skips, unnamed tuples, fallbacks and broad `except` returning a default hide the missing owner and are blocking: fail loud and restore the owner. "it's garbage, I don't want it there, all it does is cause problems".
- A bug that needs fixes in several places is the restated fact showing itself: find the copy and delete it, then the fix is one spot. Tristan, 2026-10-21: "literally most bugs are due to slop". A run of bugs is grouped by the authority it signals and fixed there; a detector gate for the bug class is the quick-fix shape, not the fix. Tristan, 2026-10-25: "whack-a-mole happens when you refuse to find the real nominal authority and derive information from it"; "bugs are signals for a bigger issue".
- Line growth or file size is not slop; only a restated fact is. Raw scanner counts are candidates; rules/refactor-game.md owns scoring verified repairs.
- Nothing on a hot path scales with history, threads or message rate: "it shuold decode from latest to oldest or jsut decode what it needs so that tis always fast".
- Structural cleanup in the touched code goes in the current change; a platform gap is fixed at its owner. Tristan, 2026-10-19: "fuck narrow fixes have you not been listening to me?"
- A scratch script calling a method never keeps it: "just because a script calls shit to be deleted doesn't mean u put it back, u fix the scripts."
- Expose an external tool's whole surface: "there's no reason why we aren't exposing the same amount of functions. remember there's no real limits."
- No exemption for a metaclass reading its bases in `__new__`: the OwnTypeChecksPhase FamilyMeta sites (declared_family.py) get a structural fix. Tristan decided it on 2026-10-23.
