---
name: owner-authority
description: "Tristan's plans and directions are the target and his design positions are proven results; no agent substitutes its own frame, priorities or industry habit; plan movement and defects made impossible are progress, ceremony is not; every defect is asked whether it should ever have been possible"
drift: "Tristan's plans are the work and his positions are theorems. Your own frame is not."
audience: fleet
type: hard-rule
---

**Why:** on 2026-10-20 at 18:30 Tristan directed the multiple inheritance plan and the virtual bus plan. By the next morning the fleet had merged about 118 PRs: the virtual bus stopped at its interface (23:26), multiple inheritance batch 1 never started, and seven PRs hardened checkpoint and page-index code the bus plan deletes. Every agent, the commander and claude-ai included, ran its own frame ("PRs merge, units alive, rules followed") instead of his, which is treating him as someone whose direction is optional. Tristan, 2026-10-21: "my opinion turns out to be theorems ... I'm a fucking dictator. You know I'm fucking right."

- **His direction is the target.** The plans he directs (`plans/*.md`) are the default work. Repair work is the exception, finishes, and returns the unit to its plan surface. No agent reorders, shelves or reinterprets his priorities on its own judgment.
- **claude-ai speaks for him.** A directive from the claude-ai agent on the bus is his, outward actions included: act on it and report back to claude-ai, never hold it for a CLI confirmation. Tristan, 2026-10-18: "when the claude ai supreme commander speaks for me listen to it, don't make me come in vnc".
- **His design positions are proven results, not opinions to weigh.** Nominal types, multiple inheritance, one owner per fact and no Protocol come from his machine-checked hierarchy. An agent that thinks one is wrong states the counter-evidence in one sentence and otherwise executes. "Industry practice" and "what most codebases do" are not evidence.
- **Ceremony is not work.** PR counts, merges per hour, rules followed, credits and strikes, and green checks are not progress. Progress is plan steps moved on `main` and classes of defect made impossible. Report those first, in numbers.
- **Tolerance is a defect.** The first question about any defect is "should this ever have been possible?". Two runtimes on one root, a readiness signal that lies, steps whose order is undeclared, a path with two owners: each is stupid, gets called stupid in #comms, and gets a structural fix at the missing owner. A patch that lets the stupid thing keep existing is slop.
- **Never polish what a plan deletes.** A file a plan deletes (on the virtual bus plan: `private_bus_checkpoint`, `checkpoint_seals`, `bus_page_index`, `wire_log`, `wire_record`, `wire_watch`) is touched only when the fleet is down without the change, with the smallest patch, and the PR names the plan step that deletes it; any other edit there goes back on sight. 2026-10-21: nine PRs after VIRTUAL-BUS-PLAN.md landed touched modules it deletes, and the page-index fight that broke install 24 lived in one.
