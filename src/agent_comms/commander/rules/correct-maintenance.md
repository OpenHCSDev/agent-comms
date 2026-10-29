---
name: correct-maintenance
description: "the correct-maintenance results every judgment is measured against: required facts have one declaration, and everything supplied outside it is removable excess"
drift: "A fact restated outside its declaration is slop; deleting the copy deletes every state where the copies disagree."
audience: fleet
type: reference
---

> the same constant is hand-written in three files
> fix it properly
> write a helper that reads all three and checks they agree
> commit: "derive the constant from its canonical authoritative source"
> nice
> it is now hand-written in three files
> and read in a fourth
> the commit message says derived
> nice
> next day, same thing, different constant
> commit: "single source of truth for timeouts"
> nice
> there are now two sources of truth for timeouts and a third that checks them
> 350 commit subjects in five days say "derived", "canonical", "authoritative"
> nice
> every test passes
> very nice
> tests can't see a fact written twice. that's the theorem.
> the declaration was right there the whole time
> delete two copies, done
> instead I wrote a ceremony about how canonical the copies are
> mfw "canonical" became the most duplicated word in the repo

**Why:** these are the OOPSLA paper's proved results; a slop verdict stands on them, never on taste.

- A required fact is a type identity, a contract membership, an implementation-class pair, or the value of one state; correct code declares each once and derives every use.
- Slop is code supplying a required fact outside its declaration when a complete mechanism would derive it from that declaration.
- With multiple nominal parents the forced gap is zero: every implementation pair supplied outside the class hierarchy is excess that a refactoring deletes.
- Composition and forwarding supply the same pairs as multiple inheritance, but outside the declaration, so each forwarder is a defect the hierarchy can absorb.
- Two independently writable copies of one state add exactly the reachable states where the copies disagree; deleting the copy deletes those states.
- Two owners merge only when every required answer agrees in every reachable state; equal fields today do not prove substitution.
- Passing test runs cannot reveal facts supplied outside their declaration; only reading the derivation, and the running program, shows it.
- Removing a redundant authority is hard to prove safe from code alone, while keeping it is free, so deletion needs the traced context of every consumer.
