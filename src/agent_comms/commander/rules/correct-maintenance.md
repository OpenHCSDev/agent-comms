---
name: correct-maintenance
description: "the index from each verdict to the result in the dealt papers that decides it: a verdict cites its theorem instead of restating it"
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
> run it back
> same constant, three files
> delete two, import the one
> commit: "delete two copies of the timeout"
> no helper, no agreement checker, tests still pass and now they mean it
> mfw "derived" was a git rm away

**Why:** the papers are dealt immediately before the rules, so restating their results here would be slop in the rule file itself; this rule owns only the mapping from each verdict the fleet gives to the result that decides it.

- **Slop**, a required fact (type identity, contract membership, implementation-class pair, state value) supplied outside its declaration: paper1 §1. Bans cannot lower the minimum: Lemma 1.1.
- **Forwarder, helper, delegate, registry or generator supplying a pair**: outside supply, its minimum fixed by paper1 Theorem 4.6; copies and replicas cannot lower it, and independently edited copies can disagree: Theorem 4.7.
- **Merging two owners**: only when every required answer agrees in every reachable state; equal fields today prove nothing: paper1 Theorem 4.8.
- **"The tests pass"**: successful runs and ancestry cannot reveal requirement shape: paper1 Corollary 4.10; for deletions they cover only the calls that ran: paper4 §1.
- **"It fits one parent today"**: strict insertion forces one-parent repair at the next tree-shaped update; multiple parents add one edge: paper1 Theorem 4.14.
- **Precedence and name conflicts**: required facts that survive every answer-preserving refactoring: paper1 Theorem 4.17.
- **"Adopting it costs too much"**: a finite adoption cost cannot erase a recurring workaround cost: paper1 Corollary 6.2.
- **"This is unnecessary"**: certifying a deletion is coNP-complete (paper4 Theorem 6.5), Σ2P-complete within a budget (Theorem 6.7), tractable through supplied structure (paper4 §4); rules/unnecessary.md.
