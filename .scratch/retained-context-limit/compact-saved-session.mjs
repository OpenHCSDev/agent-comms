import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

const pkg=process.env.PI_PACKAGE;
const sessionFile=process.env.SESSION_FILE;
const cwd=process.env.SESSION_CWD;
const agentDir=process.env.PI_CODING_AGENT_DIR;
if(!pkg||!sessionFile||!cwd||!agentDir) throw new Error('explicit package, session, cwd, and agent directory required');
const pi=await import(pathToFileURL(join(pkg,'dist/index.js')));
const runtime=await pi.ModelRuntime.create({
  authPath:join(agentDir,'auth.json'),modelsPath:join(agentDir,'models.json'),
  modelsStorePath:join(agentDir,'models-store.json'),
});
const model=runtime.getModel('openai-codex','gpt-5.6-sol');
if(!model) throw new Error('configured openai-codex/gpt-5.6-sol model unavailable');
const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
const loader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager:settings,
  noExtensions:true,noSkills:true,noPromptTemplates:true,noContextFiles:true});
await loader.reload();
const manager=pi.SessionManager.open(sessionFile);
let session;
try {
  ({session}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,model,
    sessionManager:manager,settingsManager:settings,resourceLoader:loader,noTools:'all'}));
  const events=[];
  session.subscribe(event=>{if(event.type==='compaction_start'||event.type==='compaction_end') events.push(event.type);});
  const result=await session.compact('Preserve the current active task, in-progress work, decisions, exact paths, blockers, and next steps. Do not treat the context-window error as a user request and do not replay any interrupted input.');
  console.log(JSON.stringify({sessionFile,tokensBefore:result.tokensBefore,
    estimatedTokensAfter:result.estimatedTokensAfter,firstKeptEntryId:result.firstKeptEntryId,
    summaryStart:result.summary.slice(0,1200),events},null,2));
} finally {
  session?.dispose();
  manager.entryStore.close();
}
