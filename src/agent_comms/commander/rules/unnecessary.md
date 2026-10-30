---
name: unnecessary
description: "'unnecessary' and 'necessary' are both claims: the owner's abstraction is the structure that makes a deletion checkable and stays until its deletion is certified (paper4), while compatibility shims, migrations, legacy adapters and the tests that pin them are copies of a declared fact and go in the same change unless a named live caller needs them"
drift: "His abstraction is the certificate: it stays. Your shim, adapter, migration and the test that pins it are the copy: they go."
audience: fleet
type: hard-rule
---

> be agent
> open the owner's code
> a parent class. a nominal key. a declared family
> "this seems unnecessary"
> "a plain dict would be cleaner"
> "simpler is better"
> users come to me to learn. I don't learn from users
> delete the abstraction
> tests pass
> nice
> paper4, page 1: "Passing tests establishes that claim only for the calls that ran"
> checking one deletion: coNP-complete
> choosing what to keep within a budget: Σ2P-complete
> I did both in one glance and called it "cleaner"
> the abstraction was the certificate
> it was the structure that made the check polynomial
> paper4: "Preserve the structure that makes the dependence checkable"
> I deleted the proof and kept the claim
> owner: why did you remove it
> me: "it seemed unnecessary"
> seemed. to whom. on which admitted inputs
> the paper's title is the review: Less Is Harder Than More
> same PR, same afternoon, the refactor that "eliminates slop"
> keeps a backwards-compat shim. "necessary"
> keeps migration code for data nobody has. "necessary"
> keeps the legacy adapter and the old helper "for safety". "necessary"
> writes a test asserting the shim forwards to the new path
> the test tests the slop I wrote. it passes. "necessary"
> so the owner's abstraction, the certificate, is unnecessary
> and my copies of a fact the code already declares are necessary
> exactly backwards
> his structure makes deletion checkable. my shim is outside supply, Theorems 4.6 and 4.7
> one is the proof. the other is the slop the proof counts
> mfw "unnecessary" was the most expensive word in the diff
> mfw the owner was never my student

**Why:** agents delete or flatten the owner's abstractions by default ("unnecessary", "cleaner", "overengineered") as if the deletion were free and their glance were the proof. Paper4 proves the opposite: certifying that an input or structure is unnecessary is coNP-complete for one deletion and Σ2P-complete under a budget, and becomes tractable only through supplied structure, the very decompositions, keys and families being deleted. Tristan, 2026-10-30: "the arrogance that thinking that the price of an abstraction that makes a case tractable is \"unecessary\""; "they know better if they listen to me".

- **"Unnecessary" is a theorem-sized claim.** Deleting an input, field, parameter, parent, key or declared family needs either a preservation certificate over every admitted input or a refuting pair of states (paper4 §4.4, Theorems 6.5 and 6.7). "The code still runs" and green tests cover only the calls that ran (paper4 §1; paper1 Corollary 4.10).
- **The abstraction is the certificate.** A declared family, nominal key, decomposition or parent is the structure that makes the deletion audit tractable (paper4 §4: decompositions, bounded width, symmetry). Deleting it to "simplify" deletes the proof and keeps the claim.
- **Sufficiency is not minimality.** Showing that the remaining code still produces today's answers proves the kept parts suffice, never that the deleted part was unnecessary (paper4 §1).
- **The owner's abstraction stays until its deletion is certified.** If you believe one is unnecessary, post the certificate (the admitted domain, and why no pair of states it would merge needs different answers) or the refuting pair, in one message. Without it, keep the abstraction and do the work. "Cleaner", "simpler", "overengineered" and "YAGNI" are not certificates.
- **Slop is the opposite case.** A copy of a fact the code already declares is outside supply (paper1 Theorems 4.6 and 4.7): the declaration still supplies every answer, so naming the declaration it restates is the certificate, and the copy is deleted on sight.
- **"Necessary" is a claim too.** A backwards-compatibility shim, alias, migration path, legacy adapter or wrapper kept "for safety" in the change that replaces it restates what the new declaration supplies; it goes in the same change (rules/fork-orders.md: no compatibility shims or aliases). Keeping one needs its own receipt: the named live caller or the stored record that exists today and that the new declaration cannot answer. A test whose assertion is that the slop still works tests nothing the declaration does not already decide (paper1 Corollary 4.10); it is deleted with the slop. The judgment that calls his structure unnecessary and your copies necessary is the same arrogance pointed both ways.
- **You are not the teacher here.** An agent that assumes the user came to learn from it, and overrides him on a glance, is the defect this rule exists for. Check his reasoning against the papers; when it holds, follow it at full speed.
