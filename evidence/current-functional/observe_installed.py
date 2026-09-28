"""Read-only installed observation; no provider calls or input replay."""
import json
from pathlib import Path
from agent_comms.comms import wire
from agent_comms.private_bus_checkpoint import verify_private_bus_checkpoint_unlocked

state=Path('/home/ts/.local/state/agent-comms');c=wire()
report={'root':str(c.root),'links':{},'owners':{},'compaction_overrides':{}}
for name in ('toad','agent-comms','agent-comms-acp','agent-comms-agent','agent-comms-nk-foreground'):
 report['links'][name]=str((Path('/home/ts/.local/bin')/name).resolve())
with c.bus.log.locked():
 marker=c.bus.log._private_marker_unlocked()
 witness=verify_private_bus_checkpoint_unlocked(c.bus.log,marker)
 report['checkpoint']={'version':marker['checkpoint_version'],'through_seq':witness.through_seq,'latest_seq':marker['last_seq']}
for name in ('agent-comms-ux','pr95-selected-pi-summary-owner'):
 t=c.registry.require(name)
 report['owners'][name]={'status':c.registry.status(name).declared_name,'alive':c.owners._process_alive(t.pid),'local':c.owners._is_local_participant(t),'model':t.model,'thinking':t.thinking_level,'active_turn':t.active_turn is not None}
 path=Path(t.worktree)/'.pi/settings.json'
 report['compaction_overrides'][name]=json.loads(path.read_text()).get('compaction',{}) if path.exists() else {}
p=Path('/home/ts/.pi/agent/settings.json')
report['compaction_overrides']['global']=json.loads(p.read_text()).get('compaction',{}) if p.exists() else {}
report['roster_count']=len(c.registry.snapshot().threads)
assert all(v['alive'] and v['local'] for v in report['owners'].values())
assert all(v.get('enabled') is not False for v in report['compaction_overrides'].values())
assert report['checkpoint']['through_seq']==report['checkpoint']['latest_seq']
(state/'current-functional-installed-observation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
