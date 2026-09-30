import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync,writeFileSync,existsSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [repo,pkg,root] = process.argv.slice(2);
const manifest=JSON.parse(readFileSync(join(repo,'stack/native-import-manifest.json')));
const declared=manifest.extensionEntries.find(e=>e.entry.endsWith('global-agent-comms/index.mjs')).sources[0];
const source=readFileSync(join(repo,declared.snapshot));
assert.equal(source.length,declared.bytes);
assert.equal(createHash('sha256').update(source).digest('hex'),declared.sha256);
const project=join(root,'project'),agentDir=join(root,'agent');
const {DefaultResourceLoader,createAgentSession,SessionManager}=await import(pathToFileURL(join(pkg,'dist/index.js')));
const loader=new DefaultResourceLoader({cwd:project,agentDir,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true});
await loader.reload();
const loaded=loader.getExtensions();assert.deepEqual(loaded.errors,[]);
const extension=loaded.extensions.find(e=>e.path===join(pkg,'agent-comms-extensions/global-agent-comms/index.mjs'));
const handlers=[...extension.handlers.keys()];
assert.deepEqual(handlers,['session_start']);
assert(extension.tools.has('comms_send'));
const saved=SessionManager.create(project,join(root,'sessions'));
const savedFile=saved.getSessionFile();
// Persist only the header produced by the actual native owner; no invented history.
writeFileSync(savedFile,JSON.stringify(saved.getHeader())+'\n',{mode:0o600,flag:'wx'});
saved.entryStore.close();
const opened=SessionManager.open(savedFile,join(root,'sessions'),project);
const {session}=await createAgentSession({cwd:project,agentDir,resourceLoader:loader,sessionManager:opened});
const errors=[];
const timings=[];
try {
 await session.bindExtensions({onError:e=>errors.push({event:e.event,error:e.error})});
 assert.deepEqual(errors,[]);
 for (const [name,args] of [['read',{path:'fixture.txt'}],['edit',{path:'fixture.txt',edits:[{oldText:'before',newText:'after'}]}],['read',{path:'fixture.txt'}]]) {
  const tool=session.agent.state.tools.find(t=>t.name===name);assert(tool);
  const toolCall={type:'toolCall',id:`fixture-${timings.length}`,name,arguments:args};
  const start=performance.now();
  const before=await session.agent.beforeToolCall({toolCall,args});assert(!before?.block);
  const hooksDone=performance.now();
  const prepared=tool.prepareArguments?.(args)??args;
  const result=await tool.execute(toolCall.id,prepared);
  const toolsDone=performance.now();
  const after=await session.agent.afterToolCall({toolCall,args,result,isError:false});
  timings.push({name,hook_before_ms:hooksDone-start,tool_ms:toolsDone-hooksDone,hook_after_ms:performance.now()-toolsDone,text:(after?.content??result.content).filter(c=>c.type==='text').map(c=>c.text).join('\n')});
 }
 assert(timings[0].text.includes('before'));
 assert(timings[2].text.includes('after'));
 assert.equal(readFileSync(join(project,'fixture.txt'),'utf8'),'after\n');
 assert(!existsSync(join(root,'wire/activity.jsonl')),'Managed SDK tool hooks emitted duplicate activity');
 const receipt={ok:true,source_snapshot_sha256:declared.sha256,compiled_bytes:readFileSync(join(pkg,'agent-comms-extensions/global-agent-comms/index.mjs')).length,discovered_extensions:loaded.extensions.map(e=>e.path),inline_factories:0,handlers,tool_count:extension.tools.size,saved_native_session:savedFile,saved_state:'Native-generated header only, no retained message history claim',tool_timings:timings.map(({text,...timing})=>timing),network:'kernel-denied',provider_prompts:0,native_inputs:0,limits:'Actual packaged DefaultResourceLoader discovery through supported private settings extension paths and production manifest, native hooks and builtin read/edit/read. No extensionFactories, inline compilation, provider call or ACP turn. Default global source remains old and paired deployment is parent-owned.'};
 writeFileSync(join(root,'receipt.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt,null,2));
} finally {session.dispose();}
