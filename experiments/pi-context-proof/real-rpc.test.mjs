// Opt-in real-provider integration check of the disposable copied Pi package.
// Uses a fresh /var/tmp HOME/session and disables all extensions/tools; never
// invokes or mutates the globally installed Pi. Requires provider credentials.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

const pkg = process.env.PI_PACKAGE_DIR;
if (!pkg?.startsWith('/var/tmp/') || !process.env.OPENROUTER_API_KEY) {
  throw new Error('Disposable PI_PACKAGE_DIR and OPENROUTER_API_KEY required');
}
const INPUT_ID = '712eee1314644da48675083528169a31';
function launch(root, sessionFile) {
  const args = [join(pkg,'dist/cli.js'), '--mode','rpc','--no-tools','--no-extensions',
    '--session-dir',join(root,'sessions'),'--provider','openrouter',
    '--model','z-ai/glm-5.3-flash'];
  if (sessionFile) args.push('--session', sessionFile);
  const child = spawn(process.execPath, args, {
    cwd: root, env: {...process.env, HOME:root, PI_OFFLINE:'1'},
    stdio: ['pipe','pipe','pipe'],
  });
  let buffer = '';
  let stderr = '';
  const events = [];
  let notify = () => {};
  child.stdout.on('data', chunk => {
    buffer += chunk.toString();
    while (buffer.includes('\n')) {
      const at = buffer.indexOf('\n');
      const line = buffer.slice(0,at);
      buffer = buffer.slice(at+1);
      if (!line) continue;
      try { events.push(JSON.parse(line)); notify(); }
      catch (error) { stderr += `invalid JSONL: ${String(error)}\n`; }
    }
  });
  child.stderr.on('data',chunk => {stderr += chunk.toString().slice(0,4096);});
  const send = cmd => child.stdin.write(JSON.stringify(cmd)+'\n');
  async function take(predicate, ms = 30000) {
    const old = events.findIndex(predicate);
    if (old >= 0) return events.splice(old,1)[0];
    return await new Promise((resolve,reject) => {
      const timer = setTimeout(()=>{ notify=()=>{}; reject(new Error(`Timed out: ${stderr.slice(0,300)}`)); },ms);
      notify = () => {
        const at=events.findIndex(predicate);
        if(at<0) return;
        clearTimeout(timer); notify=()=>{}; resolve(events.splice(at,1)[0]);
      };
      child.once('exit', code => {clearTimeout(timer);reject(new Error(`Pi exited ${code}: ${stderr.slice(0,300)}`));});
    });
  }
  async function stop() {
    child.stdin.end();
    child.kill('SIGTERM');
    await Promise.race([new Promise(resolve=> child.once('exit',resolve)),
      new Promise(resolve=> setTimeout(()=>{child.kill('SIGKILL');resolve();},3000))]);
  }
  return {send,take,stop,child};
}

test('real patched RPC model request commits input/session/context IDs; restart exact replay is inert',
  {timeout:80000}, async () => {
    const root=mkdtempSync(join(tmpdir(),'pi-native-rpc-'));
    let first, second;
    try {
      first=launch(root);
      first.send({type:'get_state',id:'state-1'});
      const state=await first.take(e=>e.type==='response' && e.id==='state-1',12000);
      assert.equal(state.success,true);
      assert.equal(state.data.nativeInputProofCapability,'pi-native-input-v1-live-only');
      const sessionFile=state.data.sessionFile;
      assert.equal(statSync(join(root,'sessions')).mode & 0o777,0o700);
      first.send({type:'prompt',id:'rpc-command-1',inputId:INPUT_ID,
        message:'Respond with exactly HELLO and no tools.'});
      assert.equal((await first.take(e=>e.type==='response'&&e.id==='rpc-command-1')).success,true);
      const input=await first.take(e=>e.type==='input_committed' && e.inputId===INPUT_ID);
      const context=await first.take(e=>e.type==='context_committed' && e.inputId===INPUT_ID);
      assert.equal(input.sessionEntryId,context.sessionEntryId);
      assert.match(context.llmContextDigest,/^[a-f0-9]{64}$/);
      await first.take(e=>e.type==='agent_settled',60000);
      const entries=readFileSync(sessionFile,'utf8').trim().split('\n').map(JSON.parse);
      assert(entries.some(e=>e.type==='message' && e.id===input.sessionEntryId && e.message.inputId===INPUT_ID));
      assert.equal(statSync(sessionFile).mode & 0o777,0o600);
      assert.equal(statSync(sessionFile+'.input-proof').mode & 0o777,0o600);
      const proof=readFileSync(sessionFile+'.input-proof','utf8').trim().split('\n').map(JSON.parse);
      assert(proof.some(e=>e.inputId===INPUT_ID && e.sessionEntryId===input.sessionEntryId));
      await first.stop(); first=undefined;
      second=launch(root,sessionFile);
      second.send({type:'get_state',id:'state-2'});
      assert.equal((await second.take(e=>e.type==='response'&&e.id==='state-2',12000)).success,true);
      second.send({type:'prompt',id:'rpc-command-2',inputId:INPUT_ID,
        message:'Respond with exactly HELLO and no tools.'});
      assert.equal((await second.take(e=>e.type==='response'&&e.id==='rpc-command-2')).success,true);
      second.send({type:'get_entries',id:'entries'});
      const after=await second.take(e=>e.type==='response'&&e.id==='entries');
      assert.equal(after.success,true);
      assert.equal(after.data.entries.filter(e=>e.type==='message'&&e.message.inputId===INPUT_ID).length,1);
      second.send({type:'prompt',id:'conflict',inputId:INPUT_ID,message:'different content'});
      const conflict=await second.take(e=>e.type==='response'&&e.id==='conflict');
      assert.equal(conflict.success,false);
      assert.match(conflict.error,/Conflicting replay/);
      assert(existsSync(sessionFile+'.input-proof'));
    } finally {
      if(first) await first.stop();
      if(second) await second.stop();
      rmSync(root,{recursive:true,force:true});
    }
});
