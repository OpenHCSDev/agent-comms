# S14 scheduler and accepted-input ownership — PR365

**328 production lines deleted; 543 added across15 production modules.** Source
`6a456105a47b56fdbeb49482dfac33b01ecc9c13`, based on merged main363/364
`11dcae27`. Boyle owns this work; parent owns merge and live installation.

## Ownership and deletion

- `GoalScheduler` owns the existing private ledger handle, goal-origin map,
  projection signatures, goal controls and READY recovery. These members and
  their old entrypoints are deleted from `TurnRunner`; runtime requests,
  configuration, input draining, owned turns and every test caller use the actual
  owner. No captured runner facade or duplicate store was introduced.
- The existing `ReservationRule` family owns independent goal/wake admission
  barriers. Shared shutdown/in-flight-wake checks are inherited through a common
  check declaration. Goal scheduling and queued waking retain their different
  occupancy/configuration requirements. There is no second rule roster.
- `GoalLaunchOwner` specializes the existing registry authority for unused READY
  recovery. It retains process incarnation, thread incarnation, admission,
  worktree, running presence and active-goal checks. Registry aliases are resolved
  before identity comparison. It deliberately does not require an old active
  turn or goal revision; those are not READY-recovery authority.
- `QueuedInput` now owns its acceptance context using `OwnerIdentity` plus the
  accepted goal/wait snapshots. Current-context comparison is one value equality.
  The declaration owns live-handoff and future-receipt eligibility. Deleted the
  runner's eight-way handoff chain and the drain's piecewise identity comparisons;
  ordinary admission callers import the declaration directly. Exact input ID,
  reserved row, source text, no-active-turn, local process and running status are
  still required. UNKNOWN rows never create prompts; the wire lock and one turn
  lock still protect the same handoff and send boundaries.
- UI goal CAS uses existing `GoalRevision`, and owner preconditions now bind the
  actual `ProcessIdentity`, not a bare PID. Goal/registry/generation lifecycle
  declarations own their active/running/READY requirements.

Patterns: **IDEN-1**, **IDEN-3**, **IDEN-8**, **IMPL-14**, **TIME-3**. Current global
AGENTS and latest installed NRA/refactor-audit SKILL, pattern README and relevant
identity/implementation guidance were reread. Skills match the authoritative
archive checked in the preceding work checkpoint. No compatibility aliases,
codec subclass, new storage or durable-format conversion.

This is an authored semantic ownership transformation; no NRA DSL equivalence
proof is claimed. Resource assertion remains warning (root6.5GiB/home13.0GiB,
RAM14.5GiB/swap9.0GiB). No global scan, agents, large suite or paid provider calls.

## Executed evidence

All production imports for acceptance come from the **noneditable** installed
`.venv/lib/python3.14/site-packages/agent_comms` in this persistent worktree.
Native package:
`/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent`.
Only provider HTTP replies are controlled on localhost; routing, socket, registry,
SQLite, scheduler, ACP, native children, journals and send fences remain real.

| Evidence | Result and scope |
| --- | --- |
| `current-installed.txt` | **8 passed21.75s**, current merged-main package: saved-history ACP input -> failed native goal -> passive failure read -> explicit Retry through owner socket -> scheduler/wake/native continuation; separate real selected+fresh queued-input journey; READY admission/PID/process-birth refusal plus alias-rename recovery; UI grant creation and refusal to invent a missing grant. |
| `scheduler-native.txt` | **10 passed16.23s**: actual retry journey plus retry accepted once while an unrelated turn remains active, no overlap through success/error/EOF/exception/cancel outcomes and deferred goal continuation. |
| `installed-native.txt` | **20 passed18.46s**: real selected input/native path plus saved failure and focused registry/goal behavior before integrating363. |
| `focused.txt` |59 passed,1 skipped and one stale fixture failure subsequently corrected; actual native skipped case was explicitly executed in current-installed. Not claimed a green suite. |
| `retry-native-first.txt` |13 focused cases passed, initial native retry assertion failed: successful assistant output correctly records progress and creates READY generation3; the test had incorrectly expected generation2 BLOCKED. Actual success and exact fourth journal input are now asserted. |
| `ratchet.json` | No increases; **35 chain terms, six long conditions, eight foreign-absence probes removed**. Class excess above500 drops229 lines. No long condition remains in `turn_runner`, `input_drain`, or the three new declaration modules. |

The final continuous retry receipt records four localhost provider requests and
four actual saved native inputs. Retained history remains a byte prefix; the old
UNKNOWN row is unchanged. Retry generation2 records successful terminal progress
and creates generation3 READY. The isolated fixture disables further autonomous
wakes after that one observed continuation; it does not claim an active goal
should stop continuing in production.

Initial RED receipts are preserved. Two old session fixtures bypassed required
private-root/package configuration; they now use the existing canonical fixture.
The old foreign-key test changed `OwnedTurn.original_keys` after its immutable
source had been captured, so it never changed actual send authority. It now
changes that captured source and verifies native refusal. No production fence
was weakened to make the test pass.

Commands used `python -m pytest -o addopts=''` serially with both
`PI_COMPACTION_TEST_PACKAGE` and (for the real selected case)
`AC_NATIVE_COPIED_PACKAGE` set to the package above. Exact current test selection:

```
tests/test_goal_failure_native.py
tests/test_selected_owner_followup.py::test_actual_native_selected_and_followup_use_one_live_input_lifetime
tests/test_goal_standby.py::test_ready_recovery_rechecks_executing_owner_before_rotating
tests/test_goal_ui_creation.py
```

Ruff F/E9/I and diff whitespace checks pass. Caller search finds zero retired
runner goal APIs, old owner-PID preconditions or imports of QueuedInput through
the drain. The current remaining-plan mapping is updated in place; dated old
measurements remain historical.

## Remaining scope

No diagnosed blocker remains for this scheduler/handoff slice. It is ready for
parent integration and affected live verification, **not yet installed live** by
this worker. No CI hold. Other native/event S14 and whole T4 scopes remain with
their existing owners and Dalton's read-only all-requirement audit. No changes
to Wegener's selected/coordinated/startup files beyond merging his main commit;
no live root, launcher, package pin or user-input replay changes.

Owned noneditable test environment and caches removed after process-reference check;
source and receipts retained (`cleanup-owned.json`).
