"""A late native settlement cannot terminate a started dependency reply."""

import asyncio
import sys
from pathlib import Path

import pytest

import agent_comms.agent_events as ae
from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.pi_counts import PiCount
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.fresh_private_session import create_fresh_private_session
from pi_session_turn import session_turn_events

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="executes a POSIX script")


@pytest.mark.usefixtures("native_rpc_fixture")
@pytest.mark.asyncio
@pytest.mark.parametrize("start_before_settlement", [False, True])
@pytest.mark.parametrize("inject_during_stats", [False, True])
async def test_followup_outlives_previous_native_settlement(
    tmp_path, start_before_settlement, inject_during_stats
):
    identity = create_fresh_private_session(tmp_path / 'sessions', worktree=tmp_path)
    session, session_id = identity.path, identity.session_id
    stub = tmp_path / "pi-stub"
    stub.write_text(f"#!{sys.executable}\n" + f"""
import json, sys, threading, time
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import request_response, read_request, send as send_row
lock = threading.Lock()
busy = False
def send(event):
    with lock:
        send_row(event)
def start(command):
    send({{"type":"message_start", "message":{{"role":"user",
          "content":command["message"], "inputId":command["inputId"]}}}})
def stop(index):
    send({{"type":"message_end", "message":{{"role":"assistant",
          "stopReason":"stop", "content":[{{"type":"text", "text":"answer"}}],
          "usage":{{"totalTokens":index}}}}}})
def finish():
    global busy
    time.sleep(.15)
    stop(2)
    busy = False
    send({{"type":"agent_settled"}})
for line in sys.stdin:
    command = read_request(line)
    kind = command["type"]
    if kind == "get_state":
        send(request_response(command, {{"nativeInputProofCapability":
              "pi-native-input-v1-live-only", "sessionId":{session_id!r},
              "sessionFile":{str(session)!r}, "isStreaming":busy}}))
    elif kind == "get_session_stats":
        send(request_response(command, {{"contextUsage":{{"tokens":2}}}}))
    elif kind == "prompt":
        send({{"type":"response", "command":kind, "id":command["id"], "success":True}})
        if command["message"] == "first":
            start(command)
            stop(1)
            if {inject_during_stats!r}:
                send({{"type":"agent_settled"}})
        else:
            busy = True
            if {start_before_settlement!r}:
                start(command)
            send({{"type":"agent_settled"}})
            if not {start_before_settlement!r}:
                start(command)
            threading.Thread(target=finish, daemon=True).start()
""")
    stub.chmod(0o755)
    queue = TurnInbox()
    finish_event = asyncio.Event()
    persistent = PersistentPiSession()
    starts = []
    events = []
    injected = False

    async def collect():
        nonlocal injected
        async for event in session_turn_events(
            persistent, str(stub),
            [],
            "first",
            str(tmp_path),
            session_file=str(session),
            steering_queue=queue,
            finish_event=finish_event,
            native_start=lambda public_id, native_id, text: starts.append(text) or True,
        ):
            events.append(event)
            inject = (
                isinstance(event, ae.ContextUsageObservation) and event.context_used == PiCount.from_wire(2)
                if inject_during_stats
                else isinstance(event, ae.ProviderUsage) and event.usage.total_tokens == PiCount.from_wire(1)
            )
            if inject and not injected:
                injected = True
                queue.put_nowait(InputRequest(input_id=InputId("late"), content=InputContent(text="second"), when_busy=WhenBusy.STEER))
                if not inject_during_stats:
                    await asyncio.sleep(0.025)
            if isinstance(event, ae.StreamSettled):
                finish_event.set()

    try:
        await asyncio.wait_for(collect(), 3)
        assert events[-1].ok, events[-1]
        assert starts == ["first", "second"]
        usages = [i for i, e in enumerate(events) if isinstance(e, ae.ProviderUsage)]
        settlements = [i for i, e in enumerate(events) if isinstance(e, ae.StreamSettled)]
        assert len(usages) == 2
        assert len(settlements) == 1 and settlements[0] > usages[-1]
        assert persistent.available and persistent.child_state.child.proc.returncode is None
    finally:
        await persistent.close()
