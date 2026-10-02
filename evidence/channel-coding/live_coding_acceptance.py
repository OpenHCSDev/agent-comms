"""Actual configured-provider coding turn in an owned persistent work directory."""
import asyncio
import faulthandler
import json
import os
import sys
import tempfile
import time

faulthandler.dump_traceback_later(45, repeat=True)
from dataclasses import asdict
from pathlib import Path

from agent_comms.operations import Comms

from agent_comms import Thread
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination


async def main():
    evidence=Path(__file__).resolve().parent
    run_name=sys.argv[1] if len(sys.argv)>1 else 'live-coding'
    receipt=evidence/(run_name+'-result.json')
    assert not receipt.exists(), 'Inspect prior attempt; do not replay'
    work=evidence.parents[1]/'.artifacts'/run_name
    work.mkdir(parents=True,exist_ok=False)
    (work/'input.txt').write_text('reference=ORCHID-7301\nstate=BEFORE\n')
    root=Path(tempfile.mkdtemp(prefix='agent-comms-coding-check-',dir='/var/tmp'))
    c=Comms(root,private_initial_writes=True)
    for name,tags,model in [('sender',frozenset(),None),('coding-check',frozenset({'coding-check'}),'openai-codex/gpt-6-sol')]:
        c.register(Thread(name,tags,str(work),pid=os.getpid(),model=model,task='Implement the requested local coding acceptance task',thinking_level='medium'))
    root_id=c.initialize_private_initial_protocol();c.initialize_private_claim_protocol()
    source=c.send_message('sender','#coding-check','For the coding-check owner: use read to read input.txt; use edit to replace BEFORE with AFTER; use write to create nested/result.txt containing the full edited input text; use bash to run a Python assertion that both files have equal contents and state=AFTER. Use all four tools. Reply CODING_TOOLS_OK only after the checks pass. Work only in this worktree.')
    initial=c.bus.read_delivery_cohort(root_id,source.seq)
    with Coordination(str(root/'coordination.sqlite3')) as store:
        install_private_cohort_schema(store);install_private_response_schema(store);install_native_runtime_schema(store)
        for person in initial.audience.recipients:
            store.participants.register(person.recipient_lookup,person.canonical_thread,person.canonical_thread,committed=True)
        accept_delivery_cohort(c.bus,root_id,source.seq,store)
    package=Path((evidence/'physical-package.txt').read_text().strip())
    report={'root':str(root),'worktree':str(work),'package':str(package),'source_seq':source.seq,'started_at':time.time()}
    receipt.write_text(json.dumps(report,indent=2)+'\n')
    try:
        result=await run_one_sealed_claim(root,wire_root_id=root_id,owner_name='coding-check',native_package=package)
        report['result']=asdict(result) if result else None
        report['files']={str(p.relative_to(work)):p.read_text() for p in (work/'input.txt',work/'nested/result.txt') if p.exists()}
        report['claims_remaining']=list(c.claim_projection())
        report['messages']=[{'seq':m.seq,'body':m.body} for m in c.full_history()]
        assert report['files']=={'input.txt':'reference=ORCHID-7301\nstate=AFTER\n','nested/result.txt':'reference=ORCHID-7301\nstate=AFTER\n'}
        assert not report['claims_remaining']
        assert any(m.body.strip()=='CODING_TOOLS_OK' for m in c.full_history())
        report['passed']=True
    except BaseException as error:
        report['error']=repr(error)
        raise
    finally:
        report['finished_at']=time.time();receipt.write_text(json.dumps(report,indent=2,default=str)+'\n');print(json.dumps(report,indent=2,default=str),flush=True)

asyncio.run(main())
