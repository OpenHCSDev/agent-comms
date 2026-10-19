"""Compiled native recovery owns closure; Core preserves the original failure."""
from agent_comms.native_package import pinned_native_package

import asyncio
import json

import pytest

import agent_comms.backend as backend
import agent_comms.native_pi as native_pi
from agent_comms.acp_extension import RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import NativeMessageFailureSource, NativeTurnError
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import ProviderTransportFailureDiagnostic
from delivery_owner_fixture import canonical_agent
from test_native_refusal_recovery import _settled_records, _stub_with

FINAL = "Opaque provider transport failure"
DIAGNOSTIC = {
    "type": "provider_transport_failure",
    "error": {"name": "OpaqueTransportFailure", "message": FINAL, "code": "EOPAQUE"},
    "details": {"configuredTransport": "http", "phase": "before_message_stream_start",
                "eventsEmitted": False, "requestBytes": 123},
}


async def native_records():
    package = pinned_native_package()
    program = f"""
import assert from 'node:assert/strict';
import {{ NativeTurnRun }} from {json.dumps((package / 'dist/core/turn-recovery.js').as_uri())};
import {{ isRetryableAssistantError }} from {json.dumps((package / 'node_modules/@earendil-works/pi-ai/dist/utils/retry.js').as_uri())};
const terminal = {{role:'assistant', content:[], stopReason:'error',
    errorMessage:{json.dumps(FINAL)}, diagnostics:[{json.dumps(DIAGNOSTIC)}]}};
assert.equal(isRetryableAssistantError(terminal), false);
const original = JSON.stringify(terminal);
const claims = new Map([['isolated-input', {{digest:'isolated'}}]]);
function prepare(events) {{
    const agent = {{state:{{messages:[]}}}};
    const run = new NativeTurnRun(agent, event => events.push(event), isRetryableAssistantError);
    run.include([{{role:'user',inputId:'isolated-input'}}],claims);
    return {{agent,run}};
}}
const refused={{role:'assistant',content:[],stopReason:'error',errorMessage:'503 overloaded'}};
const codexRefusal={{role:'assistant',content:[],stopReason:'error',errorMessage:
    'Codex error: An error occurred while processing your request. You can retry your request, or contact us through our help center at help.openai.com if the error persists.'}};
function finished(owner, events, message) {{
    owner.agent.state.messages.push(message);
    const start={{type:'message_start',message:{{role:'assistant',content:[]}}}};
    owner.run.observe(start,claims);
    events.push(start);
    owner.run.observe({{type:'message_end',message}},claims);
    events.push({{type:'message_end',message}});
}}
async function differentTerminal() {{
    const events=[]; const owner=prepare(events);
    finished(owner,events,refused);
    assert.equal(await owner.run.recover(refused),true);
    finished(owner,events,terminal);
    assert.equal(await owner.run.recover(terminal),false);
    owner.run.close();owner.run.close();owner.run.cancel();
    assert.equal(JSON.stringify(terminal),original);
    const ends=events.filter(e=>e.type==='native_retry_end');
    assert.deepEqual(ends,[{{type:'native_retry_end',success:false,attempt:1,finalError:terminal.errorMessage}}]);
    assert(events.indexOf(ends[0]) < events.findIndex(e=>e.type==='message_end' && e.message===terminal));
    return events;
}}
async function answered() {{
    const events=[]; const owner=prepare(events);
    finished(owner,events,refused);
    assert.equal(await owner.run.recover(refused),true);
    finished(owner,events,{{role:'assistant',content:[{{type:'text',text:'recovered'}}],stopReason:'stop'}});
    owner.run.close();
    assert.deepEqual(events.filter(e=>e.type==='native_retry_end'),[
        {{type:'native_retry_end',success:true,attempt:1}}]);
}}
async function exhausted() {{
    const events=[]; const owner=prepare(events);
    for (let attempt=1; attempt<=4; attempt++) {{
        const message={{...refused}};
        finished(owner,events,message);
        assert.equal(await owner.run.recover(message),attempt<4);
    }}
    owner.run.close();
    assert.deepEqual(events.filter(e=>e.type==='native_retry_end'),[
        {{type:'native_retry_end',success:false,attempt:3,finalError:refused.errorMessage}}]);
}}
async function toolThenCodexRefusals() {{
    const events=[]; const owner=prepare(events);
    const tool={{role:'assistant',content:[{{type:'toolCall',id:'call-1',name:'read',arguments:{{}}}}],
        stopReason:'toolUse'}};
    finished(owner,events,tool);
    owner.run.observe({{type:'tool_execution_start'}},claims);
    for (let attempt=1; attempt<=3; attempt++) {{
        const message={{...codexRefusal}};
        finished(owner,events,message);
        assert.equal(await owner.run.recover(message),true);
    }}
    const answer={{role:'assistant',content:[{{type:'text',text:'recovered after tools'}}],stopReason:'stop'}};
    finished(owner,events,answer);
    owner.run.close();
    assert.deepEqual(events.filter(e=>e.type==='native_retry_start').map(e=>e.attempt),[1,2,3]);
    assert.deepEqual(events.filter(e=>e.type==='native_retry_end'),[
        {{type:'native_retry_end',success:true,attempt:3}}]);
    return {{tries:4,retries:3,answer:answer.content[0].text}};
}}
async function thinkingOnlyThenAnswer() {{
    const events=[]; const owner=prepare(events);
    const disconnected={{role:'assistant',content:[{{type:'thinking',thinking:'private only'}}],
        stopReason:'error',errorMessage:'WebSocket closed 1000'}};
    assert.equal(isRetryableAssistantError(disconnected),true);
    owner.agent.state.messages.push(disconnected);
    owner.run.observe({{type:'message_start',message:{{role:'assistant',content:[]}}}},claims);
    owner.run.observe({{type:'message_update',assistantMessageEvent:{{
        type:'thinking_delta',delta:'private only'}}}},claims);
    owner.run.observe({{type:'message_end',message:disconnected}},claims);
    assert.equal(await owner.run.recover(disconnected),true);
    const answer={{role:'assistant',content:[{{type:'text',text:'visible answer'}}],stopReason:'stop'}};
    finished(owner,events,answer);
    owner.run.close();
    assert.deepEqual(events.filter(e=>e.type==='native_retry_start'),[{{
        type:'native_retry_start',attempt:1,maxAttempts:3,delayMs:1000,
        errorMessage:'WebSocket closed 1000'}}]);
    assert.deepEqual(events.filter(e=>e.type==='native_retry_end'),[
        {{type:'native_retry_end',success:true,attempt:1}}]);
    return {{answer:answer.content[0].text,retries:1}};
}}
async function retiredDuringBackoff() {{
    const events=[]; const owner=prepare(events);
    owner.agent.state.messages.push(refused);
    owner.run.observe({{type:'message_end',message:refused}},claims);
    const wait=owner.run.recover(refused);
    owner.run.close();owner.run.close();
    assert.equal(await wait,false);
    assert.equal(owner.run.willRetry(),false);
    assert.deepEqual(events.filter(e=>e.type==='native_retry_end'),[
        {{type:'native_retry_end',success:false,attempt:0,
            finalError:'Provider retry closed before completing'}}]);
    assert.equal(owner.agent.state.messages.length,1);
}}
const [events,,,,proof,noOutput] = await Promise.all([
    differentTerminal(), answered(), exhausted(), retiredDuringBackoff(), toolThenCodexRefusals(),
    thinkingOnlyThenAnswer(),
]);
console.log(JSON.stringify({{events,proof,noOutput}}));
"""
    process = await asyncio.create_subprocess_exec(
        "node", "--input-type=module", "-e", program,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 80)
    assert process.returncode == 0, error.decode()
    return json.loads(output)


@pytest.mark.asyncio
async def test_clean_stops_leave_answer_validation_to_tracked_terminal():
    package = pinned_native_package()
    source = Path(__file__).parents[1] / "stack/native-turn-recovery.mjs"
    program = f"""
import assert from 'node:assert/strict';
import {{ NativeTurnRun }} from {json.dumps(source.as_uri())};
import {{ isRetryableAssistantError }} from {json.dumps((package / 'node_modules/@earendil-works/pi-ai/dist/utils/retry.js').as_uri())};
const events=[]; const agent={{state:{{messages:[]}}}};
const claims=new Map([['original',{{digest:'original'}}],['notification',{{digest:'notification'}}]]);
const run=new NativeTurnRun(agent,event=>events.push(event),isRetryableAssistantError);
run.include([{{role:'user',inputId:'original'}}],claims);
function finished(message) {{
    agent.state.messages.push(message);
    run.observe({{type:'message_start',message:{{role:'assistant',content:[]}}}},claims);
    run.observe({{type:'message_end',message}},claims);
}}
const empty={{role:'assistant',content:[],stopReason:'stop'}};
finished(empty);
assert.equal(run.willRetry(),false);
assert.equal(await run.recover(empty),false);
assert.deepEqual(events,[]); // Even a required empty original is not a provider error.
finished({{...empty,content:[{{type:'text',text:'Requested answer'}}]}});
run.observe({{type:'message_start',message:{{role:'user',inputId:'notification'}}}},claims);
finished(empty);
assert.equal(run.willRetry(),false);
assert.deepEqual(events,[]);
const refusal={{role:'assistant',content:[],stopReason:'error',errorMessage:'503 overloaded'}};
finished(refusal); // A clean empty request must not close the next request's retry window.
assert.equal(run.willRetry(),true);
assert.equal(await run.recover(refusal),true);
finished(empty); // Provider recovery succeeded; Core still decides if an answer is required.
assert.equal(run.willRetry(),false);
assert.deepEqual(events.filter(event=>event.type==='native_retry_end'),[
    {{type:'native_retry_end',success:true,attempt:1}}]);
run.close();
console.log('clean stops stay clean; real provider failures still recover');
"""
    process = await asyncio.create_subprocess_exec(
        "node", "--input-type=module", "-e", program,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, error.decode()
    assert b"real provider failures still recover" in output


@pytest.mark.asyncio
@pytest.mark.usefixtures("native_rpc_fixture")
async def test_native_closed_window_preserves_terminal_diagnostics_in_completed_outcome(
    wired, tmp_path, monkeypatch,
):
    # Retry/end events are produced by the compiled native owner and classifier,
    # not invented by Core. The rest is the existing recorded RPC transport.
    result = await native_records()
    records = result["events"]
    assert result["proof"] == {
        "tries": 4, "retries": 3, "answer": "recovered after tools",
    }
    assert result["noOutput"] == {"answer": "visible answer", "retries": 1}
    print(json.dumps({"refusal": result["proof"], "no_output": result["noOutput"]}, sort_keys=True))
    stub = _stub_with(tmp_path, [*records, *_settled_records()])
    agent = canonical_agent(wired, agent_bin="pi", agent_args=[], runtime_enabled=True)
    original_stream = backend.stream_agent_events

    async def recorded(agent_bin, agent_args, task, cwd, *args, **kwargs):
        async for event in original_stream(stub, agent_args, task, cwd, *args, **kwargs):
            yield event

    monkeypatch.setattr(backend, "stream_agent_events", recorded)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    agent.sessions.client = Client()
    try:
        await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
        with pytest.raises(NativeTurnError) as caught:
            await asyncio.wait_for(agent.turns.run_agent_turn("project", "project", "fresh isolated work"), 20)
        failures = [item for update in updates for item in decode_updates(update.field_meta)
                    if isinstance(item, RequestFailedUpdate)]
        assert len(failures) == 1
        failure = failures[0]
        assert failure.failure.detail == FINAL
        assert failure.failure.retry_result.attempt == 1
        assert failure.failure.retry_result.success is False
        assert failure.failure.retry_result.final_error == FINAL
        assert isinstance(failure.source, NativeMessageFailureSource)
        assert failure.source.message.error_message == FINAL
        assert len(failure.failure.diagnostics) == 1
        diagnostic = failure.failure.diagnostics[0]
        assert isinstance(diagnostic, ProviderTransportFailureDiagnostic)
        assert diagnostic.error.code == "EOPAQUE" and diagnostic.error.message == FINAL
        assert failure.source.message.diagnostics == failure.failure.diagnostics
        request_failure = caught.value.request
        assert request_failure.notification_published
        assert request_failure.failure == failure.failure
        assert not any(getattr(getattr(update, "content", None), "text", None) for update in updates)
    finally:
        await agent.shutdown()
