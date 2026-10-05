"""Authored inspector protocol controls; no SDK, provider or native package.

These detect a retained debugger after runtime completion, stranded protocol
requests after disconnect, and leaked controller custody after caller failure.
"""
import ast
import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from summary_prefix_configured_installed_journey import observe_native_requests
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.child_process import join_retirement
from agent_comms.input_disposition import InputDispositions
from agent_comms.field_codec import FieldCodec


@pytest.mark.parametrize('failed_arm', (None, 'baseline'))
def test_pair_acquires_separate_arm_trajectories_from_one_untouched_seed(tmp_path, failed_arm):
    # Execute the runner body with authored resource boundaries only. Detect
    # source reuse, order/stimulus drift and erasure of a completed first arm.
    # No native files, model calls, original records or installed imports.
    from compaction_retention_fixture import RecordedNativeProbes, RecallScenario, coding_scenario

    source = Path(__file__).with_name('three_cut_retention_configured_journey.py')
    declarations = [node for node in ast.parse(source.read_text()).body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ('record', 'run')]
    assert len(declarations) == 2
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *declarations], type_ignores=[])
    plan = {'trajectories': ({'sample': 1, 'condition_order': ('candidate', 'baseline')},),
            'rounds': ({'source_text': 'Frozen first stimulus'}, {'source_text': 'Frozen correction'})}
    design = SimpleNamespace(model='original-model', candidate='candidate', baseline='baseline',
        oracle='authored-only', construction_plan=lambda seed, trajectory: plan)
    original = SimpleNamespace(model=design.model, require_saved_session=lambda: 'authored-parent')
    captured = SimpleNamespace(require_current=lambda: original, retained='authored-config')
    calls, events, runs = [], [], {}
    refused = RuntimeError('Authored second arm refusal')
    scenario = coding_scenario()

    @asynccontextmanager
    async def acquire(stage, package, source_file, receiver, receipt, **kwargs):
        stage.mkdir()
        events.append('seed-open')
        try:
            yield None, None, SimpleNamespace(session_file='unchanged-seed')
        finally:
            events.append('seed-joined')

    async def arm(stage, package, source_file, **kwargs):
        calls.append((stage, source_file, kwargs))
        stage.mkdir()
        if kwargs['condition'] == failed_arm:
            raise refused
        result = RecordedNativeProbes({})
        runs[kwargs['condition']] = result
        return result

    from agent_comms.turn_context import FileProvenance
    namespace = dict(Path=Path, json=json, FieldCodec=FieldCodec, FileProvenance=FileProvenance,
        digest=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
        CurrentTypedCapture=lambda *args: SimpleNamespace(read=lambda name: captured),
        RecallScenario=RecallScenario,
        RecordedNativeCheckpoint=SimpleNamespace(read_record=lambda *args: scenario),
        configured_saved_agent=acquire, run_arm=arm)
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    output = tmp_path/'new-pair'
    operation = namespace['run'](output, tmp_path/'unread-package', tmp_path/'unread-python',
        design=design, sampling_seed=7, trajectory=1, core_source='original-declaration')
    if failed_arm:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value is refused
    else:
        with patch.object(type(scenario), 'compare_native', return_value={'scope': 'authored orchestration'}):
            asyncio.run(operation)
    assert events == ['seed-open', 'seed-joined']
    assert [call[0] for call in calls] == [output/'arm-1', output/'arm-2']
    assert [call[1] for call in calls] == [Path('unchanged-seed')]*2
    assert [call[2]['condition'] for call in calls] == ['candidate', 'baseline']
    assert all(call[2]['plan'] is plan for call in calls)
    assert all(call[2]['scenario'] is scenario for call in calls)
    assert len({id(call[2]['receiver']) for call in calls}) == 1
    receipt = json.loads((output/'receipt.json').read_text())
    assert receipt['completed_arms'] == (['candidate'] if failed_arm else ['candidate', 'baseline'])
    assert receipt['complete'] is (failed_arm is None)
    assert 'workflow_elapsed_seconds' not in receipt
    assert (output/'arm-1-run.private.json').exists()
    assert (output/'paired-measurements.private.json').exists() is (failed_arm is None)


@pytest.mark.parametrize('cleanup_failure', (False, True))
def test_arm_clock_completes_only_after_agent_and_inspector_join(tmp_path, cleanup_failure):
    # Zero authored rounds isolate clock/resource ordering. This does not
    # qualify any source/cut/probe, SDK completion, configured path or study.
    from compaction_retention_fixture import RecordedNativeProbes
    from agent_comms.native_session_reopen import NativeSessionIdentity

    source = Path(__file__).with_name('three_cut_retention_configured_journey.py')
    declarations = [node for node in ast.parse(source.read_text()).body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ('record', 'run_arm')]
    assert len(declarations) == 2
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *declarations], type_ignores=[])
    events = []
    receipt_seen = []
    refused = RuntimeError('Authored cleanup refusal')
    agent = MagicMock()
    agent._comms.root = tmp_path/'unread-wire'
    owner = SimpleNamespace(name='authored-owner', worktree='authored-worktree')
    creation = SimpleNamespace(session_id='authored-session', session_file=str(tmp_path/'unread-saved.jsonl'))

    @contextmanager
    def observe(*args, **kwargs):
        events.append('inspector-open')
        try:
            yield 'authored-launch'
        finally:
            events.append('inspector-joined')

    @asynccontextmanager
    async def acquire(stage, package, source_file, receiver, receipt, **kwargs):
        stage.mkdir()
        receipt_seen.append(receipt)
        events.append('agent-open')
        try:
            yield agent, owner, creation
        finally:
            events.append('agent-joined')
            assert receipt['complete'] is False
            assert 'workflow_elapsed_seconds' not in receipt
            if cleanup_failure:
                raise refused

    def clock():
        if events:
            assert events == ['inspector-open', 'agent-open', 'agent-joined', 'inspector-joined']
            return 19.
        return 5.

    from agent_comms.turn_context import FileProvenance
    namespace = dict(Path=Path, json=json, FieldCodec=FieldCodec, FileProvenance=FileProvenance,
        digest=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(), monotonic=clock,
        observe_native_requests=observe, configured_saved_agent=acquire,
        NativeSessionIdentity=NativeSessionIdentity, build_agent_router=MagicMock(),
        CompactionJournal=MagicMock(), InputDispositions=MagicMock(filename='inputs'),
        RecordedNativeProbes=RecordedNativeProbes)
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    output = tmp_path/'new-arm'
    operation = namespace['run_arm'](output, tmp_path/'unread-package', Path('unchanged-seed'),
        condition='candidate', plan={'rounds': (), 'trajectories': ({'sample': 1},)},
        scenario=SimpleNamespace(rounds=(SimpleNamespace(evaluation_instructions=lambda: 'authored'),)),
        capture_source='authored-capture', receiver='authored-receiver', core_source='original-declaration')
    if cleanup_failure:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value is refused
        assert receipt_seen[0]['complete'] is False
        assert 'workflow_elapsed_seconds' not in receipt_seen[0]
        assert not (output/'recorded-run.private.json').exists()
    else:
        run = asyncio.run(operation)
        observed = run.workflow_timing()
        assert observed['evaluated'] and observed['exclusive_input_set']
        assert observed['seconds'] == 14.
        assert run.checkpoints == {}
    assert events == ['inspector-open', 'agent-open', 'agent-joined', 'inspector-joined']


@pytest.mark.parametrize('mode', ('complete', 'publication', 'setup-error', 'pending-close',
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
let socket,contextPoint,publicationPoint;
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
                if (command.params.url.endsWith('/agent-session.js'))
                    publicationPoint=result.breakpointId;
            }
            if (command.method==='Debugger.evaluateOnCallFrame') {
                if (mode==='paused-close') {this.close();return;}
                // Execute the original reader expression on authored values:
                // provider JSON omits callbacks, CDP must not expand them to {}.
                const tool={name:'authored',execute:()=>{},prepareArguments:()=>{}};
                const value={tools:[tool]};
                if (mode==='publication') {
                    const event={context:{segments:[{provenance:[{kind:'native',
                        request_generation:1,context_digest:'authored'}]}],values:[value]}};
                    const projected=Function('event','return '+command.params.expression)(event);
                    assert.deepEqual(projected,JSON.parse(JSON.stringify(event.context)));
                    assert.deepEqual(projected.values[0].tools,[{name:'authored'}]);
                    result={result:{value:projected}};
                } else {
                const owner={full:()=>({segments:[{provenance:[{kind:'native',
                    request_generation:1,context_digest:'authored'}],value}]}),segments:[{value}]};
                result={result:{value:Function('return '+command.params.expression).call(owner)}};
                assert.deepEqual(result.result.value.context.segments[0].value,JSON.parse(result.result.value.serialized[0]));
                }
            }
            this.receive({id:command.id,result});
            if (command.method==='Runtime.runIfWaitingForDebugger') {
                if (mode==='shutdown') {process.emit('SIGTERM');return;}
                if (mode==='malformed') {
                    this.dispatchEvent(new MessageEvent('message',{data:'{not JSON'}));return;
                }
                this.receive({method:'Debugger.paused',params:{hitBreakpoints:[mode==='publication' ? publicationPoint : contextPoint],callFrames:[{callFrameId:'original'}]}});
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
if (mode==='complete' || mode==='publication') {
    assert.equal(failure,undefined);
    assert.equal(process.exitCode,undefined);
    assert.deepEqual(rows.map(row=>row.stage),['observer-ready',
        mode==='publication' ? 'source-observation' : 'source-context','observer-runtime-complete']);
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


def test_nested_original_observers_only_acquire_their_declared_launch(tmp_path):
    # Parent restoration uses its original launch; an arm must not attach both
    # inspectors to that one child or leave its override on the parent.
    children = [MagicMock(), MagicMock(), MagicMock()]
    bootstrap = classmethod(lambda cls, cli, args, cwd, env, config: (('node', 'native'), env))
    with patch.object(NativePiRpcLaunch, 'bootstrap', bootstrap), patch(
            'summary_prefix_configured_installed_journey.ParentedProcess.launch',
            side_effect=children) as launch:
        with observe_native_requests(tmp_path, tmp_path/'parent') as parent:
            parent_environment = {}
            parent(parent_environment)
            NativePiRpcLaunch.bootstrap((), (), tmp_path, parent_environment, None)
            with observe_native_requests(tmp_path, tmp_path/'arm') as arm:
                arm_environment = dict(parent_environment)
                arm(arm_environment)
                argv, _ = NativePiRpcLaunch.bootstrap((), (), tmp_path, arm_environment, None)
                assert sum(arg.startswith('--inspect-brk=') for arg in argv) == 1
                assert launch.call_count == 2
            NativePiRpcLaunch.bootstrap((), (), tmp_path, parent_environment, None)
        assert launch.call_count == 3
        assert [call.args[0][4] for call in launch.call_args_list] == [
            str(tmp_path/'parent'), str(tmp_path/'arm'), str(tmp_path/'parent')]
        for child in children:
            child.__exit__.assert_called_once()


@pytest.mark.parametrize('failure', ('prepare', 'prompt', 'assertion', 'cancel'))
def test_original_application_joins_retirement_before_observer_and_environment_exit(tmp_path, failure):
    # Execute the unchanged original function body with controlled boundary
    # calls. This checks failure/cancellation ordering, not module loading,
    # native custody, an ACP transaction or an installed input.
    source = Path(__file__).with_name('three_cut_retention_configured_journey.py')
    declarations = [node for node in ast.parse(source.read_text()).body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name in ('condition_application', 'apply_condition_input')]
    assert len(declarations) == 2
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *declarations], type_ignores=[])
    events, cancellations = [], []
    original_failure = RuntimeError('Authored application boundary refusal')
    observation = tmp_path/'condition-observation.jsonl'
    before_environment = dict(os.environ)

    @contextmanager
    def observe(*args, **kwargs):
        events.append('observer-open')
        try:
            yield lambda environment: environment.update(AC_PREFIX_OBSERVATION=str(observation))
        finally:
            assert os.environ == before_environment
            events.append('observer-close')

    async def prepare(*args):
        events.append('prepare')
        if failure == 'prepare':
            raise original_failure

    async def prompt(*args):
        events.append('prompt')
        if failure == 'cancel':
            asyncio.current_task().cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise
        if failure == 'prompt':
            raise original_failure
        return SimpleNamespace(stop_reason='authored-unfinished')

    async def retire(session):
        assert session == 'authored-owner'
        assert os.environ['AC_PREFIX_OBSERVATION'] == str(observation)
        events.append('retire-start')
        await asyncio.sleep(0)
        events.append('retire-joined')

    async def retire_selected():
        # The source control supplies this acquired capability. Production
        # SessionLifecycle/NativeCustody owns its actual binding and join.
        await join_retirement(asyncio.create_task(retire('authored-owner')))

    namespace = {'Path': Path, 'os': os, 'patch': patch, 'asyncio': asyncio,
        'join_retirement': join_retirement, 'InputDispositions': InputDispositions,
        'observe_native_requests': observe, 'record': lambda *args: None,
        'build_agent_router': lambda agent: prompt}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    agent = SimpleNamespace(_comms=SimpleNamespace(root=tmp_path),
        turns=SimpleNamespace(prepare_selected_session=prepare))
    checkpoint = SimpleNamespace(fork_condition_source=lambda *args: {})
    owner = SimpleNamespace(name='authored-owner')
    fork = SimpleNamespace(session_file=str(tmp_path/'authored-child'))
    error_type = (asyncio.CancelledError if failure == 'cancel'
                  else AssertionError if failure == 'assertion' else RuntimeError)
    with pytest.raises(error_type) as raised:
        asyncio.run(namespace['condition_application'](tmp_path, tmp_path, agent, owner,
            fork, 'authored', checkpoint, prompt_text='Authored control', receipt={},
            retire_selected=retire_selected))
    if failure in ('prepare', 'prompt'):
        assert raised.value is original_failure
    elif failure == 'cancel':
        assert raised.value is cancellations[0]
    expected = ['observer-open', 'prepare']
    if failure != 'prepare':
        expected.append('prompt')
    assert events == [*expected, 'retire-start', 'retire-joined', 'observer-close']
    assert os.environ == before_environment
