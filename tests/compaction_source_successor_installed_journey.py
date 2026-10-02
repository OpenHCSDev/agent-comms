"""Configured saved fork: ordinary peer publication during native summary, then new input.

The SDK/router, journal, native child, writer and private wire are production.
The notification receiver publishes a real private peer message; it does not
replace state, protocol, source checks or provider responses. No public input.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from acp.agent.router import build_agent_router
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CompactRequest, CompactionChangedUpdate, decode_updates, encode_request
from agent_comms.agent_events import CompactionSummaryProgress
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.native_package import verify_native_package
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.threads import Thread


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def run(stage, package, source_file):
    import agent_comms
    installed = Path(agent_comms.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[1]
    assert installed.is_relative_to(Path(sys.prefix))
    for path in (checkout/'src/agent_comms').rglob('*.py'):
        assert path.read_bytes() == (installed/path.relative_to(checkout/'src/agent_comms')).read_bytes()
    stage.mkdir(mode=0o700, exist_ok=False)
    started = time.monotonic()
    receipt = {'complete': False, 'public_inputs': 0, 'input_replays': 0,
               'installed_UI': False, 'acceptance_scope': 'configured SDK/ACP/native saved-source compaction and distinct input'}
    public = wire()
    snapshot = public.registry.snapshot()
    original = snapshot.require_active('openhcs-architecture-memory')
    launch = RetainedOwnerLaunch.capture(original, snapshot)
    original_hash = digest(source_file)
    verify_native_package(package)
    fork = await ForkSessionHelper.run(ForkSessionRequest(
        str(package), str(source_file), original.worktree, str(stage/'forks')),
        cwd=Path(original.worktree), env=dict(launch.environment))
    service = Comms(stage/'wire')
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    binary = Path(sys.executable).with_name('pi-comms-native')
    environment = dict(launch.environment)
    environment.update(AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_AGENT_BIN=str(binary), AGENT_COMMS_RUNTIME_ROOT=str(binary.parent),
        PATH=str(binary.parent)+os.pathsep+environment.get('PATH',''))
    for key in ('PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID',
                'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY', 'PYTHONPATH'):
        environment.pop(key, None)
    identity = ProcessIdentity.capture(os.getpid())
    for name in ('source529','peer529'):
        service.registry.declare(Thread(name, frozenset({'source529'}), original.worktree,
            parent=original.name, process_identity=identity,
            session_file=fork.session_file if name=='source529' else None,
            model=original.model, thinking_level=original.thinking_level))
    owner = service.registry.require('source529')
    environment.update(owner.native_environment(service.root, service.registry.snapshot(), owner.worktree))
    os.environ.clear(); os.environ.update(environment)
    agent = CommsAgent(service, agent_bin=str(binary), agent_args=list(launch.arguments or ()),
        runtime_enabled=True, auto_wake=False, private_nk_native_package=package,
        private_nk_wire_root_id=root_id)
    peer_publications = []
    text_chunks = []
    class Receiver:
        async def session_update(self, **value):
            update = value['update']
            for item in decode_updates(update.field_meta):
                if isinstance(item, CompactionChangedUpdate) and isinstance(item.event, CompactionSummaryProgress) and not peer_publications:
                    message = service.messaging.send_message('peer529','#source529',
                        'Distinct private peer reply while the original summary is streaming.', notice=True)
                    peer_publications.append(message.reference)
                    print('PEER_PUBLISHED_DURING_SUMMARY', flush=True)
            if update.session_update=='agent_message_chunk' and update.content.type=='text':
                text_chunks.append(update.content.text)
    agent.on_connect(Receiver())
    try:
        await agent.sessions.bind_owned(owner, owner.name)
        router = build_agent_router(agent)
        receipt.update(model=original.model, thinking=ThinkingLevel.optional_name(original.thinking_level),
            source_bytes=source_file.stat().st_size, original_sha256=original_hash,
            fork_bytes=Path(fork.session_file).stat().st_size)
        print('CONFIGURED_SAVED_COMPACTION_STARTED', flush=True)
        await router('session/prompt', {'sessionId':owner.name,
            'prompt':[{'type':'text','text':' '}], '_meta':encode_request(CompactRequest(
                'Preserve exact decisions and original source coordinates concisely. Do not resume prior work.'))}, False)
        journal = CompactionJournal(service.root/'compaction-commits.sqlite3')
        (attempt,) = journal.summaries.history(fork.session_file)
        assert isinstance(attempt.state, ManualCommittedSummary)
        operation = journal.operations.get(attempt.state.commit_id)
        operation.committed_outcome()
        assert peer_publications
        assert InputDispositions(service.root/InputDispositions.filename).read().rows=={}
        text_chunks.clear()
        marker='SOURCE529_DISTINCT_AFTER_COMMIT'
        print('CONFIGURED_DISTINCT_INPUT_STARTED', flush=True)
        result = await router('session/prompt', {'sessionId':owner.name,
            'prompt':[{'type':'text','text':f'New bounded acceptance input. Do not use tools or resume inherited work. Reply exactly {marker}.'}]}, False)
        assert result.stop_reason=='end_turn'
        assert marker in ''.join(text_chunks)
        inputs=InputDispositions(service.root/InputDispositions.filename).read()
        assert len(inputs.rows)==1 and all(row.has_started for row in inputs.rows.values())
        assert service.registry.require(owner.name).active_turn is None
        receipt.update(complete=True, manual_commit=operation.commit_id,
            peer_messages_during_summary=len(peer_publications), original_inputs_before_new_prompt=0,
            distinct_input_started_once=True, distinct_answer=True)
    except BaseException as error:
        receipt['error']={'type':type(error).__name__,'detail':str(error)}
        raise
    finally:
        children = [backend.custody.child.proc for backend in agent.turns.persistent_backends.values() if backend.available]
        await agent.shutdown()
        receipt.update(elapsed_seconds=time.monotonic()-started,
            original_source_unchanged=digest(source_file)==original_hash,
            native_children_closed=all(child.returncode is not None for child in children))
        (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        (stage/'receipt.json').chmod(0o600)
        print(json.dumps(receipt),flush=True)


if __name__=='__main__':
    asyncio.run(run(Path(sys.argv[1]).absolute(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()))
