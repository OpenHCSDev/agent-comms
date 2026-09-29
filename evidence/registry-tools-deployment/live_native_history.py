"""Actual installed native session display, no prompt and no live owner stop."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from agent_comms.comms import wire
from toad.app import ToadApp
from toad.widgets.transcript_history import TranscriptHistory


async def main():
    owner = wire().registry.require('pr95-selected-pi-summary-owner')
    directory = Path(__file__).resolve().parent
    agent = {
        'identity': 'live-history-diagnostic', 'name': 'Agent Comms',
        'short_name': 'agent', 'protocol': 'acp', 'type': 'coding',
        'run_command': {'*': f'{sys.executable} -m agent_comms.acp'},
        'actions': {},
    }
    with tempfile.TemporaryDirectory(dir=directory.parents[1] / '.artifacts',
                                     prefix='live-history-view-') as scratch:
        for key, leaf in (('XDG_CONFIG_HOME','config'), ('XDG_STATE_HOME','state'),
                          ('XDG_DATA_HOME','data')):
            os.environ[key] = str(Path(scratch) / leaf)
        app = ToadApp(agent_data=agent, project_dir=owner.worktree,
                      agent_session_id=owner.name)
        async with app.run_test(size=(120, 42)) as pilot:
            try:
                async with asyncio.timeout(45):
                    while True:
                        await asyncio.sleep(.1)
                        view = app.screen.conversation
                        if view.agent_ready and view.query(TranscriptHistory):
                            break
                await pilot.pause(.5)
                result = {'name': owner.name, 'ready': view.agent_ready,
                          'history_widgets': len(view.query(TranscriptHistory)),
                          'contents_children': len(view.contents.children),
                          'loading_overlay': view.has_class('-initial-loading'),
                          'prompts_sent': 0, 'exception': repr(app._exception)}
                assert app._exception is None
                assert not view.has_class('-initial-loading')
                directory.joinpath('live-native-history.json').write_text(json.dumps(result, indent=2)+'\n')
                print(json.dumps(result), flush=True)
            finally:
                app.save_screenshot('live-native-history.svg', path=str(directory))
                view = app.screen.conversation
                print('STATE', view.agent_ready, len(view.query(TranscriptHistory)), flush=True)
                if view.agent is not None:
                    task = view.agent.process.session_task
                    print('SESSION_TASK', task, flush=True)
                    if task is not None and task.done() and not task.cancelled():
                        print('SESSION_ERROR', repr(task.exception()), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
