# Recorded admission recovery for comms428

The old channel attempt remains current. The newer DM owner remains untouched.
No recovery, stop, restart, prompt or provider operation ran for this checkpoint.

## Original contract and correction

At 783164547, PrivateSendAdmission._saved_session required the journal parent to
be root/native-sessions/recipient_lookup. Its raw writer recorded admission
before prompt bytes; session identity arrived later with the context receipt.
NativePiRpcLaunch.tracked supplied that directory through --session-dir.
Original recovery at 0ba40d991 checked this same launch directory. d15362ba1
later recorded the selected identity atomically at admission. Positive admission
without session identity is an original persisted state, not a missing identity
to replace with the current saved session.

OwnerReleaseReceipt declares that a later same-incarnation release fences older
recorded admissions. Its unconditional current-process/idle checks contradicted
that contract after replacement. Those checks now belong to
UnrecordedNativeAdmission, which still requires the exact stopped owner.
RecordedNativeAdmission preserves admission ordering, dead released process,
incarnation, participant identity and no admission regression. At the release
generation itself it still requires the exact stopped declaration.

RecordedNativeAdmission checks its selected file when present. Without the later
context receipt it retains the original directory lifetime check, never borrowing
the replacement selection. No durable declarations, input rows, codecs or journal
formats changed. UNKNOWN, publication refusal and replay refusal are unchanged.

AST declarations/calls across src/tests/tools parsed without omissions. The
family contains both admission members, OwnerReleaseReceipt, VerifiedOwnerLoss
and the two monitor recovery consumers; native exit checks remain with that owner.

## Actual read-only checks

Files under /home/ts/.cache/agent-scratch/mfc01/comms428-recovery:

- original-admission-check.json: real admission938 release passes while the
  newer DM process remains alive; unrecorded admission refuses that successor.
- native-lifetime-check.json: original VerifiedOwnerLoss acquisition and native
  absence check pass under wire/bus/registry exclusion. The old slot remains
  current, without frozen publication. Connection query-only; no settlement.
- check.stdout.log/check.stderr.log: the historical native recovery model fixture
  failed at setup before recovery. The batch was stopped, not repeated; no pass
  claimed. No remaining test process or child.

Parked field_codec.py changes are excluded from this checkpoint. Source imports
used the working checkout; installed confirmation remains necessary.

## Existing operator action after matched backend delivery

Parent owns installed backend delivery. Run with that installed interpreter,
without source PYTHONPATH. Do not stop/restart comms428 or resend the old input.

```python
from pathlib import Path
from agent_comms.coordinator import Coordination
from agent_comms.attempt_recovery import RecoveryMonitorCapability

root = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
execution = 'wirev162960e0b834144ac3c80087ca6e918abb489ff02cf88c6effd4c408a00377bc4'
with Coordination(root / 'coordination.sqlite3') as store:
    result = RecoveryMonitorCapability.abandon_released_native_attempt(store, execution).value
    assert not result.is_current and not result.can_retry
    assert not result.replay.replay_safe
    assert result.execution.reason_code == 'released_native_unknown'
```

The owner reacquires current facts and native absence, then atomically retires
only this original attempt as replay-unsafe UNKNOWN. Refusal leaves the slot
intact. After success verify the old pointer is retired, input/cursor records
unchanged and newer DM process alive. Channel progress still needs observation;
read-only admission proof is not a claim that channel delivery works.
