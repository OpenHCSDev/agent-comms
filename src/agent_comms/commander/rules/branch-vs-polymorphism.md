---
name: branch-vs-polymorphism
description: "branching is presumed slop: the decision belongs on an ABC or base method with subclass overrides; an `if` survives only when no type could own it"
type: feedback
---

Default verdict on any `if`/`isinstance`/kind string/flag/`None` check: the decision belongs on a type. Put the shared algorithm on the ABC or existing base, give the base the default behaviour, let subclasses override only what is irreducibly different, and compose cross-family residue as a mixin. Then delete the `if`. Absence or a mode is a state type, not `None` plus a check.

An `if` survives only when its arms are arithmetic on a value that never selects behaviour (a threshold, a count). Prove that in the review (`judge ok-unowned --no-owner`); do not assume it. Test: if adding a new case forces an edit to this `if`, it restates a declaration.
