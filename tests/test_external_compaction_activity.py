import os
import time
from types import SimpleNamespace

from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
from agent_comms.cli_bridges.runtime_updates import ThreadOutput
from agent_comms.comms import client_wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry import activity
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_status import PausedBridgeThreadStatus
from agent_comms.registry.threads import ExternalThread


def test_connected_compaction_stays_current_until_source_ends_or_reader_pauses(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(time=lambda: clock[0]))
    with client_wire(tmp_path) as comms:
        comms.registry.declare(ExternalThread('cli', frozenset(), Checkout(tmp_path)))
        output = ThreadOutput(comms.transcripts.external('cli'), comms.agents)
        stream = ItemStream('session')

        def receive(method, **params):
            update = stream.receive(RpcNotification(
                method=method, params={'threadId': 'session', **params}))
            assert update is not None
            output.publish_batch([update])

        def observed():
            return comms.views.observed_activity(comms.views.revision(), ('cli',))['cli']

        with comms.registry.external_process('cli', ProcessIdentity.capture(os.getpid())):
            receive('item/started', turnId='turn', startedAtMs=int(clock[0] * 1000),
                    item={'type': 'contextCompaction', 'id': 'compact'})
            revision = comms.views.revision()
            clock[0] += 181
            assert comms.views.revision() == revision
            current = observed()
            assert current.state is ActivityState.WORKING
            assert current.detail == 'Compacting context'
            assert current.presentation('cli').summary == 'Working · Compacting context'

            receive('turn/completed', turn={'id': 'turn'})
            assert observed().state is ActivityState.IDLE

            receive('item/started', turnId='next', startedAtMs=int(clock[0] * 1000),
                    item={'type': 'contextCompaction', 'id': 'next-compact'})
            comms.registry.register(comms.registry.require('cli'), PausedBridgeThreadStatus())
            clock[0] += 181
            assert observed().state is ActivityState.IDLE
