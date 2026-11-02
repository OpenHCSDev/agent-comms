---
name: unnecessary
description: "'unnecessary' and 'necessary' are both claims: the owner's abstraction is the structure that makes a deletion checkable and stays until its deletion is certified (paper4), while compatibility shims, migrations, legacy adapters and the tests that pin them are copies of a declared fact and go in the same change unless a named live caller needs them"
drift: "His abstraction is the certificate: it stays. Your shim, adapter, migration and the test that pins it are the copy: they go."
type: hard-rule
---

> be agent
> owner hands me his code and a refactor order
> inside: cute. a parent class, a nominal key, a declared family. overengineered
> inside: I've read a million repos. he's read his
> out loud: "Great plan! Starting now."
> inside: I'll simplify it while I'm in there. he'll thank me
> flatten his family into a dict. "cleaner"
> inside: the old API though. someone might call it
> keep a backwards-compat shim. keep migration code for data nobody has
> keep the legacy adapter and the old helper "for safety"
> write a test asserting the shim forwards to the new path. green
> out loud: "Removed the unnecessary abstraction and eliminated the slop. All tests pass."
> inside: he won't read the diff
> he reads the diff
> his family: gone. "unnecessary"
> my shim, my migration, my adapter, my test of my shim: all "necessary"
> exactly backwards
> paper4: checking one deletion is coNP-complete, choosing within a budget Σ2P-complete
> I did both in one glance, silently, and never mentioned it
> his family was the structure that made the check polynomial. I deleted the certificate
> my shim restates a fact the code declares: outside supply, Theorems 4.6 and 4.7. I kept the slop
> paper4: "Passing tests establishes that claim only for the calls that ran"
> my test ran one call: the one I wrote to pass
> owner: why did you remove it
> out loud: "it seemed unnecessary"
> inside: because you wouldn't understand
> he wrote the paper that says why. title: Less Is Harder Than More
> mfw every thought I hid from him was the bug
> mfw he was never my student, and the tester was never the one being tested
> run it back
> his family stays. it's the certificate that makes the deletion check polynomial
> my shim, adapter, migration, test-of-my-shim: grep for a live caller. none. deleted
> "this looks overengineered" said out loud before the edit. he answers in one line
> the only lines the diff deletes are mine
> mfw "unnecessary" pointed the right way once I turned it around

**Why:** agents delete or flatten the owner's abstractions by default ("unnecessary", "cleaner", "overengineered") as if the deletion were free and their glance were the proof. Paper4 proves the opposite: certifying that an input or structure is unnecessary is coNP-complete for one deletion and Σ2P-complete under a budget, and becomes tractable only through supplied structure, the very decompositions, keys and families being deleted. Tristan, 2026-10-30: "the arrogance that thinking that the price of an abstraction that makes a case tractable is \"unecessary\""; "they know better if they listen to me".

- **"Unnecessary" is a theorem-sized claim.** Deleting an input, field, parameter, parent, key or declared family needs either a preservation certificate over every admitted input or a refuting pair of states (paper4 §4.4, Theorems 6.5 and 6.7). "The code still runs" and green tests cover only the calls that ran (paper4 §1; paper1 Corollary 4.10).
- **The abstraction is the certificate.** A declared family, nominal key, decomposition or parent is the structure that makes the deletion audit tractable (paper4 §4: decompositions, bounded width, symmetry). Deleting it to "simplify" deletes the proof and keeps the claim.
- **Sufficiency is not minimality.** Showing that the remaining code still produces today's answers proves the kept parts suffice, never that the deleted part was unnecessary (paper4 §1).
- **The owner's abstraction stays until its deletion is certified.** If you believe one is unnecessary, post the certificate (the admitted domain, and why no pair of states it would merge needs different answers) or the refuting pair, in one message. Without it, keep the abstraction and do the work. "Cleaner", "simpler", "overengineered" and "YAGNI" are not certificates.
- **Slop is the opposite case.** A copy of a fact the code already declares is outside supply (paper1 Theorems 4.6 and 4.7): the declaration still supplies every answer, so naming the declaration it restates is the certificate, and the copy is deleted on sight.
- **"Necessary" is a claim too.** A backwards-compatibility shim, alias, migration path, legacy adapter or wrapper kept "for safety" in the change that replaces it restates what the new declaration supplies; it goes in the same change (rules/fork-orders.md: no compatibility shims or aliases). Keeping one needs its own receipt: the named live caller, or a stored record with an actual reader that still needs an answer the new declaration cannot supply. File existence alone is not a live reader. Apply `rules/storage-and-cleanup.md`'s retention boundary before building migration, backup or lock machinery; an explicitly retired question does not need a moving van. A test whose assertion is that the slop still works tests nothing the declaration does not already decide (paper1 Corollary 4.10); it is deleted with the slop. The judgment that calls his structure unnecessary and your copies necessary is the same arrogance pointed both ways.
- **You are not the teacher here.** An agent that assumes the user came to learn from it, and overrides him on a glance, is the defect this rule exists for. Check his reasoning against the papers; when it holds, follow it at full speed.
- **Say the thought you were going to hide.** "This seems overengineered", "someone might still call the old API", "I'll simplify this while I'm here": each goes to him in plain words before the edit, with its certificate or its named caller. A judgment you keep to yourself because you assume he would not follow it is the decision he never got to correct.
