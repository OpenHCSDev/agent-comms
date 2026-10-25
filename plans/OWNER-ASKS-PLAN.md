# agent-comms: the owner's unowned asks, on the board

Opened: 2026-10-25, at `7f0923fca` (main).

## The thesis

An ask Tristan made that no order holds lives only in someone's memory, and memory is a second copy that disagrees with nothing until it is lost. Each ask here is a phase: the heartbeat board prints `START owner_asks/<phase>` for it until its evidence holds on `origin/main`, so the ask has one owner, the board, and nobody restates it.

## Phases

Each phase and its evidence on `origin/main` are declared in `src/agent_comms/plan_phases.py` (`OwnerAsksPlan`). No phase waits on another.

| Phase | Ask | Done when, on main | Now |
|---|---|---|---|
| `keymap_k2_k7` | Toad keymap K2 to K7 (toad#1212, `KEYMAP-PLAN.md` in Toad): remappable F1/F2, bindings that change with focus, a PC-game-style binding menu (K7) | this document records the merged Toad commit (below) | not recorded |
| `page_quarter_scroll` | plain PageUp/PageDown in Toad scroll a quarter page; toad#1233 did only Shift+PgUp/PgDn | this document records the merged Toad commit | not recorded |
| `loading_spinner` | Toad's top loading spinner is back; both were removed instead of keeping the top one | this document records the merged Toad commit | not recorded |
| `commander_latency` | each `agent-comms-commander` verb answers in under 5 s (about 30 s now) | `test_commander_verb_answers_under_five_seconds` declared in `tests` | absent |
| `delivery_latency` | a bus message is injected into the recipient's next tool call; delivery slowed at about 01:57 UTC on 2026-10-25 | `test_message_injects_into_next_tool_call` declared in `tests` | absent |
| `ram_budget` | desktop free RAM fell to about 2.2 GB under the fleet: find the owner and cap it | `test_fleet_memory_stays_under_its_cap` declared in `tests` | absent |
| `unflatten_modules` | plan the package structure of the 373 flat modules in `src/agent_comms`, after the fleet is stable | class `PackageStructurePlan` declared in `plan_phases.py` | absent |
| `board_line_lock_text` | board lines say "LOCKS your writing verbs" although #3345 made `HeartbeatGated` (`commander/order.py:843`) print the board instead of refusing; the text must derive from what the gate does. JUDGE lines embed each verb's full argparse usage | no `LOCKS your writing verbs` in `src` (now `agent_comms_hooks/board.py:34`); no `verb\.usage\(\)` in `commander/standing.py` (now `:167`, `:382`) | 1; 2 |
| `closed_unmerged_prs` | a standing rule flags a PR closed unmerged that still holds commits nobody landed (lost: toad #1080, #1095, the commander board page) | class `ClosedUnmergedPullRequest` declared under `src/agent_comms` | absent |

### Toad asks

Phase evidence reads only the plan repository's `origin/main`, and Toad's tree is not in it. Until an evidence kind reads another repository's main, a Toad phase is done when the Toad PR has merged and its merge commit is recorded here as a line of its own, in the list below, starting with a dash, the phase name, the word `landed:`, and `OpenHCSDev/toad@` followed by the full 40-character merge SHA. The pattern is declared once, on `ToadAskPhase`.

Landed:

### Latency and memory asks

Each is done when a test pins the target, so a regression fails the suite rather than an owner's patience. The test's name is the evidence; the test must measure the real path (a commander verb end to end; a message sent to a live recipient and seen in its next tool call; the fleet's resident memory against a declared cap on its owner).
