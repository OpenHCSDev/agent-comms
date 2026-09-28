## Scope
Complete original S7 WireLog/Publisher ownership on main183. Real persistence, durability and publication behavior moved to the owners; all current core callers migrate directly.

- WireLog owns canonical path, sequence/metadata, JSONL append/read/snapshot, private validation and checkpoint interaction, fsync read barrier and guarded rewrite.
- Publisher owns routing, audience decisions, settings, claim/cohort/keyed transactions using that log.
- Delete bus_durability.py, fabricated MessageBus, old bus publication/persistence methods, send forwarding, _next_seq and _load_log adapters. No compatibility aliases or second store.
- Preserve PR176 checkpoint defaults, lock order, UNKNOWN outcomes, exactly-once receipts, saved wire formats and PR181 manual compaction.

## Local evidence
- Read/history/concurrency/ingress: 223 passed.
- Durability/receipt batch: 144 passed, 1 skipped; one migrated checkpoint fixture corrected and passed in the coordination batch.
- Coordination: 128 passed; old pre-PR176 cutover assertion corrected and passed in targeted acceptance.
- ACP/native seam batch bounded at 60 seconds after 47 completed cases; remaining scale cases split: all 5 pass; awareness/tools 28 pass. Partial runs never reported as complete.
- NRA full package context: 79 detectors, zero omissions, complete; one finding before and after (not a zero-finding claim or equivalence proof).

Parent owns paired Toad consumers and deployment. Exact mapping and evolving acceptance: evidence/s7-wire-log/HANDOFF.md and caller-map.json. No provider calls or live writes. CI deferred by owner.
