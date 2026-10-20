---
name: optimize-for-slop-elimination
description: Owner directive: optimize for eliminating all slop; it has a right answer, measured in restated answers, driven to zero
type: feedback
---
Optimize for eliminating slop entirely ("slop genocide"), across OpenHCS and its libraries, tests included. Slop = a required answer supplied outside the mechanism that would derive it (paper 1). It has a right answer, so it is removable, not a style preference.

**Why:** the owner spent nights forcing Codex to stop generating slop: without a whole-codebase view it kept adding new files and classes instead of finding the existing owner, then drowned in its own output. "it's garbage, I don't want it there, all it does is cause problems; if we know how to avoid making it then it's not justified." There is a right answer; being wrong just means a check fails and gets fixed.

**How to apply:**
- Slop cannot be counted from the code: a count needs the required relation, i.e. the right answer (owner, 2026-10-12; P1 Cor 4.10, Prop 5.1, Thm 6.1). Scanners (ratchet, overlay, NRA, line/method counts) only produce *candidates*: where to look. Never report a scan as "slop remaining" or as a score.
- The reliable record runs the other way: after a surface is factored correctly, note in its receipt what was required, what now derives it, and which outside answers were removed (deletion certificates, "answers removed"). Report progress as concepts that now have one owner, with the receipt.
- Guards and the ratchet lock a shape in *after* it is known to be right; they are regression guards, not measures.
- Inventory before creation: no new class, file or registry without an AST search showing no existing owner of that fact.
- Tests are in scope (362k lines; expected values are a second copy of the spec).
- Still slow down for: tests-only deletions (paper 4; list them), scientific behaviour (owner decides), external contracts (exact).
See , [[review-papers-periodically]], [[refactor-aggressively-tests-too]].
