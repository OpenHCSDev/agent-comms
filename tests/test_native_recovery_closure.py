"""Compiled native recovery owns closure; Core preserves the original failure."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms import backend, cohort_foreground, native_pi
from agent_comms.acp_extension import RequestFailedUpdate, decode_updates
from agent_comms.acp_failure import NativeMessageFailureSource, RequestFailure
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import ProviderTransportFailureDiagnostic
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
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
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
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
const events=[];
const {{agent,run}}=prepare(events);
const refused={{role:'assistant',content:[],stopReason:'error',errorMessage:'503 overloaded'}};
function finished(message) {{
    agent.state.messages.push(message);
    events.push({{type:'message_start',message:{{role:'assistant',content:[]}}}});
    run.observe({{type:'message_end',message}},claims);
    events.push({{type:'message_end',message}});
}}
finished(refused);
assert.equal(await run.recover(refused),true); // Actual one-second native backoff.
finished(terminal);
assert.equal(await run.recover(terminal),false);
run.close();run.close();run.cancel();
assert.equal(JSON.stringify(terminal),original); // No replacement/mutation.
const ends=events.filter(e=>e.type==='native_retry_end');
assert.deepEqual(ends,[{{type:'native_retry_end',success:false,attempt:1,finalError:terminal.errorMessage}}]);
assert(events.indexOf(ends[0]) < events.findIndex(e=>e.type==='message_end' && e.message===terminal));

// Retiring a pending backoff also closes once, cancels its admitted wait, and
// cannot let the old wait overwrite the closed state or re-request context.
const retiredEvents=[];
const retired=prepare(retiredEvents);
retired.agent.state.messages.push(refused);
retired.run.observe({{type:'message_end',message:refused}},claims);
const wait=retired.run.recover(refused);
retired.run.close();retired.run.close();
assert.equal(await wait,false);
assert.equal(retired.run.willRetry(refused),false);
assert.deepEqual(retiredEvents.filter(e=>e.type==='native_retry_end'),[
    {{type:'native_retry_end',success:false,attempt:0}}]);
assert.equal(retired.agent.state.messages.length,1);
console.log(JSON.stringify(events));
"""
    process = await asyncio.create_subprocess_exec(
        "node", "--input-type=module", "-e", program,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 12)
    assert process.returncode == 0, error.decode()
    return json.loads(output)


@pytest.mark.asyncio
@pytest.mark.usefixtures("native_rpc_fixture")
async def test_native_closed_window_preserves_terminal_diagnostics_in_completed_outcome(
    wired, tmp_path, monkeypatch,
):
    # Retry/end events are produced by the compiled native owner and classifier,
    # not invented by Core. The rest is the existing recorded RPC transport.
    records = await native_records()
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
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    route = PrivateNkLaunch(Path(agent._comms.root), agent._configured_root_id(), package, None)
    monkeypatch.setattr(PrivateNkLaunch, "current", classmethod(lambda cls: route))
    monkeypatch.setattr(cohort_foreground, "_preflight",
                        lambda root, root_id, pkg, enabled: native_pi._trusted_package(pkg))
    try:
        await agent.new_session(cwd=str(tmp_path / "project"), mcp_servers=[])
        with pytest.raises(RequestError) as caught:
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
        request_failure = RequestFailure.from_error(caught.value.code, str(caught.value), caught.value.data)
        assert request_failure.notification_published
        assert request_failure.failure == failure.failure
        assert not any(getattr(getattr(update, "content", None), "text", None) for update in updates)
    finally:
        await agent.shutdown()
