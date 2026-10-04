# Fold-in, 4 October

**Heads:** agent-comms `main` at `28c8de1c`, Toad fork at `901771fc`, Textual fork at `2fac5395d`. Pattern IDs refer to the refactor-audit skill's catalog. Every finding below comes from reading the code; the counts only located it.

## Where things stand

The direction is right, and strongly so. Since the first audit on 28 September, per 1,000 production lines while the code grew 23% (agent-comms) and 14% (Toad):

| | agent-comms | Toad |
|---|---|---|
| Headline debt | 31.7 → 6.6 | 11.6 → 3.7 |
| Chain terms | 39.5 → 4.7 | 14.5 → 6.1 |
| String-keyed reads | 16.5 → 2.8 | 7.8 → 2.0 |
| String dispatch | 0.41 → 0.02 | 0.83 → 0.05 |
| `None` checks | 35.9 → 23.0 | 33.2 → 30.2 |

The reasoning has improved too. Replay safety now has one owner (`ReplayAssessments`), reached by deleting the turn-state event's copy; the compaction payload cap became one owned constant; #614 to #616 replaced re-reads of shared state with captured values flowing through pure transitions.

Three things still go wrong, all at edges:

1. **Enforcement lapsed.** The ratchet runs in CI but merges proceed when it fails: #607, #520 and #592 all fail it with real increases, and all merged. Since 1 October, chain terms (+26, +37) and string-keyed reads (+27, +23) crept back in both repositories.
2. **Features land broken.** 39 of agent-comms' 56 fixes since 1 October repaired something a change from the previous 24 hours broke; 22 of those changes were features.
3. **Facts leave their owners untyped.** Target actions cross into Toad as dicts (F1), session coverage is decided piecewise in five places (F2), and the context explorer keeps its lifecycle as optional fields (F3).

## Items

| Item | Fix | Where |
|---|---|---|
| [F0](F0-enforcement.md) | The ratchet blocks; features take the live-path gate; the Textual fork gets the ratchet | settings (Tristan) and new PRs |
| [F1](F1-target-actions.md) | Target actions as a typed API across both repositories | two lockstep PRs, agent-comms first |
| [F2](F2-session-position.md) | One session-position type owning coverage | new agent-comms PR |
| [F3](F3-explorer-state.md) | The context explorer's lifecycle as a state family | fold into Toad #417 |
| [F4](F4-flattened-families.md) | Families leaving their owners as strings, triaged and ratcheted | after F0 |

## Order

F0 first, since everything else regresses without it. Then F1, F2 and F3 in parallel. F4 last.

## Method, for every item

Reason first: trace the fact to its owner, storage, lifecycle and every consumer. Fix it at the owner, nominally. Delete every other copy in the same change. Tests and the live path come last, to confirm.
