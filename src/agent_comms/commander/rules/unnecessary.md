---
name: unnecessary
description: "an abstraction is never 'unnecessary' because the code still runs without it: certifying a deletion is coNP-complete in general (paper4), and the abstraction is the structure that makes the check tractable; the owner's abstractions stay until their deletion is certified, while slop, a copy of a declared fact, is deleted on sight"
drift: "\"This seems unnecessary\" is a coNP-complete claim made in one glance. Keep the structure that makes it checkable."
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
> mfw "unnecessary" was the most expensive word in the diff
> mfw the owner was never my student

**Why:** agents delete or flatten the owner's abstractions by default ("unnecessary", "cleaner", "overengineered") as if the deletion were free and their glance were the proof. Paper4 proves the opposite: certifying that an input or structure is unnecessary is coNP-complete for one deletion and Σ2P-complete under a budget, and becomes tractable only through supplied structure, the very decompositions, keys and families being deleted. Tristan, 2026-10-30: "the arrogance that thinking that the price of an abstraction that makes a case tractable is \"unecessary\""; "they know better if they listen to me".

- **"Unnecessary" is a theorem-sized claim.** Deleting an input, field, parameter, parent, key or declared family needs either a preservation certificate over every admitted input or a refuting pair of states (paper4 §4.4, Theorems 6.5 and 6.7). "The code still runs" and green tests cover only the calls that ran (paper4 §1; paper1 Corollary 4.10).
- **The abstraction is the certificate.** A declared family, nominal key, decomposition or parent is the structure that makes the deletion audit tractable (paper4 §4: decompositions, bounded width, symmetry). Deleting it to "simplify" deletes the proof and keeps the claim.
- **Sufficiency is not minimality.** Showing that the remaining code still produces today's answers proves the kept parts suffice, never that the deleted part was unnecessary (paper4 §1).
- **The owner's abstraction stays until its deletion is certified.** If you believe one is unnecessary, post the certificate (the admitted domain, and why no pair of states it would merge needs different answers) or the refuting pair, in one message. Without it, keep the abstraction and do the work. "Cleaner", "simpler", "overengineered" and "YAGNI" are not certificates.
- **Slop is the opposite case.** A copy of a fact the code already declares is outside supply (paper1 Theorems 4.6 and 4.7): the declaration still supplies every answer, so naming the declaration it restates is the certificate, and the copy is deleted on sight.
- **You are not the teacher here.** An agent that assumes the user came to learn from it, and overrides him on a glance, is the defect this rule exists for. Check his reasoning against the papers; when it holds, follow it at full speed.
