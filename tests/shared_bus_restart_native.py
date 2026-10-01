"""Actual private retained restart and concurrent automatic inbox drains.

Only the localhost provider is controlled. Worker, registry, bus, selected
admission, native process, source proof and response publication remain real.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import subprocess
import threading

from compaction_loopback import LoopbackProvider
from agent_comms.comms import Comms
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import decode_updates
from agent_comms.messages import Message, MessageType
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest
from agent_comms.threads import Thread
from agent_comms.thread_identity import ThreadRole
from agent_comms.bus_publication import HumanOrigin
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_input_record import TriageNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from contextlib import closing
import sqlite3


async def attach(service, name):
    owner = service.registry.require(name)
    async with asyncio.timeout(30):
        while not socket_path(service.root, owner.pid).exists():
            assert owner.process_alive
            await asyncio.sleep(.03)
        reader, writer = await asyncio.open_unix_connection(
            socket_path(service.root, owner.pid), limit=8 * 1024 * 1024)
        try:
            writer.write((json.dumps(SubscribeRuntimeRequest(thread=name).to_wire())+'\n').encode())
            await writer.drain()
            while line := await reader.readline():
                packet = json.loads(line)
                assert 'error' not in packet, packet
                if 'ready' in packet:
                    return
            raise AssertionError('Worker closed before complete attachment')
        finally:
            writer.close()
            await writer.wait_closed()


class PublishOriginalsAtStoppedBatch(StoppedOwnerInstallation):
    def __init__(self, service, names, *, collective=False, after_attachment=False):
        self.service, self.names, self.collective = service, names, collective
        self.after_attachment = after_attachment
        self.originals = []

    def require_selection(self, snapshot, owners):
        assert {owner.name for owner in owners} == set(self.names)

    def after_stopped(self, lifecycle):
        assert all(not lifecycle.registry.require(name).process_alive for name in self.names)
        if self.after_attachment:
            return
        self.publish()

    def publish(self):
        # Batch already owns the wire lock. The canonical publication owner
        # acquires bus/registry custody; Messaging's outer wire scope would nest.
        if self.collective:
            sender = self.service.registry.require('human')
            self.originals = [self.service.bus.publisher.publish_ordinary(
                Message(sender.name, '#team', 'New collective channel original: test',MessageType.INFO),
                _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))]
            return
        self.originals = [self.service.bus.publisher.publish_initial_cohort(Message(
            'fixture-sender', name, f'Unique new inbox input for {name}: reply ONCE.', MessageType.INFO))
            for name in self.names]

    def publish_cancel_and_pending(self):
        from agent_comms.store_files import _store_lock

        # Both original publications precede admission, under the same existing
        # wire custody. No observer can reserve the first between the two.
        with _store_lock(self.service._wire_lock_path):
            sender = self.service.registry.require('human')
            self.originals = [self.service.bus.publisher.publish_ordinary(
                Message(sender.name, '#team', body, MessageType.INFO),
                _human_origin=HumanOrigin(sender.name, sender.created_at, sender.worktree))
                for body in ('Cancel THIS original before grant.',
                             'Independent saved channel original: reply ONCE.')]


async def run(arguments):
    if arguments.cancel_before_grant:
        assert arguments.collective and arguments.contention and arguments.owners == 1
    stage = arguments.stage.absolute()
    assert stage.is_relative_to('/home/ts/wt')
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    assert len(str(stage/'wire'/'native-sessions'/('0'*32)/'s')) < 108
    project, config = stage/'project', stage/'config'
    project.mkdir(); config.mkdir(mode=0o700)
    class ChannelReplyProvider(LoopbackProvider):
        def response_chunks(self):
            # Only the provider is controlled; select its bounded triage or
            # ordinary answer from the actual request envelope.
            messages = self.requests[-1]['messages']
            latest = json.dumps(next(message for message in reversed(messages)
                                     if message['role']=='user'))
            text = ('{"decision":"FULL"}' if 'bounded triage' in latest
                    else 'Unique controlled native reply.')
            yield {'content':text}, None
            yield {}, 'stop'

    provider_type = ChannelReplyProvider if arguments.cancel_before_grant else LoopbackProvider
    provider = provider_type(status=200, text=(
        '{"decision":"IGNORE"}' if arguments.collective else 'Unique controlled native reply.'))
    provider.response_gate = asyncio.Event()
    connections = set()

    async def serve(reader, writer):
        task = asyncio.current_task(); connections.add(task)
        try:
            await provider.handle(reader, writer)
        finally:
            connections.remove(task)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    (config/'models.json').write_text(json.dumps({'providers':{'restart-local':{
        'baseUrl':f'http://127.0.0.1:{port}/v1','api':'openai-completions',
        'apiKey':'local-only','models':[{'id':'fixture','name':'Private restart fixture',
            'contextWindow':2000000,'maxTokens':8192}]}}}))
    (config/'auth.json').write_text(json.dumps({'restart-local': {
        'type': 'api_key', 'key': 'local-only'}}))
    (config/'settings.json').write_text(json.dumps({'compaction':{'enabled':False},
        'retry':{'enabled':False,'maxRetries':0,'provider':{'maxRetries':0}}}))
    service = Comms(stage/'wire', private_initial_writes=True)
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, arguments.package)
    env = dict(os.environ, AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(arguments.package),
        AGENT_COMMS_NATIVE_CONFIG_DIR=str(config), PI_CODING_AGENT_DIR=str(config),
        AGENT_COMMS_AGENT_MODELS='restart-local/fixture')
    for key in ('PYTHONPATH','PI_PROMPT','PI_TASK','PI_PARENT_ID','PI_AGENT_ID',
                'AGENT_COMMS_STARTUP_INPUT_KEY'):
        env.pop(key, None)
    os.environ.clear();os.environ.update(env)
    names = [f'restart-worker-{index}' for index in range(arguments.owners)]
    service.registry.declare(Thread('fixture-sender',frozenset(),str(project)))
    service.registry.declare(Thread('history-recipient',frozenset(),str(project)))
    if arguments.collective:
        service.registry.declare(Thread('human',frozenset(),str(project),role=ThreadRole.USER))
    for index in range(arguments.registry_size):
        service.registry.declare(Thread(f'unrelated-{index}',frozenset(),str(project)))
    for name in names:
        service.registry.declare(Thread(name,frozenset({'team'} if arguments.collective else ()),str(project),
            model='restart-local/fixture',thinking_level='off',
            task='Provider-free isolated restart acceptance; reply to each original once'))
    for index in range(arguments.history):
        service.messaging.send_initial_cohort('fixture-sender','history-recipient',
            f'Retained unrelated canonical source {index}: ' + 'history '*250)
    started = time.perf_counter()
    failure = None
    packets = []
    class Observation:
        async def session_update(self, **kwargs):
            packets.append(kwargs)
    attachment = CommsClient(service, runtime_enabled=True,
        private_nk_native_package=arguments.package, private_nk_wire_root_id=root_id)
    attachment.on_connect(Observation())
    contention_release = threading.Event()
    contention_held = threading.Event()
    contention_observations = []
    cancel_observation = {}

    def hold_original_reader():
        deadline = time.monotonic()+30
        while time.monotonic()<deadline and not contention_release.is_set():
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                reserved = NativeRuntimeInput.select(db)
            if reserved:
                # Observe the original reservation, then acquire a real reader.
                with service.bus.log.certified_read():
                    begun = time.monotonic()
                    contention_held.set()
                    contention_release.wait(arguments.contention_seconds)
                    contention_observations.append({'held_seconds':time.monotonic()-begun,
                        'original_reserved_input':reserved[0].input_id})
                return
            contention_release.wait(.01)

    contender = None
    try:
        for name in names:
            await asyncio.to_thread(service.owners.start,name,
                agent_args=['--offline','--no-extensions','--no-skills',
                            '--no-context-files','--no-prompt-templates','--no-tools'])
            await attach(service,name)
        assert provider.posts == 0
        originals = [service.registry.require(name) for name in names]
        cutover = PublishOriginalsAtStoppedBatch(service,names,collective=arguments.collective,
            after_attachment=arguments.cancel_before_grant)
        if arguments.contention:
            contender = threading.Thread(target=hold_original_reader,name='original-wire-reader')
            contender.start()
        result = await asyncio.to_thread(service.owners.restart_owners,names,cutover=cutover)
        assert len(result)==len(names)
        assert all(not owner.process_alive for owner in originals)
        if not arguments.contention or arguments.cancel_before_grant:
            await asyncio.gather(*(attachment.load_session(
                cwd=str(project), session_id=name) for name in names))
        if arguments.cancel_before_grant:
            await asyncio.to_thread(cutover.publish_cancel_and_pending)
            async with asyncio.timeout(30):
                while not contention_held.is_set():
                    await asyncio.sleep(.03)
                # Verify the real dedicated writer is waiting at admission;
                # readiness/lease alone would not prove the affected seam.
                while True:
                    owner = service.registry.require(names[0])
                    stack = await asyncio.to_thread(subprocess.run,
                        ['sudo','-n','/home/ts/.local/bin/py-spy','dump','--pid',
                         str(owner.pid),'--nonblocking'],capture_output=True,text=True,timeout=5)
                    if '_enter_admission' in stack.stdout:
                        (stage/'writer-wait-before-cancel.txt').write_text(stack.stdout)
                        break
                    await asyncio.sleep(.05)
            assert provider.posts == 0
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                cancelled_input, = NativeRuntimeInput.select(db)
            await asyncio.wait_for(attachment.cancel(names[0]), 15)
            assert provider.posts == 0
            cancel_observation = {'cancelled_input':cancelled_input.input_id,
                'localhost_posts_at_joined_cancel':0,'writer_wait_observed':True}
            contention_release.set()
            provider.response_gate.set()
        diagnosed = False
        concurrent_native = False
        async with asyncio.timeout(45):
            while True:
                diagnostics = list((service.root/'diagnostics').glob('*.json'))
                drains = [activity.diagnostic for activity in service.agents.all_activity().values()
                          if activity.diagnostic is not None]
                assert not drains, drains
                if diagnostics:
                    observed = json.loads(diagnostics[0].read_text())
                    raise AssertionError(observed.get('source_error',observed.get('reason')))
                expected_posts = 2 if arguments.cancel_before_grant else len(names)
                if provider.posts == expected_posts:
                    if not provider.response_gate.is_set():
                        assert all(service.registry.require(name).active_turn is not None
                                   for name in names)
                        concurrent_native = True
                        provider.response_gate.set()
                    replies = service.bus.inbox('fixture-sender')
                    completed = len(replies)==len(names)
                    if arguments.collective:
                        with closing(sqlite3.connect(
                            (service.root/'coordination.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                            rows = WakeAssignment.select(db,where='wire_seq=?',
                                parameters=(cutover.originals[0].seq,))
                            if arguments.cancel_before_grant:
                                rows = WakeAssignment.select(db,where='wire_seq=?',
                                    parameters=(cutover.originals[1].seq,))
                                completed = len(rows)==1 and rows[0].lifecycle.declared_name=='completed'
                            else:
                                completed = len(rows)==len(names) and all(
                                    row.lifecycle.declared_name=='ignored' for row in rows)
                    if completed and all(service.registry.require(name).active_turn is None
                                         for name in names):
                        break
                if not diagnosed and time.perf_counter()-started > 25:
                    diagnosed = True
                    for name in names:
                        owner = service.registry.require(name)
                        stack = await asyncio.to_thread(subprocess.run,
                            ['sudo','-n','/home/ts/.local/bin/py-spy','dump','--pid',
                             str(owner.pid),'--nonblocking'],capture_output=True,text=True,timeout=5)
                        (stage/(name+'-stack.txt')).write_text(stack.stdout+stack.stderr)
                await asyncio.sleep(.05)
        if arguments.cancel_before_grant:
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                retained = NativeRuntimeInput.one(db,input_id=cancelled_input.input_id)
                cancelled_assignment = WakeAssignment.one(db,assignment_id=retained.assignment_id)
                assert retained == cancelled_input
                assert cancelled_assignment.lifecycle.declared_name=='deferred'
            replies = service.bus.channel_history('#team')
            assert any(reply.sender==names[0] and reply.body=='Unique controlled native reply.'
                       for reply in replies)
            assert provider.posts == 2
        else:
            assert concurrent_native
            assert provider.posts==len(names),(provider.posts,len(names))
        if arguments.collective and not arguments.cancel_before_grant:
            assert replies == []
            with closing(sqlite3.connect((service.root/'coordination.sqlite3').as_uri()+
                                        '?mode=ro',uri=True)) as db:
                inputs = NativeRuntimeInput.select(db)
                assert len(inputs)==len(names)
                assert all(row.reference_stage is TriageNativeExecution and
                           row.verdict is IgnoreSelectedTriage for row in inputs)
        elif not arguments.cancel_before_grant:
            assert {reply.sender for reply in replies}==set(names)
        facts = [fact for packet in packets
                 for fact in decode_updates(packet['update'].get('_meta'))]
        if arguments.contention and not arguments.cancel_before_grant:
            assert contention_held.is_set() and contention_observations
        else:
            assert {packet['session_id'] for packet in packets} == set(names)
            assert facts, 'No authoritative ACP notifications observed'
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        contention_release.set()
        if contender is not None:
            await asyncio.to_thread(contender.join,35)
            assert not contender.is_alive()
        await attachment.shutdown()
        for name in reversed(names):
            await asyncio.to_thread(service.owners.stop,name)
        server.close();await server.wait_closed()
        for task in tuple(connections):task.cancel()
        await asyncio.gather(*connections,return_exceptions=True)
        native_inputs=[]
        for path in (service.root/'native-sessions').rglob('*.jsonl'):
            for row in map(json.loads,path.read_text().splitlines()):
                if row.get('type')=='message' and row['message'].get('role')=='user':
                    native_inputs.append({'session':str(path.relative_to(stage)),
                                          'entry_id':row['id']})
        if failure is None:
            assert len(native_inputs) == (2 if arguments.cancel_before_grant else len(names)), native_inputs
        receipt={'elapsed_seconds':time.perf_counter()-started,'owners':len(names),
            'history_rows':arguments.history,'bus_bytes':(service.root/'bus.jsonl').stat().st_size,
            'localhost_provider_posts':provider.posts,'native_originals':native_inputs,
            'failure':failure,'all_owned_workers_retired':all(
                not service.registry.require(name).process_alive for name in names),
            'public_mutations':0,'paid_provider_calls':0,'original_replays':0,
            'installed_interpreter':sys.executable,
            'acp_notification_count':len(packets),'contention':contention_observations,
            'cancellation':cancel_observation,
            'all_native_turns_active_before_provider_release':concurrent_native if failure is None else False,
            'acp_fact_counts':{kind:sum(type(fact).__name__ == kind for fact in facts)
                for kind in sorted({type(fact).__name__ for fact in facts})} if failure is None else {}}
        (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--owners',type=int,default=3)
    parser.add_argument('--history',type=int,default=259)
    parser.add_argument('--collective',action='store_true')
    parser.add_argument('--registry-size',type=int,default=0)
    parser.add_argument('--contention',action='store_true')
    parser.add_argument('--contention-seconds',type=float,default=12)
    parser.add_argument('--cancel-before-grant',action='store_true')
    asyncio.run(run(parser.parse_args()))
