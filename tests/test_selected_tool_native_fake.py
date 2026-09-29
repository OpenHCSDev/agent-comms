"""Fake-RPC provider-free handshake; no pinned Pi CLI or external model."""

from __future__ import annotations

import sys
from pathlib import Path

from native_proof_cases import child_writer_imports, read_proof_rows, write_proof_rows

import pytest

from agent_comms import native_pi as native
from agent_comms import selected_tool_broker as broker
from agent_comms.child_process import AttachedChild
from agent_comms.selected_tool_broker import (
    SelectedToolMode,
    consume_selected_slot,
    record_selected_terminal,
)
from agent_comms.tracked_turn import TrackedTurnSession

INPUT_ID = "a" * 32


@pytest.mark.skipif(sys.platform != "linux", reason="selected tool socket requires SO_PEERCRED")
@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["success", "tamper", "denied", "badproof", "forged_terminal"])
async def test_fake_rpc_tool_event_and_terminal_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    fake = tmp_path / "fake_selected_rpc.py"
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/selected_claimed_write.mjs").write_bytes(
        Path(broker.__file__).with_name("selected_claimed_write.mjs").read_bytes()
    )
    fake.write_text(child_writer_imports() + """import json, os, socket, sys
from pathlib import Path
session_dir = Path(sys.argv[1]); variant = sys.argv[2]
file = session_dir / 'test.jsonl'
def send(value): print(json.dumps(value), flush=True)
state = json.loads(sys.stdin.readline())
send({'type':'response','id':state['id'],'command':'get_state','success':True,
      'data':{'nativeInputProofCapability':'pi-native-input-v1-live-only',
              'sessionId':'sid','sessionFile':str(file)}})
command = json.loads(sys.stdin.readline()); iid = command['inputId']
entry = {'type':'message','id':'entry','message':{'role':'user','inputId':iid,'inputDigest':'b'*64}}
file.write_text(json.dumps({'type':'session','id':'sid'})+'\\n'+json.dumps(entry)+'\\n')
proof = {'schema':1,'type':'context_committed','sessionId':'sid','inputId':iid,
    'sessionEntryId':'entry','requestGeneration':1,'llmContextDigest':'c'*64}
write_proof_rows(file, [proof])
os.chmod(file,0o600); os.chmod(str(file)+'.input-proof',0o600)
send({'type':'response','id':command['id'],'command':'prompt','success':True})
send({'type':'input_committed','sessionId':'sid','inputId':iid,'sessionEntryId':'entry'})
send({**proof, 'llmContextDigest':'d'*64} if variant == 'badproof' else proof)
args = {'resource':'notes.txt','contents':'new contents'}
call = {'type':'toolCall','id':'call_1','name':'selected_claimed_write','arguments':args}
send({'type':'message_end','message':{'role':'assistant','stopReason':'toolUse','content':[call]}})
send({'type':'tool_execution_start','toolCallId':'call_1','toolName':'selected_claimed_write','args':args})
sock = socket.socket(socket.AF_UNIX); sock.connect(os.environ['AGENT_COMMS_SELECTED_TOOL_SOCKET'])
wire = {'token':os.environ['AGENT_COMMS_SELECTED_TOOL_TOKEN'],
        'request':{'call_id':'call_1','arguments':{'resource':'notes.txt',
        'contents':'new contents' if variant != 'tamper' else 'different'}}}
sock.sendall((json.dumps(wire)+'\\n').encode()); answer = b''
while not answer.endswith(b'\\n'): answer += sock.recv(1024)
sock.close(); ok = json.loads(answer)['ok']
send({'type':'tool_execution_end','toolCallId':'call_1','toolName':'selected_claimed_write',
      'isError':not ok and variant != 'forged_terminal','result':{'content':[
          {'type':'text','text':'committed' if ok else 'denied'}]}})
if ok or variant == 'forged_terminal':
    send({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'Done'}})
    send({'type':'message_end','message':{'role':'assistant','stopReason':'stop',
          'content':[{'type':'text','text':'Done'}]}})
send({'type':'agent_settled'})
""")
    orig = AttachedChild.start

    async def launch(*_argv: str, **kwargs: object):
        return await orig(
            (sys.executable, "-u", str(fake), str(tmp_path / "sessions"), variant), **kwargs
        )

    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    monkeypatch.setattr(AttachedChild, "start", launch)
    observed: list[str] = []

    def owner_action(request) -> None:
        observed.append(request.call_id)
        consume_selected_slot(tmp_path / "sessions", INPUT_ID, request.call_id)
        if variant == "denied":
            raise ValueError("simulate current owner revocation")
        if variant == "forged_terminal":
            real_sync = broker._sync_dir

            def fail_terminal_parent(path: Path) -> None:
                if path.name == "selected-tool-ledger":
                    raise OSError("simulated lost .done parent fsync")
                real_sync(path)

            with monkeypatch.context() as patch:
                patch.setattr(broker, "_sync_dir", fail_terminal_parent)
                record_selected_terminal(tmp_path / "sessions", INPUT_ID, request.call_id)
        else:
            record_selected_terminal(tmp_path / "sessions", INPUT_ID, request.call_id)

    operation = TrackedTurnSession.execute(
        tmp_path,
        input_id=INPUT_ID,
        prompt="Selected request",
        worktree=tmp_path,
        session_dir=tmp_path / "sessions",
        selected_tool_mode=SelectedToolMode(owner_action),
        timeout=15,
    )
    if variant == "success":
        result = await operation
        assert result.text == "Done"
        assert result.selected_tool_call_id == "call_1"
        assert observed == ["call_1"]
    else:
        with pytest.raises(native.NativePiUnavailable):
            await operation
        assert observed == ([] if variant in {"tamper", "badproof"} else ["call_1"])
        terminal = tmp_path / "sessions" / "selected-tool-ledger" / (INPUT_ID + ".done")
        assert terminal.exists() is (variant == "forged_terminal")
