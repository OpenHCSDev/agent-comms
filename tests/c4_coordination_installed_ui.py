"""Installed snapshot values, original notification receipts and real sidebar rows."""
import argparse
import asyncio
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import perf_counter

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--toad-tests', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--private-root', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.toad_tests))
from runtime_fixture import ToadApp, wait_channel_roster
from agent_comms.comms import Comms
from agent_comms.presentation import MessageNotification
from agent_comms.private_nk_entrypoint import PrivateNkLaunch, ROOT_ID_ENV, PACKAGE_ENV
from agent_comms.threads import Thread
from toad.widgets.comms_sidebar import ChannelGroup


class InstalledApp(ToadApp):
    CSS_PATH = files('toad').joinpath('toad.tcss')


async def main():
    started = perf_counter()
    root = args.private_root
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ.update(XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                      XDG_DATA_HOME=str(root / 'data'), AGENT_COMMS_ROOT=str(root / 'wire'))
    comms = Comms(root / 'wire', private_initial_writes=True)
    root_id = comms.messaging.initialize_private_initial_protocol()
    launch = PrivateNkLaunch.from_environment(comms.root, {
        ROOT_ID_ENV: root_id, PACKAGE_ENV: os.environ['AC_NATIVE_COPIED_PACKAGE']})
    assert launch is not None
    launch.apply_environment(os.environ)
    owner = root.name
    for name in (owner, 'stopped', 'archived'):
        source = root / (name + '.jsonl')
        source.touch()
        comms.registry.declare(Thread(name, frozenset({'team'}), str(root), session_file=str(source)))
    comms.owners.stop('stopped')
    comms.owners.stop('archived')
    comms.threads.archive('archived')
    message = comms.messaging.send_initial_cohort(owner, '#team', 'Original metadata receipt')
    original_wire = comms.bus.log.path.read_bytes()
    actor = comms.views.coordination_snapshot('stopped')
    human = comms.views.viewer_snapshot(str(root))
    assert {view.thread.name for view in actor.threads} == {owner, 'stopped'}
    assert actor.channel_unread['#team'] == human.channel_unread['#team'] == 1
    assert comms.views.coordination_snapshot('stopped') == actor
    original = comms.views.message_notifications((message,))
    assert original[message.seq, message.message_id]
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)) == original
    page = comms.views.channel_display_page('#team', worktree=str(root))
    comms.views.mark_channel_view_read('#team', worktree=str(root), through=page.newest_seq,
                                      expected_scope=page.display_scope)
    assert comms.views.viewer_snapshot(str(root)).channel_unread['#team'] == 0
    assert comms.views.coordination_snapshot('stopped').channel_unread['#team'] == 1
    checks = ['full original snapshot value equality', 'original bounded reference notification windows',
              'human display acknowledgement leaves actor delivery pending']
    app = InstalledApp(project_dir=str(root))
    async with app.run_test(size=(120, 45)) as pilot:
        await app.selected_session.wait_content_ready()
        sidebar = await wait_channel_roster(app, pilot, '#team')
        group = next(group for group in sidebar.query(ChannelGroup) if group.row.target_name == '#team')
        await sidebar.observation.sync()
        await group.reveal_members()
        await pilot.pause()
        assert set(group._members) == {owner, 'stopped'}
        checks.append('real mounted sidebar consumes default captured cohort')
        for show_stopped, show_archived, expected in (
            (False, False, {owner}), (True, True, {owner, 'stopped', 'archived'})):
            app.settings.sidebar.show_stopped = show_stopped
            app.settings.sidebar.show_archived = show_archived
            await sidebar.observation.sync()
            await group.reveal_members()
            await pilot.pause()
            assert set(group._members) == expected
            snapshot = comms.views.viewer_snapshot(str(root), show_stopped=show_stopped,
                                                  show_archived=show_archived)
            assert set(group._members) == {view.thread.name for view in snapshot.threads}
        checks.append('real sidebar filtered cohorts agree with original registry status')
    await asyncio.get_running_loop().shutdown_default_executor()
    assert comms.bus.log.path.read_bytes() == original_wire
    with sqlite3.connect(comms.root / 'coordination.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM native_runtime_input').fetchone()[0] == 0
    checks.append('whole original App close; source unchanged; no native input')
    args.output.write_text(json.dumps({'state': 'affected installed App complete', 'seconds': perf_counter()-started,
        'checks': checks, 'provider_calls': 0, 'native_inputs': 0, 'physical_or_performance_claim': False,
        'core': str(files('agent_comms')), 'toad': str(files('toad'))}, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
