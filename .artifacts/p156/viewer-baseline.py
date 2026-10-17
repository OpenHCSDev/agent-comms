"""Normal private viewer data and DM counts; no message or worker input."""
import json,time
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
s=Path('/var/tmp/agent-scratch-p156')
c=Comms(s,private_initial_writes=False,private_claim_writes=False)
t=time.perf_counter(); snap=c.views.viewer_snapshot(str(s)); print('viewer seconds',time.perf_counter()-t,flush=True)
encoded=FieldCodec.encode(snap)
Path(__file__).with_name('viewer-baseline.json').write_text(json.dumps(encoded,indent=2))
print('snapshot fields',encoded.keys(),flush=True)
print('dm codex',c.bus.pending_count('user','codex'),flush=True)
