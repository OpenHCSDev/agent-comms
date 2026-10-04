"""Authored inspector protocol controls; no SDK, provider or native package.

These detect a retained debugger after runtime completion, stranded protocol
requests after disconnect, and leaked controller custody after caller failure.
"""
import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from summary_prefix_configured_installed_journey import observe_native_requests
from agent_comms.native_pi import NativePiRpcLaunch


@pytest.mark.parametrize('mode', ('complete', 'setup-error', 'pending-close',
                                  'paused-close', 'shutdown', 'malformed'))
def test_original_observer_connection_completion(tmp_path, mode):
    # Minimal authored anchor files, not a substitute SDK or copied artifact.
    anchors = {
        'dist/modes/rpc/rpc-mode.js': 'const selectedStream = (model, context, options) => {\n',
        'node_modules/@earendil-works/pi-ai/dist/api/openai-codex-responses.js':
            'if (model.baseUrl !== DEFAULT_CODEX_BASE_URL)\n',
        'node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js':
            '    await config.onContextReady?.(llmContext, request.requestId);\n',
        'dist/core/turn-context.js': '    observation(requestId) {\n',
        'dist/core/agent-session.js': '    _emit(event) {\n',
    }
    for relative, text in anchors.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    contexts = tmp_path / 'contexts'
    contexts.mkdir()
    output = tmp_path / 'observation.jsonl'
    observer = Path(__file__).with_name('summary_prefix_native_observer.mjs')
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const [module,packageRoot,output,contexts,mode]=process.argv.slice(1);
const commands=[];
let socket,contextPoint;
globalThis.fetch=async()=>({json:async()=>[{webSocketDebuggerUrl:'ws://authored'}]});
class AuthoredSocket extends EventTarget {
    static OPEN=1;
    readyState=1;
    constructor() {super();socket=this;queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
    close() {
        if (this.readyState===3) return;
        this.readyState=3;
        queueMicrotask(()=>this.dispatchEvent(new Event('close')));
    }
    receive(message) {this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(message)}));}
    send(raw) {
        const command=JSON.parse(raw);commands.push(command);
        queueMicrotask(()=>{
            let result={};
            if (command.method==='NodeRuntime.notifyWhenWaitingForDisconnect') {
                assert.equal(command.params.enabled,true);
                if (mode==='pending-close') {this.close();return;}
                if (mode==='setup-error') {
                    this.receive({id:command.id,error:{message:'Authored registration refusal'}});return;
                }
            }
            if (command.method==='Debugger.setBreakpointByUrl') {
                result={breakpointId:command.params.url};
                if (command.params.url.endsWith('/turn-context.js')) {
                    contextPoint=result.breakpointId;
                    assert.equal(command.params.condition,'requestId !== undefined');
                }
            }
            if (command.method==='Debugger.evaluateOnCallFrame') {
                if (mode==='paused-close') {this.close();return;}
                // Execute the original reader expression on authored values:
                // provider JSON omits callbacks, CDP must not expand them to {}.
                const tool={name:'authored',execute:()=>{},prepareArguments:()=>{}};
                const value={tools:[tool]};
                const owner={full:()=>({segments:[{provenance:[{kind:'native',
                    request_generation:1,context_digest:'authored'}],value}]}),segments:[{value}]};
                result={result:{value:Function('return '+command.params.expression).call(owner)}};
                assert.deepEqual(result.result.value.context.segments[0].value,JSON.parse(result.result.value.serialized[0]));
            }
            this.receive({id:command.id,result});
            if (command.method==='Runtime.runIfWaitingForDebugger') {
                if (mode==='shutdown') {process.emit('SIGTERM');return;}
                if (mode==='malformed') {
                    this.dispatchEvent(new MessageEvent('message',{data:'{not JSON'}));return;
                }
                this.receive({method:'Debugger.paused',params:{hitBreakpoints:[contextPoint],callFrames:[{callFrameId:'original'}]}});
            }
            if (command.method==='Debugger.resume')
                this.receive({method:'NodeRuntime.waitingForDisconnect'});
        });
    }
}
globalThis.WebSocket=AuthoredSocket;
process.argv=['node',module,'0',packageRoot,output,contexts];
let failure;
try {await import(module);} catch(error) {failure=error.message;}
assert.equal(socket.readyState,3);
assert.equal(process.listenerCount('SIGTERM'),0);
const rows=readFileSync(output,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
if (mode==='complete') {
    assert.equal(failure,undefined);
    assert.equal(process.exitCode,undefined);
    assert.deepEqual(rows.map(row=>row.stage),['observer-ready','source-context','observer-runtime-complete']);
} else if (mode==='shutdown') {
    assert.equal(failure,undefined);
    assert.deepEqual(rows.map(row=>row.stage),['observer-ready']);
} else {
    assert.ok(failure || rows.some(row=>row.observerFailed));
    assert.ok(!rows.some(row=>row.stage==='observer-runtime-complete'));
}
process.exitCode=0; // Authored refusals were asserted, not successful observations.
console.log(JSON.stringify({mode,closed:true,commands:commands.map(row=>row.method)}));
'''
    # Setup-error/pending-close can fail before writing the first observation.
    output.touch()
    result = subprocess.run(['node', '--input-type=module', '-e', script,
        observer.as_uri(), str(tmp_path), str(output), str(contexts), mode],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout)['closed']
    assert result.stderr == ''


def test_original_controller_releases_all_owned_children_on_caller_failure(tmp_path):
    children = [MagicMock(), MagicMock()]
    original = NativePiRpcLaunch.bootstrap
    bootstrap = classmethod(lambda cls, cli, args, cwd, env, config: (('node','native'), env))
    with patch.object(NativePiRpcLaunch, 'bootstrap', bootstrap), patch(
            'summary_prefix_configured_installed_journey.ParentedProcess.launch',
            side_effect=children) as launch:
        with pytest.raises(ValueError, match='Original caller failure'):
            with observe_native_requests(tmp_path, tmp_path/'observations') as observe:
                environment={}
                observe(environment)
                for _ in children:
                    argv, _ = NativePiRpcLaunch.bootstrap((), (), tmp_path, environment, None)
                    assert argv[1].startswith('--inspect-brk=127.0.0.1:')
                raise ValueError('Original caller failure')
        for child, call in zip(children, launch.call_args_list):
            child.__enter__.assert_called_once()
            child.__exit__.assert_called_once()
            assert call.kwargs['output'].closed
    assert NativePiRpcLaunch.bootstrap == original
