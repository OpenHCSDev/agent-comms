---
name: correct-maintenance
description: "the correct-maintenance results every judgment is measured against: required facts have one declaration, and everything supplied outside it is removable excess"
audience: fleet
type: reference
---

- A required fact is a type identity, a contract membership, an implementation-class pair, or the value of one state; correct code declares each once and derives every use.
- Slop is code supplying a required fact outside its declaration when a complete mechanism would derive it from that declaration.
- With multiple nominal parents the forced gap is zero: every implementation pair supplied outside the class hierarchy is excess that a refactoring deletes.
- Composition and forwarding supply the same pairs as multiple inheritance, but outside the declaration, so each forwarder is a defect the hierarchy can absorb.
- Two independently writable copies of one state add exactly the reachable states where the copies disagree; deleting the copy deletes those states.
- Two owners merge only when every required answer agrees in every reachable state; equal fields today do not prove substitution.
- Passing test runs cannot reveal facts supplied outside their declaration; only reading the derivation, and the running program, shows it.
- Removing a redundant authority is hard to prove safe from code alone, while keeping it is free, so deletion needs the traced context of every consumer.

**Why:** these are paper 1's proved results; a slop verdict stands on them, never on taste.
