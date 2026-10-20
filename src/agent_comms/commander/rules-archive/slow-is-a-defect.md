---
name: slow-is-a-defect
description: "Treat slow tooling (ratchet, board, tests, installs) as a defect to profile and fix now, not a wait to sit through or explain"
type: feedback
---

2026-10-19: I explained to the owner why the debt ratchet takes 1–3 min (concurrent runs, a whole-tree report of about 850 KB) and listed fixing it as "worth an order later". Owner: "u should be impatient and ask yourself whether things are as slow as they are forced to be. make it less heavy if u can see it being less heavy."

**Why:** every slow tool is paid on every PR by every fork, so slowness compounds into most of the fleet's wall time.

**How to apply:** when I notice a slow step, profile it and dispatch the fix in the same tick; never only describe it. The test for any step: does it measure or do only what it must (changed files, cached blobs, batched queries)? Done 2026-10-19: forks for a fast ratchet (diff-scoped, blob cache) and a fast board (batched GraphQL) plus `threads`. See [[tooling-for-decisiveness]], [[structural-fixes-over-pushing-through]].
