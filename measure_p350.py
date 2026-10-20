"""Read-only live P350 measurement. Never submits input or starts an owner."""
import asyncio
import cProfile
from pathlib import Path
import pstats
import time

from agent_comms.acp import CommsClient
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import Comms
from agent_comms.session_load import ExistingSessionLoadPlan
from agent_comms.ui_model.observation import ReadTranscript

ROOT = Path('/home/ts/.agent-comms')


async def measure(name):
    comms = Comms(ROOT)
    snapshot = comms.registry.snapshot()
    thread = snapshot.require(name)
    agent = CommsClient(comms, auto_wake=False, runtime_enabled=False)
    started = time.perf_counter()
    first = []

    class Client:
        async def session_update(self, **kwargs):
            arrived = time.perf_counter()
            update = kwargs['update']
            metadata = update.get('_meta', {}) if isinstance(update, dict) else (update.field_meta or {})
            for fact in decode_updates(metadata):
                if isinstance(fact, TranscriptSnapshotUpdate) and not first:
                    first.append(arrived - started)
                    print(name, 'FIRST_REPLAY', first[0], 'seconds',
                          'events', len(fact.page.events), 'outer_session', kwargs['session_id'], flush=True)

    agent.on_connect(Client())
    try:
        plan = ExistingSessionLoadPlan(snapshot.owner_binding(name))
        started = time.perf_counter()
        await agent.load_session(thread.worktree, name, **plan.metadata())
        print(name, 'LOAD_COMPLETE', time.perf_counter() - started,
              'seconds', 'session_bytes', Path(thread.session_file).stat().st_size, flush=True)
    finally:
        await agent.shutdown()


async def main():
    for name in ('unit-directive-delete', 'unit-hygiene'):
        await measure(name)
    print('PROFILE: canonical transcript read for unit-directive-delete', flush=True)
    profile = cProfile.Profile()
    profile.enable()
    result = ReadTranscript(str(ROOT), 'unit-directive-delete').read(Comms(ROOT))
    profile.disable()
    print('PROFILE_EVENTS', len(result.page.events), flush=True)
    pstats.Stats(profile).sort_stats('cumulative').print_stats(25)


if __name__ == '__main__':
    asyncio.run(main())
