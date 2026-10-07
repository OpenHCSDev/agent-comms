"""Exercise display-scope changes in the original private, mounted Channels App."""
import asyncio
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from time import perf_counter

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.history_views import HistoryViews
from agent_comms.threads import Thread
from agent_comms.wire_record import WireRecord
from runtime_fixture import ToadApp
from test_turn_context_wire import manifest
from toad.widgets.channels_sidebar import ChannelsSidebar


async def main(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    project = output / 'project'
    project.mkdir()
    for key in tuple(os.environ):
        if key.startswith('AGENT_COMMS_'):
            del os.environ[key]
    os.environ.update(AGENT_COMMS_ROOT=str(output / 'wire'),
        TOAD_TEST_ATTEMPT=str(output), XDG_CONFIG_HOME=str(output / 'config'),
        XDG_STATE_HOME=str(output / 'state'), XDG_DATA_HOME=str(output / 'data'),
        XDG_CACHE_HOME=str(output / 'cache'))
    service = Comms(output / 'wire', private_initial_writes=True)
    service.messaging.initialize_private_initial_protocol()
    owners = []
    for number in range(8):
        worktree = project if number == 0 else project / f'agent-{number}'
        worktree.mkdir(exist_ok=True)
        owners.append(service.registry.declare(Thread(
            'project' if number == 0 else f'agent-{number}',
            frozenset({'shared', 'design', 'code', 'review'}), str(worktree))))
    for owner in owners:
        for generation in range(1, 9):
            original = manifest(owner, generation)
            service.bus.log.record_context(replace(original, segments=original.segments * 8))
    for number in range(32):
        service.messaging.send_initial_cohort(owners[number % 8].name, '#shared',
            f'Authored private display fixture {number}')
    app = ToadApp(project_dir=str(project))
    durations, phases = [], []
    async with app.run_test(size=(140, 48)) as pilot:
        await app.selected_session.wait_content_ready()
        sidebar = app.screen.query_one(ChannelsSidebar)
        sidebar.reveal()
        await sidebar.wait_content_ready()
        roster = sidebar.roster
        await roster.observation.sync()
        calls = Counter()
        watched = {
            FieldCodec._decode.__func__.__code__: 'field_decodes',
            WireRecord.public_from_wire.__func__.__code__: 'strict_public_rows',
            HistoryViews.viewer_snapshot.__code__: 'viewer_snapshots',
        }
        monitor = sys.monitoring
        monitor.use_tool_id(monitor.PROFILER_ID, 'private-display-scopes')
        monitor.register_callback(monitor.PROFILER_ID, monitor.events.PY_START,
            lambda code, offset: calls.update((watched[code],)))
        for code in watched:
            monitor.set_local_events(monitor.PROFILER_ID, code, monitor.events.PY_START)
        try:
            for enabled in (True, False, True, False):
                before = Counter(calls)
                service.channels.set_channel_any_mode('#shared', enabled)
                start = perf_counter()
                await roster.observation.sync()
                await pilot.pause()
                durations.append(perf_counter() - start)
                snapshot = roster.projection.snapshot.wire
                assert snapshot.channel_unread['#shared'] == 32
                assert next(view.channel.any_mode for view in snapshot.channels
                            if view.channel.name == '#shared') is enabled
                phases.append(dict(calls - before))
            assert app._exception is None
        finally:
            for code in watched:
                monitor.set_local_events(monitor.PROFILER_ID, code, 0)
            monitor.free_tool_id(monitor.PROFILER_ID)
    result = dict(declared_threads=8, messages=32, silent_context_rows=64,
        segments_per_context=8, scope_changes=4, mounted_unread=32,
        update_and_paint_seconds=durations, calls=phases,
        strength='private source headless Channels App; no provider or terminal latency claim')
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1])))
