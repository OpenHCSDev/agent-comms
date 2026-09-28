# UNKNOWN native execution recovery

Base: parent 844f0f6 (PF2 included). Own branch:
`fix/dead-unknown-execution-recovery-20260928`.
No live mutation, provider calls, user restarts or PR103 pushes performed.

## Implemented

- `coordination_store.py`: `RecoveryMonitorCapability.abandon_released_native_attempt`
  explicitly abandons a released, dead local backend using existing monitor
  settlement. When its send-admission epoch is absent, requires the exact current
  owner to be attested stopped/dead at the recorded release generation; also
  refuses any live native process for that session directory. Saved terminal
  evidence is not invented. Publication uncertainty is refused.
- `MutationStore.fail_unknown_attempt`: one transaction records UNKNOWN replay
  facts, local backend finality, failed execution/claim/obligation and clears the
  exact execution pointer. Reuses extracted existing `_advance_attempt` and
  `_record_replay` internals and `_settle`; no duplicate SQL/state store or nested
  transaction override.
- `durable_turn.py`: existing live turn owner calls that atomic store mutation.
- `coordinated_runtime.py`: future NativePiUnavailable FULL failures settle after
  native cleanup while holding canonical wire/bus/registry exclusion and the
  exact live turn witness. A revoked owner retains its unresolved attempt for
  monitor recovery and preserves the original exception.

Both paths retain UNKNOWN_EFFECTS, replay_safe=false and possible side effects;
neither creates native acceptance/proof/cursors nor retries an input. Backend
finality means the LOCAL backend is closed, not a known remote provider outcome.
Existing pending messages are unrelated work and may run when the owner resumes.
Parent owns notification wording and installed recovery.

## Exact operator call (parent only; not executed here)

Run with the newly installed runtime interpreter, without source PYTHONPATH.
The old UX71 reservation has no admission epoch, so normally stop idle UX first.
Do not edit SQL or clear a pointer manually. If the release/process checks refuse,
leave the evidence intact and inspect the refusal; do not force through it.

```python
from agent_comms.comms import wire
from agent_comms.coordination import ReplayFact
from agent_comms.coordination_store import MutationStore, RecoveryMonitorCapability

w = wire()
assert str(w.root) == "/var/tmp/agent-comms-live-20260927-wzjtqhza"
assert w.registry.require("agent-comms-ux").active_turn is None
w.owners.stop("agent-comms-ux")
execution = "wirev14ce9bdc54dbdcbb7d1f680ab765b8eca8572f6ae3f4cc7d39ab6e7068cea95f3"
with MutationStore(w.root / "coordination.sqlite3") as store:
    result = RecoveryMonitorCapability.abandon_released_native_attempt(store, execution).value
    assert not result.is_current and not result.can_retry
    assert result.replay.facts & ReplayFact.UNKNOWN_EFFECTS
    assert not result.replay.replay_safe
    print(result.execution.lifecycle.declared_name, result.execution.reason_code)
# Only after successful reviewed retirement; this sends no replacement prompt.
print(w.owners.start("agent-comms-ux"))
```

## Focused acceptance

Final `atomic-store.log`: **73 passed in 11.36s**. Command:

```sh
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tmp timeout 60 \
 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest \
 -o addopts='' -n 0 tests/test_native_unknown_recovery.py \
 tests/test_native_failure_recovery.py tests/test_coordination_store.py \
 tests/test_coordinated_runtime.py::test_selected_startup_changed_after_state_denies_before_fake_raw_byte \
 tests/test_coordinated_runtime.py::test_historical_native_input_view_omits_no_wake_and_reserved_unknown \
 tests/test_coordinated_runtime.py::test_full_input_crash_leaves_no_publish_and_no_automatic_restart -q
```

Covers real exited owner subprocesses, missing/admitted reservation, unchanged
native input/cursor/bus data, live child/owner and corrupted release refusal,
stale fences/revisions, frozen publication, injected atomic rollback and new
independent work after recovery. A real local RPC subprocess rejects get_state;
the actual native reader/cleanup reaps it before live settlement, then a new
input completes through real registry/bus/coordination with only model output
faked. No provider acceptance claimed. Historical fixtures intentionally retain
the old unresolved failure shape for recovery coverage.

`initial.log` (28 pass) and `focused-repair.log` (33 pass) retained. Failed
`live-settlement.log` exposed nested transactions (3 failed/88 passed); corrected
by the store-owned atomic operation, with all three failures explicitly rerun.

## Changed production paths / remaining scope

`src/agent_comms/{coordination_store,durable_turn,coordinated_runtime}.py` only.
Tests: native_unknown_recovery (new), native_failure_recovery,
test_coordination_store, test_coordinated_runtime. No feedback/UI edits.
Parent owns installation and actual UX71 operator recovery, then observation of
pending user messages. CI deferred; no additional provider send needed by worker.
