# C3: Owners report their own state

**Repository:** agent-comms at `1f5b1f3a`. **Index:** [README.md](README.md). **Patterns:** IDEN-3, IMPL-2, IMPL-7, IMPL-10, BOUND-1.

## What is wrong

**Other objects' state probed from outside,** by file (foreign probes, `None` checks): `coordination_response.py` (19, 23), `owner_lifecycle.py` (19, 18), `goal_actions.py` (15, 30), `backend.py` (13, 29), `turn_watchdog.py` (10, 14), `goal_management.py` (9, 14). Each probe restates a state its owner should report (IDEN-3). `typed_table.py` (10, 9) is A13's mechanism; its builder reviews those.

**Five small missing families,** from C0's site table:

- `goals.py::__post_init__` validates `resolution` against seven literals (`alias`, `limit_exceeded`, `malformed`, `non_executable`, `resolved`, …): resolutions are states, each carrying its own data (IMPL-10).
- `maintenance_barrier.py::current_unlocked` reads `state["phase"]` from a raw dict and compares it with `draining`, `installing`, `paused`, `ready`: a lifecycle (IMPL-2, BOUND-1).
- `relationships.py::edit` dispatches on `action` (`add`, `remove`, `update`), twice: a command family (IMPL-7).
- `thread_status.py::allows_control` compares tool names (`comms_start`, `comms_stop`, `comms_archive`, `comms_queue_restart`): which commands a status allows belongs on the commands, as a capability.
- `acp_failure.py::_error_detail` switches on the error payload's shape: decode it once, and let each shape render its detail.

## Target

The owner of each probed state gains the property or state class the probes reconstruct; the probes become one call. `owner_lifecycle.py` can use S15's `ThreadActivity` once it lands. The five sites above become families.

## Crossings

S14 still owns 41 long conditions, several in these files. **C3 runs after S14 in any file both touch, and never edits a chain.**

## Done when

The five sites are families; foreign probes in the six files fall to their true boundaries.

## Dispatch

> **`ac-c3`:** Complete C3 per `docs/refactor/cleanup/C3-owner-state.md`, file by file, after S14 has finished each shared file. Start with the five small families.
