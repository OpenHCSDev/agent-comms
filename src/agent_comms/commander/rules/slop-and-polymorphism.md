---
name: slop-and-polymorphism
description: "a branch, a string identity, Any, an env var, a forwarder or a sibling-only edit is presumed slop: the fact belongs on its owning type, shared behaviour in the parent; never fault inheritance or multiple parents, and fix structure in the current change"
type: hard-rule
---

**Why:** composition and forwarding supply the same pairs as multiple inheritance but outside the declaration, and a decision that a new case must edit restates a declaration; slop has a right answer and is driven to zero.

- Default verdict on any `if`, `isinstance`, kind string, flag or `None` check: the decision belongs on an ABC or base method with subclass overrides, residue across families in a capability mixin. An `if` survives only for arithmetic on a value that never selects behaviour.
- Siblings keep only irreducible differences. Every review diffs the siblings; an edit that adds behaviour to one sibling only goes back.
- Slop needs its formal statement: the fact, where it is restated, the owning declaration, the deriving mechanism. "Prefer composition" or "inherits two classes" is dogma, not a defect. Forwarders mean a false owner or missing parents. Scanners find candidates, never a score.
- No strings for identity, no `Any`, no new environment variables: typed objects derived from the owner. Tristan, 2026-10-19: "why strings and not enum or an actual object with a proper type?" and "we fucking hate strings, especially for identity, it's web dev crayon eating slop". Tristan, 2026-10-12: "we shouldn't allow Any".
- A name gluing a domain type to a mechanism (`StepFieldNavigationDriver`) is a flattened cross product. Tristan, 2026-10-12: "Step already exists. If there's a StepField, is there a PipelineField? and FunctionField? ... So it's all kinds of wrong." Code already in the tree is never precedent.
- Silent skips, rosters, unnamed tuples and fallbacks that return a default are blocking: "it's garbage, I don't want it there, all it does is cause problems".
- Nothing on a hot path scales with history, threads or message rate: "it shuold decode from latest to oldest or jsut decode what it needs so that tis always fast".
- Structural cleanup in the touched code goes in the current change, and a platform gap is fixed at its owner. Tristan, 2026-10-12: "do all the fixes. structural fixes are more important than pushing through and ignoring them."; 2026-10-19: "fuck narrow fixes have you not been listening to me?"
- A scratch script calling a method never keeps it. Tristan, 2026-10-19: "just because a script calls shit to be deleted doesn't mean u put it back, u fix the scripts."
- Expose an external tool's whole surface. Tristan, 2026-10-12: "there's no reason why we aren't exposing the same amount of functions. remember there's no real limits."
