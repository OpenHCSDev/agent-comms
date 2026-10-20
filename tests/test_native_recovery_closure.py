"""Compiled native recovery owns closure; Core preserves the original failure."""
from agent_comms.native_package import pinned_native_package

import asyncio
import json

import pytest


FINAL = "Opaque provider transport failure"
DIAGNOSTIC = {
    "type": "provider_transport_failure",
    "error": {"name": "OpaqueTransportFailure", "message": FINAL, "code": "EOPAQUE"},
    "details": {"configuredTransport": "http", "phase": "before_message_stream_start",
                "eventsEmitted": False, "requestBytes": 123},
}


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


