---
name: branch-vs-polymorphism
description: "HARD RULE: consumers know only the parent ABC; branching, kind strings, rosters and shape checks are presumed slop: the decision belongs on an ABC with subclass overrides and a registry filled at __init_subclass__"
type: feedback
---

Default verdict on any `if`/`isinstance`/kind string/flag/`None` check: the decision belongs on a type. Put the shared algorithm on the ABC or existing base, give the base the default behaviour, let subclasses override only what is irreducibly different, and compose cross-family residue as a mixin. Then delete the `if`. Absence or a mode is a state type, not `None` plus a check.

An `if` survives only when its arms are arithmetic on a value that never selects behaviour (a threshold, a count). Prove that in the review (`judge ok --no-owner`); do not assume it. Test: if adding a new case forces an edit to this `if`, it restates a declaration.

When a consumer needs to know which kind it holds, an owner is missing. Here is the shape that replaces it:
- The ABC declares the contract as abstract methods: only what is irreducibly different per kind.
- The ABC owns the shared algorithm as concrete methods that call those hooks, plus the shared validation and the defaults.
- A subclass holds only its hook implementations and its declared class facts, such as a key or an element.
- `__init_subclass__` registers each subclass from its declaration and validates its class facts when it is defined, so a bad declaration fails at import. Abstract intermediates skip registration.
- Lookup goes through one classmethod on the ABC. An unknown key raises.

Deleted by this shape, never kept beside it:
- kind strings, rosters, and dicts from kind to class;
- `if`/`isinstance` chains in factories;
- `None` checks for absence, since absence is a state subclass;
- mode flags;
- forwarders.

Adding a kind is one new class and no other edit. If adding a kind forces an edit anywhere else, that place restates the declaration.

Structural typing is the same defect. Duck typing, Protocols, `hasattr`, a `getattr` with a default, and `try: x.method()` used as a membership test never declare that X belongs to the contract. So nothing can enumerate the members (somebody writes a roster), every consumer re-checks the shape, and accidental matches pass. Membership is a nominal parent.
