"""Current ACP owner inputs through native RPC pipes and the actual owner socket.

The local executable is an explicit protocol fixture, not a provider or native
package acceptance claim. No input store, queue or socket result is mocked.
"""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.input_disposition import InputDispositions
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_delivery_owner
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


class Updates:
    def __init__(self):
        self.rows = []

    async def session_update(self, *, session_id, update):
        self.rows.append(
            update
            if isinstance(update, dict)
            else update.model_dump(by_alias=True, exclude_none=True)
        )


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def rpc_process(root, *, capability=True, second_start=False):
    """A real child ACKs commands and emits only the explicitly selected starts."""
    session = root / "session.jsonl"
    session.touch()
    received = root / "received.jsonl"
    release = root / "release"
    child = root / "native-rpc-fixture"
    child.write_text(
        f"#!{sys.executable}\n"
        + f"session={str(session)!r}\nreceived={str(received)!r}\nrelease={str(release)!r}\n"
        + f"capability={capability!r}\nsecond_start={second_start!r}\n"
        + """import json, sys, time
from pathlib import Path
Path(session + '.launches').open('a').write('x')
def send(event):
    print(json.dumps(event), flush=True)
count = 0
for line in sys.stdin:
    command = json.loads(line)
    kind = command['type']
    data = {}
    if kind == 'get_state':
        data = {'sessionFile':session}
        if capability:
            data['nativeInputProofCapability'] = 'pi-native-input-v1-live-only'
    send({'type':'response', 'command':kind, 'id':command.get('id'), 'success':True, 'data':data})
    if kind == 'prompt':
        with open(received, 'a') as out:
            out.write(json.dumps(command) + '\\n')
        count += 1
        if count == 1:
            send({'type':'message_start', 'message':{'role':'user',
                 'content':command['message'], 'inputId':command['inputId']}})
        if count == 2:
            while not Path(release).exists():
                time.sleep(.01)
            if second_start:
                send({'type':'message_start', 'message':{'role':'user',
                     'content':command['message'], 'inputId':command['inputId']}})
            send({'type':'message_end', 'message':{'role':'assistant','stopReason':'stop'}})
            send({'type':'agent_settled'})
            if not second_start:
                break
"""
    )
    child.chmod(0o700)
    return child, session, received, release


@pytest.mark.usefixtures("native_rpc_fixture")
async def test_native_preflight_failure_is_visible_and_cannot_mark_started(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    child, _session, received, _release = rpc_process(tmp_path, capability=False)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, _message, _root):
        owner.inputs.auto_wake = False
        owner.turns.agent_bin, owner.turns.agent_args = str(child), []
        updates = Updates()
        owner.on_connect(updates)
        await asyncio.wait_for(
            owner.prompt("beta", [TextContentBlock(type="text", text="test")]), 6
        )
        rows = owner.inputs.dispositions.read().unknown(frozenset({"beta"}))
        assert len(rows) == 1 and not hasattr(rows[0], "native_id")
        assert not received.exists()
        text = "\n".join(row.get("content", {}).get("text", "") for row in updates.rows)
        assert "[agent error]" in text and "preflight" in text
        assert not owner.inputs.pending_turns and not owner.turns.active_turns
        assert Comms(comms.root).goals.unresolved_inputs("beta") == [row.public() for row in rows]


@pytest.mark.usefixtures("native_rpc_fixture")
@pytest.mark.parametrize("second_start", [False, True])
async def test_late_owner_socket_observes_unknown_until_exact_native_start(
    tmp_path, monkeypatch, second_start
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    child, session_file, received, release = rpc_process(tmp_path, second_start=second_start)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, _message, root_id):
        owner.inputs.auto_wake = False
        owner.turns.agent_bin, owner.turns.agent_args = str(child), []
        started = asyncio.Event()
        original_emit = owner._emit_event

        async def observe(session_id, event, client=None, **kwargs):
            await original_emit(session_id, event, client, **kwargs)
            if isinstance(event, ae.InputStarted) and event.id is None:
                started.set()

        monkeypatch.setattr(owner, "_emit_event", observe)
        turn = asyncio.create_task(
            owner.prompt("beta", [TextContentBlock(type="text", text="first")])
        )
        attachment = CommsAgent(comms, runtime_enabled=False)
        updates = Updates()
        attachment.on_connect(updates)
        proxy = RuntimeProxy(attachment, "beta", socket_path(comms.root, os.getpid()))
        try:
            await asyncio.wait_for(started.wait(), 5)
            accepted = await proxy.request(
                "prompt",
                prompt=[{"type": "text", "text": "followup"}],
                meta={"agentComms": {"delivery": "steer"}},
            )
            public_id = accepted["_meta"]["agentComms"]["inputDisposition"]["inputId"]
            key = f"acp:{public_id}"
            await wait_for(
                lambda: received.exists() and len(received.read_text().splitlines()) == 2
            )
            await proxy.subscribe()
            pending = await proxy.request("input_dispositions")
            assert [row["inputId"] for row in pending["inputs"]] == [public_id]
            assert any(
                row.get("_meta", {})
                .get("agentComms", {})
                .get("inputDisposition", {})
                .get("inputId")
                == public_id
                for row in updates.rows
            )
            unknown = owner.inputs.dispositions.read().rows[key]
            assert unknown.unresolved and unknown.native_id is not None  # ACK is not STARTED.
            release.touch()
            await asyncio.wait_for(turn, 6)
            final = InputDispositions(owner.inputs.dispositions.path).read().rows
            assert len(final) == 2
            assert final[key].unresolved is (not second_start)
            assert len({row.native_id for row in final.values()}) == 2
            commands = [json.loads(line) for line in received.read_text().splitlines()]
            assert commands[1]["message"] == "User follow-up:\nfollowup"
            assert commands[0]["inputId"] != commands[1]["inputId"]
            overview = await proxy.request("input_dispositions", include_history=True)
            assert overview["inputs"] == []
            assert overview["historicalCount"] == (0 if second_start else 1)
            cleared = await proxy.request("dismiss_historical_inputs")
            assert cleared["dismissedHistoricalCount"] == (0 if second_start else 1)
            reopened = InputDispositions(owner.inputs.dispositions.path).read().rows
            assert {k: replace(v, notice_dismissed=False) for k, v in reopened.items()} == final
            # A new owner projection never reconstructs a runnable queue from UNKNOWN.
            other = CommsAgent(
                Comms(comms.root),
                auto_wake=False,
                private_nk_native_package=tmp_path,
                private_nk_wire_root_id=root_id,
            )
            other.sessions.bindings["beta"] = "beta"
            try:
                assert await other.inputs.drain_inbox("beta") == 0
                assert not other.inputs.pending_turns and not other.inputs.wake_tasks
                assert Path(str(session_file) + ".launches").read_text() == "x"
                assert other.inputs.dispositions.read().rows == reopened
            finally:
                # No owned bindings were acquired by this inspection-only instance.
                other.sessions.bindings.clear()
                await other.shutdown()
        finally:
            release.touch()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
            await proxy.close()
            await attachment.shutdown()


def test_hard_exit_after_owner_acceptance_preserves_unknown_without_replay(tmp_path):
    code = """import asyncio, os, sys
from pathlib import Path
from delivery_owner_fixture import canonical_delivery_owner
async def main():
    async with canonical_delivery_owner(Path(sys.argv[1]), direct=True) as (_, owner, _, _):
        async def die(*args, **kwargs):
            os._exit(17)
        owner.inputs.emit_input_disposition = die
        await owner.inputs.run_owned_input('beta', 'beta', 'Retain exact crash input')
asyncio.run(main())
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parents[1] / "src"), str(Path(__file__).parent))
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 17, child.stderr
    comms = Comms(tmp_path / "wire")
    ledger = InputDispositions(comms.root / InputDispositions.filename)
    before = ledger.path.read_bytes()
    rows = ledger.read().unknown(frozenset({"beta"}))
    assert len(rows) == 1 and rows[0].source_text == "Retain exact crash input"
    assert not hasattr(rows[0], "native_id") and not hasattr(rows[0], "sent_text")
    thread = comms.registry.require("beta")
    comms.registry.register(replace(thread, process_identity=ProcessIdentity.capture(os.getpid())))

    async def inspect():
        owner = CommsAgent(
            comms,
            auto_wake=False,
            private_nk_native_package=tmp_path,
            private_nk_wire_root_id=comms.bus.log.read_metadata_unlocked().wire_root_id,
        )
        owner.sessions.bindings["beta"] = "beta"
        try:
            assert await owner.inputs.drain_inbox("beta") == 0
            assert not owner.inputs.pending_turns and not owner.inputs.wake_tasks
            assert owner.inputs.awaiting_input_keys("beta") == frozenset()
        finally:
            await owner.shutdown()

    asyncio.run(inspect())
    assert ledger.path.read_bytes() == before


@pytest.mark.refactor_guard
def test_disposition_fixtures_cannot_reintroduce_retired_delivery_engine():
    import ast

    for name in (
        "test_acp_input_disposition.py",
        "test_acp_channel_disposition.py",
        "delivery_owner_fixture.py",
    ):
        tree = ast.parse(Path(__file__).with_name(name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"delivery_cursors", "inbox_cursors", "legacy_through"}
            if isinstance(node, ast.ImportFrom):
                assert not {alias.name for alias in node.names} & {
                    "AcpDeliveryCursors",
                    "DeliveryCursor",
                    "DeliveryDocument",
                    "wire",
                }
