---
name: local-patterns-not-precedent
description: Never copy a pattern because it is already in the codebase; judge against the standard, and treat repeated bad shapes as removal candidates
type: feedback
---
Do not follow a local pattern just because it is local. The standard is the owner's rules and paper results, not the surrounding code.

**Why:** Codex did this constantly: it made a mess, saw the mess, took the mess as licence, and repeated it. On 2026-10-12 a U6 agent added `StepFieldNavigationDriver` because `FunctionFieldNavigationDriver` already existed in pyqt-reactive, and I first approved it for "extending the existing family". Models (me included) copy the code around them more than they follow instructions.

**How to apply:** when reviewing or briefing, never accept "consistent with nearby code" as a reason. An existing repeated shape that fails the standard is a candidate to report and remove, not a template. Ask what fact the thing stores and who already declares it. Extending an existing family is right only after checking the family itself is the right shape (see [[glued-class-names]]). Removing bad exemplars matters because they get copied. Keep plans and notes that outlast the current prompt (receipts, DISPATCH.md, PAPER-RESULTS.md, memory), since context is lost at compaction and the tree keeps pulling toward its own habits. See [[optimize-for-slop-elimination]].
