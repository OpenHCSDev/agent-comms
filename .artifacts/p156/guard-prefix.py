"""Complete fresh clone bootstrap in the existing registry guard's required order."""
from pathlib import Path
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.wire_log import WireLog
s=Path('/var/tmp/agent-scratch-p156')
marker=WireLog(s/'bus.jsonl').read_metadata_unlocked(required=True)
p=s/'bus_meta.json'; pending=s/'bootstrap-bus_meta.json'
assert not pending.exists() and not (s/'.registry-owner-guard').exists()
p.rename(pending)
guard=PrivateRegistryGuard(s/'registry.json',marker.root_id)
guard.create_pending();guard.commit_initial()
pending.rename(p)
print('private cloned registry guard verified',guard.verify()[0])
