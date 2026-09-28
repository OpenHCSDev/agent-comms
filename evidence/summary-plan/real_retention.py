"""Actual installed ACP turns with configured model; no synthetic native history."""
import asyncio,json,os,tempfile,time,sys
from pathlib import Path
from dataclasses import asdict
from agent_comms.acp import CommsAgent
from agent_comms.operations import Comms

state=Path('/home/ts/.local/state/agent-comms')
run=sys.argv[1]
receipt=state/(run+'.json')
assert not receipt.exists(), 'Inspect existing attempt; no replay'
project=Path('/home/ts/wt/comms-channel-coding-tools-20260927/.artifacts')/run
project.mkdir(exist_ok=False)
(project/'.pi').mkdir()
(project/'.pi/settings.json').write_text(json.dumps({'compaction':{'enabled':True,'reserveTokens':263808,'keepRecentTokens':512}})+'\n')
root=Path(tempfile.mkdtemp(prefix='ac-pr95-',dir='/var/tmp'))
report={'project':str(project),'root':str(root),'started_at':time.time(),'turns':[]}
receipt.write_text(json.dumps(report,indent=2)+'\n')
class Client:
 async def session_update(self,**kwargs):
  with (project/'updates.jsonl').open('a') as f:
   f.write(json.dumps(kwargs,default=lambda x:x.model_dump() if hasattr(x,'model_dump') else str(x))+'\n')
async def main():
 c=Comms(root)
 rid=c.initialize_private_initial_protocol();c.initialize_private_claim_protocol()
 package=Path('/var/tmp/agent-comms-pi-native-summary-20260928-ljjfj1ls/node_modules/@earendil-works/pi-coding-agent')
 os.environ['AGENT_COMMS_ROOT']=str(root)
 os.environ['AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID']=rid
 os.environ['AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE']=str(package)
 agent=CommsAgent(c,agent_bin='/home/ts/.local/share/agent-comms/runtime-summary-candidate-20260928/bin/pi-comms-native',agent_args=['--provider','openai-codex','--model','gpt-6-sol','--thinking','medium','--approve','--no-tools','--no-extensions','--no-skills','--no-prompt-templates'],runtime_enabled=True,auto_wake=False,private_nk_native_package=package,private_nk_wire_root_id=rid)
 agent.on_connect(Client())
 try:
  session=await agent.new_session(cwd=str(project),mcp_servers=[])
  report['session_id']=session.session_id
  prompts=[
   'This is a conversation retention acceptance test. Remember these exact project facts across future turns and summaries: lighthouse code ORCHID-7301; delivery window Thursday 14:30; rejected option cobalt; chosen material cedar. Keep these facts in future compacted summaries. No tools needed. Reply RECORDED and list the four facts.',
   'We are testing ordinary conversation continuity. Explain in about 250 words how you will distinguish recorded decisions from assumptions in later answers. Preserve our earlier project facts. Do not use tools.',
   'State the four project facts from the beginning exactly, without guessing. Reply RETENTION_ONE followed by them. No tools.'
  ]
  background='\n'.join(f'Archive record {n}: routine fixture observation, no change to project facts, no outstanding task, no action required.' for n in range(420))
  recent='\n'.join(f'Recent observation {n}: acknowledged fixture entry; no new requirement.' for n in range(55))
  prompts=[prompts[0]+'\nThe following archived background is disposable test context, summarize it briefly if needed:\n'+background,
   'Keep our four original project facts. Read this recent context and reply ACK only:\n'+recent,
   prompts[2],
   'Preserve the four project facts. More disposable archived background; reply ACK only:\n'+background,
   'Keep the original facts; acknowledge this current context with ACK only:\n'+recent,
   'State all four original project facts exactly from retained conversation. Reply RETENTION_TWO then list them. No tools.']
  for i,text in enumerate(prompts):
   started=time.time()
   response=await agent.prompt(session.session_id,[{'type':'text','text':text}])
   owner=c.registry.require(session.session_id)
   info=c.agent_info_of(session.session_id)
   report['turns'].append({'index':i,'response':response.model_dump(),'seconds':time.time()-started,'session_file':owner.session_file,'runtime_info':asdict(info) if info else None})
   if i==0:
    assert not agent._emitted_errors, str(agent._emitted_errors)
    from agent_comms.selected_pi_route import read_selected_compaction_decision
    persistent=agent._persistent_backends[session.session_id]
    decision=await read_selected_compaction_decision(persistent,session_file=owner.session_file,expected_launcher=agent._agent_bin,provider='openai-codex',model_id='gpt-6-sol',context_tokens=info.context_used,context_window=info.context_size)
    report['effective_settings']=asdict(decision)
    assert decision.reserve_tokens==263808 and decision.trigger, 'Owned project settings did not activate the intended acceptance threshold'
   receipt.write_text(json.dumps(report,indent=2,default=str)+'\n')
   print('TURN',i,report['turns'][-1],flush=True)
  entries=[json.loads(line) for line in Path(c.registry.require(session.session_id).session_file).read_text().splitlines()]
  report['compactions']=[{'id':e['id'],'firstKeptEntryId':e['firstKeptEntryId'],'summary':e['summary']} for e in entries if e.get('type')=='compaction']
  report['assistant_answers']=[''.join(p.get('text','') for p in e['message'].get('content',[]) if p.get('type')=='text') for e in entries if e.get('type')=='message' and e['message'].get('role')=='assistant']
  report['completed']=True
  assert len(report['compactions'])>=2, 'Repeated actual compaction is not yet proved'
  for marker in ('RETENTION_ONE','RETENTION_TWO'):
   answer=next(a for a in report['assistant_answers'] if marker in a)
   assert all(fact in answer for fact in ('ORCHID-7301','14:30','cobalt','cedar')), answer
 except BaseException as e:
  report['error']=repr(e);raise
 finally:
  await agent.shutdown()
  report['finished_at']=time.time();receipt.write_text(json.dumps(report,indent=2,default=str)+'\n')
asyncio.run(main())
