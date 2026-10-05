"""Configured saved fork: ordinary peer publication during native summary, then new input.

The SDK/router, journal, native child, writer and private wire are production.
The notification receiver publishes a real private peer message; it does not
replace state, protocol, source checks or provider responses. No public input.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import sys
import subprocess
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from publish_retained_summary import InstalledSource, ReviewedArtifact

from acp.agent.router import build_agent_router
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CompactRequest, CompactionChangedUpdate, decode_updates, encode_request
from agent_comms.agent_events import CompactionSummaryProgress
from agent_comms.child_process import ProcessIdentity
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.comms import Comms, wire
from agent_comms.coordinator import Coordination
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import ManualCommittedSummary
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_package import verify_native_package
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.threads import Thread


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unchanged_launch(environment):
    """Ordinary configured launch when no private observation is requested."""


def ordinary_source():
    public = wire()
    snapshot = public.registry.snapshot()
    original = snapshot.require_active('openhcs-architecture-memory')
    return original, RetainedOwnerLaunch.capture(original, snapshot)


@asynccontextmanager
async def configured_saved_agent(stage, package, source_file, receiver, receipt, *,
                                 core_source: InstalledSource,
                                 capture_source=ordinary_source, observe_launch=unchanged_launch,
                                 continuation=None,
                                 core_artifacts: tuple[ReviewedArtifact, ...] = ()):
    """Acquire one configured saved fork and close its original child on every exit."""
    import agent_comms
    installed = Path(agent_comms.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[1]
    assert installed.is_relative_to(Path(sys.prefix))
    direct = json.loads(distribution('agent-comms').read_text('direct_url.json'))
    revision = core_source.require_package('agent_comms', installed, direct, core_artifacts)
    # The installed Core source and the current private fixture have different
    # release identities. Verify the former against its original Git declaration,
    # never an overlay or a mutable current-checkout approximation.
    names = subprocess.check_output(
        ['git','ls-tree','-r','--name-only',revision,'src/agent_comms'],cwd=checkout,text=True).splitlines()
    source_hashes = {}
    for name in names:
        path=installed/Path(name).relative_to('src/agent_comms')
        original_bytes=subprocess.check_output(['git','show',f'{revision}:{name}'],cwd=checkout)
        assert path.read_bytes()==original_bytes, f'Installed release source differs: {name}'
        source_hashes[name]=hashlib.sha256(original_bytes).hexdigest()
    receipt.update(installed_core_revision=revision, installed_code_assets=len(source_hashes),
        installed_source_digest=hashlib.sha256(json.dumps(source_hashes,sort_keys=True).encode()).hexdigest(),
        private_fixture_revision=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=checkout,text=True).strip())
    if continuation is None:
        stage.mkdir(mode=0o700, exist_ok=False)
    started = time.monotonic()
    original, launch = capture_source()
    originals = {path: digest(path) for path in
                 (source_file, Path(str(source_file) + ".input-proof")) if path.exists()}
    original_hash = originals[source_file]
    verify_native_package(package)
    service = Comms(stage/'wire')
    journal = CompactionJournal(service.root/'compaction-commits.sqlite3')
    if continuation is None:
        fork = await journal.private_inputs.fork(ForkSessionRequest(
            str(package), str(source_file), original.worktree, str(stage/'forks')),
            cwd=Path(original.worktree), env=dict(launch.environment))
        root_id = service.messaging.initialize_private_initial_protocol()
    else:
        fork = continuation.resume_fork(journal, InputDispositions(service.root/InputDispositions.filename))
        with service.bus.log.certified_read() as source:
            root_id = source.witness.root_id
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
        if continuation is None:
            service.registry.declare(Thread(name, frozenset({'source529'}), original.worktree,
                parent=original.name, process_identity=identity,
                session_file=fork.session_file if name=='source529' else None,
                model=original.model, thinking_level=original.thinking_level))
        else:
            prior = service.registry.require(name)
            prior.require_idle()
            assert not prior.process_alive
            assert (prior.model, prior.thinking_level, prior.worktree) == (
                original.model, original.thinking_level, original.worktree)
            service.registry.register(replace(prior, process_identity=identity), new_owner=True)
    owner = service.registry.require('source529')
    # In-process fixture owners use the same participant store registration as
    # OwnerLifecycle launch. Registry presence alone is not inbox membership.
    with Coordination(str(service.root / 'coordination.sqlite3')) as store:
        for name in ('source529', 'peer529'):
            thread = service.registry.require(name)
            store.participants.register(stable_thread_lookup(thread.created_at),
                                        thread.name, thread.name, committed=True)
    environment.update(owner.native_environment(service.root, service.registry.snapshot(), owner.worktree))
    observe_launch(environment)
    # Borrow the configured process environment for this resource only.
    # A nested arm returns the parent root/identity after joined shutdown.
    with patch.dict(os.environ, environment, clear=True):
        agent = CommsAgent(service, agent_bin=str(binary), agent_args=list(launch.arguments or ()),
            runtime_enabled=True, auto_wake=False, private_nk_native_package=package,
            private_nk_wire_root_id=root_id)
        agent.on_connect(receiver)
        try:
            await agent.sessions.bind_owned(owner, owner.name)
            receipt.update(model=original.model, thinking=ThinkingLevel.optional_name(original.thinking_level),
                source_bytes=source_file.stat().st_size, original_sha256=original_hash,
                fork_bytes=Path(fork.session_file).stat().st_size)
            yield agent, owner, fork
        except BaseException as error:
            receipt['error']={'type':type(error).__name__,'detail':str(error)}
            raise
        finally:
            children = [backend.custody.child.proc for backend in agent.turns.persistent_backends.values() if backend.available]
            await agent.shutdown()
            receipt.update(elapsed_seconds=time.monotonic()-started,
                original_source_unchanged=all(digest(path)==expected for path,expected in originals.items()),
                native_children_closed=all(child.returncode is not None for child in children))
            result = stage/('receipt.json' if continuation is None else
                           f'continuation-{len(continuation.rounds)}-receipt.json')
            result.write_text(json.dumps(receipt,indent=2)+'\n')
            result.chmod(0o600)
            print(json.dumps(receipt),flush=True)


async def run(stage, package, source_file, *, capture_source=ordinary_source,
              observe_launch=unchanged_launch,
              probe_marker='SOURCE529_DISTINCT_AFTER_COMMIT',
              core_source: InstalledSource,
              core_artifacts: tuple[ReviewedArtifact, ...] = ()):
    receipt = {'complete': False, 'public_inputs': 0, 'input_replays': 0,
               'installed_UI': False, 'acceptance_scope': 'configured SDK/ACP/native saved-source compaction and distinct input'}
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
    async with configured_saved_agent(stage,package,source_file,Receiver(),receipt,
            capture_source=capture_source,observe_launch=observe_launch,
            core_source=core_source,core_artifacts=core_artifacts) as (agent,owner,fork):
        service=agent._comms
        router = build_agent_router(agent)
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
        receipt.update(manual_commit=operation.commit_id,
            peer_messages_during_summary=len(peer_publications), original_inputs_before_new_prompt=0)
        text_chunks.clear()
        marker=probe_marker
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


if __name__=='__main__':
    from publish_retained_summary import InstalledSource

    source, artifacts, arguments = InstalledSource.command_arguments(sys.argv[1:])
    stage, package, original = map(Path, arguments)
    asyncio.run(run(stage.absolute(), package.resolve(), original.resolve(),
                    core_source=source, core_artifacts=artifacts))
