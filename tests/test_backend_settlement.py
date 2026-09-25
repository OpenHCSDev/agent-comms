"""A late native settlement cannot terminate a started dependency reply."""

import asyncio
import sys

import pytest

from agent_comms import backend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="executes a POSIX script")


@pytest.mark.asyncio
@pytest.mark.parametrize("start_before_settlement", [False, True])
@pytest.mark.parametrize("inject_during_stats", [False, True])
async def test_followup_outlives_previous_native_settlement(
    tmp_path, start_before_settlement, inject_during_stats
):
    session = tmp_path / "session.jsonl"
    session.write_text("session\n")
    stub = tmp_path / "pi-stub"
    stub.write_text(f"#!{sys.executable}\n" + f"""
import json, sys, threading, time
lock = threading.Lock()
busy = False
def send(event):
    with lock:
        print(json.dumps(event), flush=True)
def start(command):
    send({{"type":"message_start", "message":{{"role":"user",
          "content":command["message"], "inputId":command["inputId"]}}}})
def stop(index):
    send({{"type":"message_end", "message":{{"role":"assistant",
          "stopReason":"stop", "usage":{{"totalTokens":index}}}}}})
def finish():
    global busy
    time.sleep(.15)
    stop(2)
    busy = False
    send({{"type":"agent_settled"}})
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_state":
        send({{"type":"response", "command":kind, "id":command.get("id"),
              "success":True, "data":{{"nativeInputProofCapability":
              "pi-native-input-v1-live-only", "sessionId":"same",
              "sessionFile":{str(session)!r}, "isStreaming":busy}}}})
    elif kind == "get_session_stats":
        send({{"type":"response", "command":kind, "id":command.get("id"),
              "success":True, "data":{{"contextUsage":{{"tokens":2}}}}}})
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
    queue = asyncio.Queue()
    finish_event = asyncio.Event()
    persistent = backend.PersistentPiSession()
    starts = []
    events = []
    injected = False

    async def collect():
        nonlocal injected
        async for event in backend.stream_agent_events(
            str(stub),
            [],
            "first",
            str(tmp_path),
            session_file=str(session),
            steering_queue=queue,
            finish_event=finish_event,
            persistent_session=persistent,
            native_start=lambda public_id, native_id, text: starts.append(text) or True,
        ):
            events.append(event)
            inject = (
                event["type"] == "agent_info" and event.get("context_used") == 2
                if inject_during_stats
                else event["type"] == "provider_usage" and event["usage"]["totalTokens"] == 1
            )
            if inject and not injected:
                injected = True
                queue.put_nowait({"type": "prompt", "message": "second", "_input_id": "late"})
                if not inject_during_stats:
                    await asyncio.sleep(0.025)
            if event["type"] == "settled":
                finish_event.set()

    try:
        await asyncio.wait_for(collect(), 3)
        assert events[-1]["ok"], events[-1]
        assert starts == ["first", "second"]
        usages = [i for i, e in enumerate(events) if e["type"] == "provider_usage"]
        settlements = [i for i, e in enumerate(events) if e["type"] == "settled"]
        assert len(usages) == 2
        assert len(settlements) == 1 and settlements[0] > usages[-1]
        assert persistent.proc is not None and persistent.proc.returncode is None
    finally:
        await persistent.close()
