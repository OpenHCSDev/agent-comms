"""Explicitly abandon dead test71 with UNKNOWN preserved; never resend it."""
import json
from dataclasses import asdict
from pathlib import Path
from agent_comms.comms import wire
from agent_comms.coordination import ReplayFact
from agent_comms.coordination_store import MutationStore, RecoveryMonitorCapability

HERE=Path(__file__).resolve().parent
name='agent-comms-ux'
execution='wirev14ce9bdc54dbdcbb7d1f680ab765b8eca8572f6ae3f4cc7d39ab6e7068cea95f3'
w=wire()
owner=w.registry.require(name)
assert owner.active_turn is None, 'Do not interrupt an active user turn'
with MutationStore(w.root/'coordination.sqlite3') as store:
 before=store.snapshot(execution)
 assert before.is_current and before.execution.owner_thread==name
 assert {a.wire_seq for a in before.assignments}=={71}
 assert before.publication_intent is None
report={'old_sequence':71,'replayed_inputs':0,'previous_pid':owner.pid,'recovered':False}
w.owners.stop(name)
try:
 with MutationStore(w.root/'coordination.sqlite3') as store:
  result=RecoveryMonitorCapability.abandon_released_native_attempt(store,execution).value
  assert not result.is_current and result.replay is not None
  assert result.replay.facts & ReplayFact.UNKNOWN_EFFECTS
  assert not result.replay.replay_safe and result.replay.side_effects_possible
  report.update(recovered=True,state=result.execution.lifecycle.declared_name,
                replay_safe=result.replay.replay_safe,side_effects_possible=result.replay.side_effects_possible)
finally:
 restarted=w.owners.start(name)
 report['restart']=asdict(restarted)
 (HERE/'ux-slot-recovery.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report,indent=2),flush=True)
