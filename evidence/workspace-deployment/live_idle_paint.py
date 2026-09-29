"""Check actual retained text painted in the live conversation viewport."""
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_comms.comms import wire
from agent_comms.transcript_events import AgentTextTranscript, UserTranscript
from toad.app import ToadApp
from toad.goal_display import GoalUnavailable
from toad.widgets.transcript_history import TranscriptHistory


def words(text):
    return tuple(re.findall(r"[a-z0-9]+", text.lower()))


def phrases(text):
    tokens = words(text)
    return {tokens[i:i+5] for i in range(len(tokens)-4)}


async def main():
    owner = wire().registry.require(os.environ['CHECK_OWNER'])
    output = Path(__file__).resolve().parent
    agent = {'identity': 'retained-paint-check', 'name': 'Agent Comms',
             'short_name': 'agent', 'protocol': 'acp', 'type': 'coding',
             'run_command': {'*': f'{sys.executable} -m agent_comms.acp'}, 'actions': {}}
    with TemporaryDirectory(prefix='retained-paint-', dir=output.parents[1]/'.artifacts') as scratch:
        for key, leaf in [('XDG_CONFIG_HOME','config'),('XDG_STATE_HOME','state'),('XDG_DATA_HOME','data')]:
            os.environ[key] = str(Path(scratch)/leaf)
        app = ToadApp(agent_data=agent, project_dir=owner.worktree, agent_session_id=owner.name)
        async with app.run_test(size=(120,42)) as pilot:
            try:
                async with asyncio.timeout(25):
                    await app.screen.wait_content_ready()
                    while True:
                        await asyncio.sleep(.1)
                        view = app.screen.conversation
                        if view.agent_ready and view.query(TranscriptHistory):
                            break
                await asyncio.gather(view.goal_observation.refresh(), view.delivery_observation.refresh())
                assert not isinstance(view.goal_display, GoalUnavailable)
                assert not view.input_delivery_error
                await pilot.pause(1)
                initial_status=view.native_history_status
                async with asyncio.timeout(15):
                    while view.native_history_status == 'unavailable':
                        await asyncio.sleep(.1)
                history = view.query_one(TranscriptHistory)
                region = view.window.content_region
                strips = app.screen._compositor.render_strips()
                painted = '\n'.join(strip.crop(region.x, region.right).text for strip in strips[region.y:region.bottom])
                sources = set().union(*(phrases(event.text) for page in history.pages
                    for event in page.page.events if isinstance(event,(AgentTextTranscript,UserTranscript))))
                matched = len(sources & phrases(painted))
                result = {'owner':owner.name, 'ready':view.agent_ready,
                          'goal_display':type(view.goal_display).__name__,
                          'delivery_error':view.input_delivery_error,
                          'initial_cursor_status':initial_status,
                          'final_cursor_status':view.native_history_status,
                          'history_height':history.region.height,
                          'viewport_height':region.height,
                          'history_overlaps_viewport':history.region.overlaps(region),
                          'matching_saved_five_word_phrases':matched,
                          'painted_nonspace_characters':len(re.sub(r'\s','',painted)),
                          'prompts_sent':0, 'exception':repr(app._exception)}
                output.joinpath(owner.name+'-paint.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result),flush=True)
                assert history.region.height > 0, 'Saved history collapsed to zero height'
                assert history.region.overlaps(region), 'Saved history outside visible conversation'
                assert matched, 'No saved message text painted in conversation viewport'
                assert app._exception is None
            finally:
                app.save_screenshot(owner.name+'-paint.svg',path=str(output))


if __name__ == '__main__':
    asyncio.run(main())
