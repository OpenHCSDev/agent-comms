# Cleanup plans, 29 September

**Heads:** agent-comms `main` at `1f5b1f3a`; Toad fork `main` at `26aff491`. **Rules:** each repository's `00-RULES.md`. Pattern IDs refer to the refactor-audit skill's catalog; measures are the skill's census and `merge_review.py`.

These plans clean up what the last two days of merges left behind. They claim only what no existing plan owns, and hand everything else to its owner.

## Evidence: two days of merges

`merge_review.py` measured every merge since the round-2 audit heads: 140 in agent-comms (from `15a4d00`) and 70 in Toad (from `511a1a2`), after minus before over the files each changed.

**agent-comms fell on every measure:** chain terms −1,259, string-keyed reads −623, `None` checks −378, long chains −190, `type()` checks −185, foreign probes −173, for +1,788 production lines. Debt that did arrive came mostly from fixes. The largest single source is **#400** (`feat/queued-owner-restart`): +19 string-keyed reads, +9 string comparisons, +8 foreign probes and two string dispatches.

**Toad fell on almost every measure,** with one exception: **`None` checks +46 and foreign probes +18,** nearly all from the workspace feature line. #129 alone added 86 `None` checks and 29 foreign probes.

**Six merges added dispatch** across both repositories (#400, #229, #273, #288 in agent-comms; #107, #125 in Toad). Nothing enforces polymorphism codebase-wide yet; C0 fixes that.

## Open pull requests

| PR | Production change | Before merging |
|---|---|---|
| agent-comms #406 | none (tests) | nothing |
| Toad #195, #201 | none (tools, tests) | nothing |
| Toad #197 | +2 `None` checks | acceptable |
| Toad #199 | removes 2 foreign probes, a `None` check and a comparison | nothing |
| **Toad #200** (frame tracing) | **+13 chain terms, +2 long chains, +13 `None` checks, +5 foreign probes, a `type()` check, a `getattr` with default** | fix before merge: tracing state belongs in one typed record, not flags probed from outside. The ratchet will reject it once Toad pins the agent-comms version carrying the chain-term and probe measures |

## The plans

| Plan | Repository | Removes |
|---|---|---|
| [C0](C0-enforce-polymorphism.md) | both | nothing directly: makes dispatch impossible to add, seals mechanisms, and decides each of the 38 existing sites |
| [C1](C1-pi-vocabulary.md) | agent-comms | pi's reasons, levels and payload shapes decided outside families; `pi_events.py`'s 39 foreign probes |
| [C2](C2-restart-queue.md) | agent-comms | #400's restart queue: dispatch on message text, an environment roster, raw records, retired epoch vocabulary |
| [C3](C3-owner-state.md) | agent-comms | owners probed from outside (`coordination_response.py`, `owner_lifecycle.py`, `goal_actions.py`, `backend.py`), plus four small missing families |
| [C4](C4-god-classes.md) | agent-comms | `HistoryViews` (1,012 lines) and `CommsAgent` (545) |
| [TC1](TC1-workspace-state.md) | Toad | the workspace line's `None` probing, in eight modules |
| [TC2](TC2-acp-specification.md) | Toad | the ACP specification's half of the boundary, still read as dicts, and the raw agent definitions |

## Handed to existing plans

- **Toad T4:** `widgets/conversation.py` has 33 foreign probes and 122 `None` checks, and `check_action` dispatches on action names. Both are `Conversation`'s own state, so they belong to T4.
- **Toad T5:** still largely unstarted: 6 row-kind comparisons and 32 uses of the transcript's lifecycle flags remain. It is now the largest open Toad surface, and TC1 must not touch its files.
- **agent-comms S14:** 41 conditions of six or more terms remain. C3 runs after S14 in the files both touch, and never edits a chain.
- **agent-comms D22:** `WireLog` (648 lines) waits on the wire-log rewrite decision.

## Order

1. **C0,** so nothing can be added while the rest is removed.
2. **Toad #200 fixed before it merges.**
3. **C1, C2 and TC2** at the boundaries, in parallel.
4. **C3 and TC1,** after S14 and T5 in any file they share.
5. **C4,** last, against classes the others will have shrunk.
