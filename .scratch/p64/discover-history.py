"""Select historical witnesses using the shipped measure implementations."""
import json
from pathlib import Path
from agent_comms.debt_ratchet import BroadExceptDefault,LiteralGetattr,git,python_paths
repo=Path('/home/ts/wt/core-doctrine-segment')
found={}
for head in git(repo,'log','--all','--no-merges','-100','--format=%H','--','src/agent_comms').decode().splitlines():
    base=git(repo,'rev-parse',head+'^').decode().strip()
    changed=set(git(repo,'diff','--no-renames','--name-only',base,head,'--','src/agent_comms').decode().splitlines())
    for measure in (LiteralGetattr,BroadExceptDefault):
        if measure.__name__ in found:continue
        before,after=measure.compare(repo,base,head,changed,'src/agent_comms/')
        growth={key:{'before':before[key],'after':value} for key,value in after.items() if value>before[key]}
        if growth:
            found[measure.__name__]={'head':head,'base':base,'growth':growth,'subject':git(repo,'show','-s','--format=%s',head).decode().strip()}
            print(json.dumps(found[measure.__name__]),flush=True)
    if len(found)==2:break
(repo/'.scratch/p64/discovered-witnesses.json').write_text(json.dumps(found,indent=2)+'\n')
assert len(found)==2,found
