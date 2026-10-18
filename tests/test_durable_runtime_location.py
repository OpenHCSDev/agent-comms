"""P117 location behavior over a real, paused durable store; no provider inputs."""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.agent_backend import AgentBackend, BackendDeclarationError, RuntimeLocation
from agent_comms.field_codec import FieldCodec
from agent_comms.native_child import PiChild
from agent_comms.pi_durable_backend import DurableDatabase, PiDurableBackend, Inspect
from agent_comms.pi_native_backend import PiNativeBackend
from agent_comms.threads import Thread
from agent_comms.turn_runner import TurnRunner
from agent_comms.unstarted_runtime import UnstartedRuntime


@pytest.mark.parametrize('saved', [False, True])
def test_durable_thread_roundtrip_and_worker_selection(tmp_path, saved, *, thread_registration):
    location = DurableDatabase(str(tmp_path / 'session.sqlite3'))
    thread = thread_registration.new_thread('durable', frozenset(), str(tmp_path), backend=PiDurableBackend, session_file=location.read_key if saved else None)
    restored = FieldCodec.decode(Thread, FieldCodec.encode(thread))
    assert restored.runtime == (location if saved else UnstartedRuntime(PiDurableBackend))
    assert restored.runtime.backend is PiDurableBackend
    assert AgentBackend.for_new_threads() is PiNativeBackend
    assert not DurableDatabase.__abstractmethods__
    class Runner:
        persistent_backends = {}
        class comms:
            root = tmp_path
    runner = Runner()
    backend = TurnRunner.backend_for(runner, 'durable', restored)
    assert isinstance(backend, PiDurableBackend)
    assert TurnRunner.backend_for(runner, 'durable', restored) is backend


def test_location_methods_and_missing_history(tmp_path):
    location = DurableDatabase(str(tmp_path / 'missing.sqlite3'))
    assert isinstance(location, RuntimeLocation)
    assert location.arguments() == {'session_file': location.path}
    assert location.read_keys() == (location.path,)
    assert location.resumable is True
    assert location.model_choice is None
    assert location.select_session(tmp_path, None).location == location
    with pytest.raises(ValueError, match='forking'):
        location.require_forkable()
    source = location.history_source(tmp_path, None)
    with source.reading() as reader:
        assert reader.cut().offset == 0
        assert tuple(reader.reverse(0)) == ()
        assert reader.current_draft().external_row() is None
    assert source.turns(5) == ()
    assert not Path(location.path).exists()
    files, = location.storage_files()
    assert files.schema_paths == (Path(location.path),)
    assert tuple(path.name for path in files.members) == (
        'missing.sqlite3', 'missing.sqlite3-wal', 'missing.sqlite3-shm',
        'missing.sqlite3-journal', 'missing.sqlite3.lock')
    assert isinstance(UnstartedRuntime(PiDurableBackend).registered_location(tmp_path, tmp_path), DurableDatabase)


SEED = """
import { BACKGROUND_CONTEXT as context } from '@earendil-works/chord/context';
import { Harness, createRegistry } from '@earendil-works/pi-durable';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { InMemoryCredentialStore } from '@earendil-works/pi-ai';
import { builtinModels } from '@earendil-works/pi-ai/providers/all';
const harness = await Harness.open(await openNodeSqliteStorage(process.argv[1]), {
  registry: createRegistry(), models: builtinModels({credentials: new InMemoryCredentialStore()}),
}, context);
try {
  const root = await harness.root(context);
  await harness.commit(async tx => {
    for (let index = 0; index < 260; index++) {
      const role = index % 2 ? 'assistant' : 'user';
      await tx.appendEntry(root.id, {kind: `pi.${role}`, data: null,
        model: [{role, content: [{type: 'text', text: `entry ${index}`}]} ]});
    }
  }, context);
} finally { await harness.close(context); }
"""


def test_real_durable_history_pages_while_writer_owns_database(tmp_path, thread_registration, monkeypatch):
    location = DurableDatabase(str(tmp_path / 'history.sqlite3'))
    start = PiChild.start_command.__func__
    history_starts = []

    async def counted_start(cls, *args, **kwargs):
        if cls is PiChild:
            history_starts.append(args)
        return await start(cls, *args, **kwargs)

    monkeypatch.setattr(PiChild, 'start_command', classmethod(counted_start))
    subprocess.run(['node', '--input-type=module', '-e', SEED, location.path],
                   cwd=Path(__file__).parents[1] / 'src/agent_comms', check=True,
                   capture_output=True, timeout=20)
    async def journey():
        backend = PiDurableBackend(tmp_path)
        thread = thread_registration.new_thread('history', frozenset(), str(tmp_path), backend=PiDurableBackend,
                        session_file=location.path)
        host = await backend.open(thread, environment=dict(os.environ))
        try:
            interrupted = await host.request(Inspect())
            # The observer reads from an active event loop; this must not nest a Runner.
            # Open/Resume added six structural entries beside the 260 seeded text entries.
            with location.history_source(tmp_path, thread.incarnation).reading() as reader:
                assert reader.cut().offset == 266
            def read():
                source = location.history_source(tmp_path, thread.incarnation)
                with source.reading() as reader:
                    cut = reader.cut()
                    records = tuple(reader.reverse(cut.offset))
                    assert len(records) == 260
                    assert [r.events[0].text for r in records[:4]] == ['entry 259', 'entry 258', 'entry 257', 'entry 256']
                    forward = tuple(reader.forward(0, cut.offset))
                    assert forward == tuple(reversed(records))
                    with reader.projection(None, None) as project:
                        assert tuple(project(iter(forward), max_records=10, max_bytes=1024))
                turns = source.turns(1)
                assert len(turns) == 1
                assert [event.text for event in turns[0].events] == ['entry 258', 'entry 259']
                assert location.history_source(tmp_path, thread.incarnation) is source
                with source.reading() as again:
                    assert len(tuple(again.reverse(again.cut().offset))) == 260
            await asyncio.to_thread(read)
            assert len(history_starts) == 1
            assert await host.request(Inspect()) == interrupted
        finally:
            await host.shutdown()
            location.transcript.worker.close()
    asyncio.run(journey())
