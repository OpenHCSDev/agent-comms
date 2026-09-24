import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
const pkg=process.env.PI_PACKAGE_DIR;
if(!pkg?.startsWith('/var/tmp/')) throw Error('Disposable copied Pi only');
const pi=await import(pathToFileURL(join(pkg,'dist/index.js')).href);
const ID='f'.repeat(32);

test('SIGKILL before first assistant leaves fsynced exact input and context on disk; restart cannot duplicate',
  {timeout:20000}, async () => {
    const root=mkdtempSync('/var/tmp/pi-native-crash-');
    try {
      const child=spawnSync(process.execPath,[new URL('./crash-helper.mjs',import.meta.url).pathname],{
        cwd:root,env:{...process.env,PI_PACKAGE_DIR:pkg,PI_CRASH_ROOT:root,PI_OFFLINE:'1'},
        timeout:12000,encoding:'utf8',
      });
      assert.equal(child.signal,'SIGKILL',`unexpected child exit: ${child.status} ${child.stderr?.slice(0,250)}`);
      const names=readdirSync(join(root,'sessions')).filter(name=>name.endsWith('.jsonl'));
      assert.equal(names.length,1);
      const file=join(root,'sessions',names[0]);
      const entries=readFileSync(file,'utf8').trim().split('\n').map(JSON.parse);
      const user=entries.find(e=>e.type==='message'&&e.message.inputId===ID);
      assert(user);
      assert.equal(entries.filter(e=>e.type==='message'&&e.message.role==='assistant').length,0);
      const rows=readFileSync(file+'.input-proof','utf8').trim().split('\n').map(JSON.parse);
      assert.equal(rows.length,1);
      assert.equal(rows[0].sessionEntryId,user.id);
      const sm=pi.SessionManager.open(file,join(root,'sessions'));
      assert.equal(sm.getTrackedInput(ID).id,user.id);
      const runtime=await pi.ModelRuntime.create({authPath:join(root,'auth.json'),
        modelsPath:join(root,'models.json'),modelsStorePath:join(root,'models-store.json')});
      await runtime.setRuntimeApiKey('openai','fake-local-only');
      const cwd=join(root,'work'),agentDir=join(root,'agent');
      const settings=pi.SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false}});
      const loader=new pi.DefaultResourceLoader({cwd,agentDir,settingsManager:settings});
      await loader.reload();
      const {session}=await pi.createAgentSession({cwd,agentDir,modelRuntime:runtime,
        model:runtime.getModel('openai','gpt-4.1-mini'),sessionManager:sm,
        settingsManager:settings,resourceLoader:loader,noTools:'all'});
      try {
        let sent=0;
        session.agent.streamFunction=async()=>{sent++;throw Error('exact replay must not call model');};
        await session.prompt('crash after context assembly',{inputId:ID});
        assert.equal(sent,0);
        assert.equal(sm.getEntries().filter(e=>e.type==='message'&&e.message.inputId===ID).length,1);
      } finally {session.dispose();}
    } finally {rmSync(root,{recursive:true,force:true});}
});
