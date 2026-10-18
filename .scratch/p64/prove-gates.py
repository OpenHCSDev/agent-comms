"""Run the existing CLI on six actual histories; never reimplement its measures."""
import json,os,subprocess,sys
from pathlib import Path
from agent_comms.debt_ratchet import (FunctionLocalImport,FrozenFieldWrite,LiteralGetattr,ForeignFieldSpellings,BroadExceptDefault,BannedVocabulary,git)
repo=Path('/home/ts/wt/core-doctrine-segment');toad=Path('/home/ts/wt/toad-live');stage=repo/'.scratch/p64'
def parent(project,head):return git(project,'rev-parse',head+'^').decode().strip()
def fork(project,head):return git(project,'merge-base',head,'origin/main').decode().strip()
cases=((FunctionLocalImport,repo,fork(repo,'7d0f27397'),'7d0f27397','src/agent_comms'),(ForeignFieldSpellings,repo,fork(repo,'9a6f3544a'),'9a6f3544a','src/agent_comms'),(BannedVocabulary,toad,fork(toad,'c73a10533'),'c73a10533','src/toad'),(FrozenFieldWrite,repo,parent(repo,'f83053f6a'),'f83053f6a','src/agent_comms'),(LiteralGetattr,repo,parent(repo,'1a62c4e16'),'1a62c4e16','src/agent_comms'),(BroadExceptDefault,toad,parent(toad,'652cd9924d'),'652cd9924d','src/toad'))
results=[]
for measure,project,base,head,root in cases:
    path=stage/f'proof-{measure.__name__}.json'
    with path.open('w') as out:
        done=subprocess.run([str(Path(sys.prefix)/'bin/python'),'-m','agent_comms.debt_ratchet','--root',root,'--base',base,'--head',head],cwd=project,stdout=out,stderr=subprocess.PIPE,text=True)
    assert done.returncode in (0,1),(measure.__name__,done.stderr)
    report=json.loads(path.read_text());prefix=measure.__name__+':'
    evidence=[{'path':key.removeprefix(prefix),'before':report['base'][key],'after':value,'new_line_matches':report['introduced'].get(key,0)} for key,value in report['head'].items() if key.startswith(prefix) and (value>report['base'][key] or report['introduced'].get(key,0))]
    result={'measure':measure.__name__,'repo':str(project),'base':report['base_revision'],'head':report['head_revision'],'exit_code':done.returncode,'own_gate_failed':done.returncode==1 and bool(evidence),'evidence':evidence,'output':str(path)}
    results.append(result);print(json.dumps(result),flush=True)
result={'core_sha':git(repo,'rev-parse','HEAD').decode().strip(),'gates_failing_on_rejected_heads':sum(item['own_gate_failed'] for item in results),'proofs':results,'scope':'AST shape checks; historical witness does not by itself prove a behavioral defect.'}
p=Path('/home/ts/.cache/agent-scratch/rts-results/p46.json');p.write_text(json.dumps(result,indent=2)+'\n')
assert result['gates_failing_on_rejected_heads']==6,result
