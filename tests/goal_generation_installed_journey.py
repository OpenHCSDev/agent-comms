"""Saved native history -> model-set goal -> blocked -> visible UI Retry.

Run with the candidate wheel installed, the shipped Toad dependencies, and
PI_COMPACTION_TEST_PACKAGE naming the unchanged immutable native package.
Only the existing loopback provider's responses are controlled. --physical runs
this same journey in an owned st window using actual X keyboard/mouse events.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import fcntl
import inspect
import json
import os
from pathlib import Path
import shlex
import struct
import sys
import termios

from agent_comms.comms import Comms
from agent_comms.goal_management import Goals
from agent_comms.goal_generation import BlockedGeneration, CompletedGeneration
from native_backend_fixture import native_backend_fixture
from textual.pilot import Pilot


class PhysicalPilot(Pilot):
    """Drive the owned terminal, not injected Textual input messages."""

    def __init__(self, app, stage: Path):
        super().__init__(app)
        self.stage = stage
        assert sys.__stdout__.isatty(), 'Physical controls require the owned terminal'
        self.window = os.environ['WINDOWID']

    async def command(self, *args):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, (args, stderr.decode())
        return stdout.decode()

    async def pause(self, delay=.05):
        await asyncio.sleep(delay)

    async def press(self, *keys):
        assert keys == ('enter',), 'This journey uses only the Enter key'
        await self.command('xdotool', 'windowfocus', '--sync', self.window,
                           'key', '--clearmodifiers', 'Return')

    async def type(self, text):
        await self.command('xdotool', 'windowfocus', '--sync', self.window,
                           'type', '--clearmodifiers', '--delay', '1', '--', text)

    async def click(self, widget):
        region = widget.region
        geometry = await self.command('xdotool', 'getwindowgeometry', '--shell', self.window)
        dimensions = dict(line.split('=', 1) for line in geometry.splitlines())
        rows, columns, pixel_width, pixel_height = struct.unpack(
            'HHHH', fcntl.ioctl(sys.__stdout__.fileno(), termios.TIOCGWINSZ, bytes(8)))
        assert (columns, rows) == tuple(self.app.size)
        assert pixel_width and pixel_height, 'st must report its terminal pixel area'
        cell_width, cell_height = pixel_width // columns, pixel_height // rows
        border_x = (int(dimensions['WIDTH']) - pixel_width) // 2
        border_y = (int(dimensions['HEIGHT']) - pixel_height) // 2
        x = border_x + (region.x + region.width // 2) * cell_width + cell_width // 2
        y = border_y + (region.y + region.height // 2) * cell_height + cell_height // 2
        await self.command('xdotool', 'mousemove', '--sync', '--window', self.window,
                           str(x), str(y), 'click', '1')
        (self.stage / 'physical-click.json').write_text(json.dumps({
            'widget_id': widget.id, 'window': self.window, 'x': x, 'y': y,
            'cell_width': cell_width, 'cell_height': cell_height,
            'border_x': border_x, 'border_y': border_y}) + '\n')
        return True

    async def capture(self, name):
        await self.pause(.2)
        await self.command('import', '-window', self.window, str(self.stage / f'{name}.png'))


async def run(stage: Path, extension: Path, *, physical=False):
    assert '/site-packages/' in inspect.getfile(Goals), 'Use the installed candidate, not editable source'
    from toad.agent_schema import AgentDefinition
    from toad.app import ToadApp

    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ.update(XDG_CONFIG_HOME=str(stage / 'ui-config'),
                      XDG_STATE_HOME=str(stage / 'ui-state'),
                      XDG_DATA_HOME=str(stage / 'ui-data'))
    (stage / 'native').mkdir(mode=0o700)
    async with native_backend_fixture(stage / 'native') as native:
        await native.author_history()
        original = native.session.read_bytes()
        options = ('--extension', str(extension))
        async with native.open_owner(runtime_enabled=True, auto_wake=True,
                                     native_options=options) as (owner, session):
            comms = owner._comms
            comms.registry.register(replace(comms.registry.require(session), auto_title_pending=False))

            class Responses:
                async def wait(self):
                    number = native.provider.posts
                    if number == 1:
                        native.provider.tool_call = ('comms_set_goal', {'text': 'Model-authored retry objective'})
                    elif number in (2, 4):
                        goal = Comms(native.root).registry.require(session).require_goal()
                        native.provider.tool_call = ('comms_goal', {
                            'goal_id': goal.id,
                            'status': 'blocked' if number == 2 else 'completed',
                            'progress': 'Need an explicit user Retry decision' if number == 2 else 'Retry completed',
                        })
                    elif number not in (3, 5):
                        raise AssertionError(f'Unexpected provider request {number}; no extra continuation allowed')
                    return True

            native.provider.response_gate = Responses()
            definition = AgentDefinition.decode({
                'name': 'Goal generation acceptance', 'identity': 'goal-generation',
                'short_name': 'goal', 'protocol': 'acp',
                'run_command': {'*': shlex.join([sys.executable, '-m', 'agent_comms.acp'])},
            })
            app = ToadApp(agent_data=definition, project_dir=str(native.project),
                          agent_session_id=session)
            async def journey(pilot):
                async def until(predicate):
                    async with asyncio.timeout(35):
                        while not predicate():
                            await pilot.pause(.05)
                            assert app._exception is None, app._exception

                await until(lambda: app.selected_session.conversation is not None
                            and app.selected_session.conversation.agent_ready)
                view = app.selected_session.conversation
                await until(lambda: 'Original saved answer' in '\n'.join(
                    strip.text for strip in app.screen._compositor.render_strips()))
                prompt = f'[{stage.name}] Set your own persistent goal, then block it for my explicit Retry.'
                view.prompt.focus()
                if physical:
                    await pilot.type(prompt)
                else:
                    view.prompt.text = prompt
                await pilot.press('enter')
                await until(lambda: comms.registry.require(session).goal is not None
                            and comms.registry.require(session).goal.state.declared_name == 'blocked'
                            and not comms.registry.require(session).executing)
                blocked = comms.registry.require(session).goal
                store = comms.goals.open_goal_store()
                before = store.require_generation(blocked.id)
                assert before.lifecycle == BlockedGeneration(), before
                await until(lambda: view.query_one('#goal-toggle').display
                            and not view.query_one('#goal-toggle').disabled
                            and 'Retry' in str(view.query_one('#goal-toggle').render()))
                app.save_screenshot(str(stage / 'blocked-before-retry.svg'))
                if physical:
                    await pilot.capture('blocked-before-retry')
                assert await pilot.click(view.query_one('#goal-toggle')), 'Click the actual visible Retry control'
                await until(lambda: comms.registry.require(session).goal.state.declared_name == 'completed'
                            and not comms.registry.require(session).executing)
                completed = comms.registry.require(session).goal
                generation = store.require_generation(completed.id)
                assert completed.id == blocked.id
                assert generation.number == before.number + 1
                assert generation.lifecycle == CompletedGeneration()
                assert native.provider.posts == 5
                assert len(native.saved_inputs()) == 3  # SDK seed, initial prompt, fresh Retry continuation.
                assert native.session.read_bytes().startswith(original)
                await until(lambda: view.goal_display.snapshot is not None
                            and view.goal_display.snapshot.state.declared_name == 'completed')
                app.save_screenshot(str(stage / 'completed-after-retry.svg'))
                if physical:
                    await pilot.capture('completed-after-retry')
                receipt = {'installed_core': inspect.getfile(Goals),
                           'physical_st': physical,
                           'saved_history_visible': True, 'model_set_then_blocked': True,
                           'actual_ui_retry_click': True, 'goal_identity_preserved': True,
                           'retry_generation': generation.number,
                           'completed': True, 'localhost_posts': native.provider.posts,
                           'saved_inputs': len(native.saved_inputs())}
                (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')

            if physical:
                async def drive(_pilot):
                    try:
                        await journey(PhysicalPilot(app, stage))
                    finally:
                        app.exit()
                await app.run_async(auto_pilot=drive)
            else:
                async with app.run_test(size=(120, 40)) as pilot:
                    await journey(pilot)
    print((stage / 'receipt.json').read_text(), flush=True)
    print('GOAL_GENERATION_INSTALLED_UI_PASS', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--extension', type=Path, required=True,
                        help='Approved compiled comms entry from the native deployment manifest')
    parser.add_argument('--physical', action='store_true',
                        help='Run in an owned plain st terminal on an isolated X display')
    args = parser.parse_args()
    asyncio.run(run(args.stage.resolve(), args.extension.resolve(strict=True), physical=args.physical))
