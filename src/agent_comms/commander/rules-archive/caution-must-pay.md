---
name: caution-must-pay
description: "overcaution is a defect, not diligence: every wait, gate, rehearsal or one-at-a-time step must name the specific failure it prevents that nothing else already catches; one that cannot is deleted. Stalling to look careful is the same failure as stalling."
type: feedback
---

**Why:** every wait, gate, rehearsal or one-at-a-time step costs throughput; one that names no failure nothing else catches buys nothing.

**How to apply:** before adding or keeping a wait, gate or serial step, name the failure it prevents that nothing else catches; if you cannot, delete it.

Tristan's words: overcaution caused most of today's problems; it is performative work that stalls instead of shipping.

Today's examples: installs waiting on idle turns (about three hours), a practice install required on every PR (merges from 26 an hour to 2), the word gate blocking file names, one failed turn parking a goal, storage strikes taking critical orders, the growth gate failing PRs, and landing PRs one at a time when drop-on-fail already covered the risk.
