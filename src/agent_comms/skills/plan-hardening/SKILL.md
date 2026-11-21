---
name: plan-hardening
description: Turn a living plan into a mechanical, executable one — literature and infra-reuse search, codebase mapping with an inheritance-hierarchy domain model, dry run against the tree, then execute. Each step leaves an artifact in the plan, never in your head.
---

A plan is aspirational until every thread names a deliverable that can be true in the tree and the
path to it is mostly mechanical. This is the method for getting there, in order. Skipping a step is
allowed once; skipping it twice means the plan still carries a guess where a fact should be.

1. **De-aspirate the plan.** Every thread states its deliverable as a thing that can be true in the
   tree — a declared family, a landed file, a measured verdict — never a hope. A thread with no
   deliverable is a note, not a thread; demote it or kill it.
2. **Search the literature and the infrastructure.** For each thread: who else did this, what
   exists to reuse (repos, checkpoints, formats, kernels), and what their measured results were.
   Links, reusable pieces and numbers go in the plan. A thread a paper already solved gets the
   paper's answer, not a new build. A thread with no prior art gets its "build, not download" cost
   stated.
3. **Reread the whole plan.** Kill what step 2 made redundant; sharpen what step 2 changed. The
   plan shrinks here more often than it grows.
4. **Search the codebase, and ask.** For every plan concept, find the owning declaration —
   file:line — or confirm it does not exist and must be built. Share with the implementation owner
   and get their map back: what is mechanical, what the code already does that the plan did not
   know (the launch-record lesson: the substrate's declaration may exist and be running while the
   plan says "build it").
5. **Domain mapping.** Draw the inheritance hierarchy the plan implies: the families, their
   members, who owns what — with every relevant filepath. This is the map the implementer eats;
   if a concept cannot name its file, it is not ready to be eaten.
6. **Review the whole plan again**, now against codebase truth instead of plan-time guesses. Every
   line either cites a file or names the build that creates the file.
7. **Dry run against the codebase.** Walk each step in a scratch checkout; every step names the
   file it touches and the check that would catch failure. No actual blockers remain: each one is
   resolved, assigned to an owner, or renamed a precondition the plan states.
8. **Execute** only when everything is sound and mostly mechanical. Ambition lives in the plan;
   mechanics live in the execution.

## Failures each step exists to catch

(1) building a hope (a thread nobody de-aspirated), (2) rebuilding existing work, (3)
carrying what the search killed, (4) planning against a codebase you summarized instead of read,
(5) concepts with no owner, (6) file-less lines, (7) blockers discovered mid-build, (8) executing
a plan that still has guesses in it.
