# F0: Make the checks block

**Index:** [README.md](README.md). **First.**

## What is wrong

The ratchet runs in agent-comms' `debt-ratchet.yml` and Toad's `ci.yml`, and merges proceed when it fails. Read from each report's `delta` section:

| Merge | Increase it should have blocked |
|---|---|
| agent-comms #607 | +20 string-keyed reads; +3 foreign probes in `cli_commands.py` |
| agent-comms #520 | +2 long conditions; +13 chain terms in `compaction_records.py` |
| agent-comms #592 | +4 chain terms in `bus_page_index.py`; `WireLog` 10 lines further past its size threshold |

The Textual fork, with 218 commits in three days, runs no ratchet at all.

Separately, 22 of agent-comms' fixes since 1 October repaired a feature merged in the previous 24 hours: features reach `main` before the live path has run on them.

## Target

1. **Tristan's settings:** the ratchet is a required status check on `main` in agent-comms and Toad, with no administrator bypass. Post this to his queue; only he can change it.
2. **The Textual fork runs the ratchet** on its `src/textual` root with no-increase semantics: existing code stays as it is, and agent changes may not add to it. A new CI job, pinned to the same agent-comms commit Toad uses.
3. **Features take the live-path gate.** In `.pi/APPEND_SYSTEM.md`, the live-path rule covers features as well as runtime changes: a feature merges only after it has run on Tristan's saved session with his configured provider and the journeys for the flows it touches pass.

## Done when

A PR that fails the ratchet cannot merge in any of the three repositories, and the live-path rule names features.
