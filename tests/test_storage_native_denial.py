"""The actual saved-history native process persists the owner's typed denial."""
import asyncio
import json
import shlex

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_payloads import NativeToolDetails
from delivery_owner_fixture import canonical_agent, reply_turn_finished


async def test_storage_refusal_survives_real_native_result_and_next_call(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    forbidden = native.project / '.artifacts' / 'p303-never-created'
    commands = (shlex.join(('mkdir', '-p', str(forbidden))), 'printf P303_NEXT_RAN')
    provider = native.provider

    def responses():
        number = provider.posts
        assert 1 <= number <= 3, 'no provider retry or original-input replay'
        if number <= len(commands):
            yield {'tool_calls': [{'index': 0, 'id': f'p303-{number}', 'type': 'function',
                'function': {'name': 'bash', 'arguments': json.dumps({'command': commands[number - 1]})}}]}, None
            yield {}, 'tool_calls'
        else:
            yield {'content': 'P303_DONE'}, None
            yield {}, 'stop'

    monkeypatch.setattr(provider, 'response_chunks', responses)
    owner = canonical_agent(Comms(native.root), auto_wake=True, runtime_enabled=True,
        agent_bin='pi', agent_args=native.native_arguments(()))
    owner.on_connect(None)
    try:
        await owner._runtime.start()
        session = await native.attach_saved_owner(owner, project=native.project, session=native.session)
        comms = owner._comms
        comms.threads.claim_thread('verifier', tags=frozenset(), worktree=str(native.project))
        comms.messaging.send('verifier', session, 'Fresh P303 storage preflight verification.').message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 45)
        assert native.session.read_bytes().startswith(original)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        tools = [row['message'] for row in rows if row.get('type') == 'message'
                 and row['message'].get('role') == 'toolResult']
        assert [tool['toolCallId'] for tool in tools] == ['p303-1', 'p303-2']
        assert tools[0]['isError'] and not forbidden.exists()
        details = FieldCodec.decode(NativeToolDetails, tools[0]['details'])
        assert details.storage_refusals()[0].reason == 'StorageRule: writes inside .artifacts are forbidden'
        assert not tools[1]['isError'] and 'P303_NEXT_RAN' in tools[1]['content'][0]['text']
        assert provider.posts == 3
    finally:
        await owner.shutdown()
        assert not owner.turns.turn_tasks and not owner.turns.persistent_backends
