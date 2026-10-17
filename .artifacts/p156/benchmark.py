"""Cold display checkpoints on the authorized authentic 2GiB prefix, no input."""
import importlib.util, json, sys, time
from pathlib import Path
from agent_comms.comms import Comms
from agent_comms.bus_display_index import BusDisplayIndex, DisplayMetricScope
s=Path('/var/tmp/agent-scratch-p156')
spec=importlib.util.spec_from_file_location('agent_comms.p156_before',Path(__file__).with_name('before_display.py'))
old=importlib.util.module_from_spec(spec);sys.modules[spec.name]=old;spec.loader.exec_module(old)
c=Comms(s,private_initial_writes=False,private_claim_writes=False)
results={}
for label,module in [('before',old),('after',sys.modules['agent_comms.bus_display_index'])]:
    index=module.BusDisplayIndex(s/'bus.jsonl','user')
    assert not index.path.exists()
    with c.bus.snapshot('user') as (selection,opened):
        semantics=module.DisplayMetricScope(selection.scopes,selection.viewer_names,selection.seen)
        started=time.perf_counter(); checkpoint=index.snapshot(opened,semantics); seconds=time.perf_counter()-started
    results[label]=dict(seconds=seconds,activity=checkpoint.activity,counts=checkpoint.counts)
    print(label,seconds,checkpoint.counts,flush=True)
    index.path.unlink()
assert results['before']['activity']==results['after']['activity']
assert results['before']['counts']==results['after']['counts']
results['prefix']=json.loads((s/'prefix-receipt.json').read_text())
Path(__file__).with_name('cold-times.json').write_text(json.dumps(results,indent=2))
print('EXACT_METRICS_MATCH',flush=True)
