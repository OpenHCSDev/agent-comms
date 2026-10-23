"""Saved native history -> model-set goal -> blocked -> visible UI Retry.

Run with the candidate wheel installed and the shipped Toad dependencies.
Only the existing loopback provider's responses are controlled.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from toad.agent_schema import AgentDefinition
from toad.app import ToadApp
from toad.goal_interaction import ToggleInteraction
from toad.widgets.goal_bar import GoalControl

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_generation import BlockedGeneration, CompletedGeneration
from agent_comms.goal_management import Goals
from agent_comms.goal_states import BlockedGoal, CompletedGoal
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import ThreadLaunch
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.progress_claims import WorkingClaim
from native_backend_fixture import native_backend_fixture


async def run(stage: Path, extension: Path, *, package: VerifiedNativePackage) -> None:
    assert '/site-packages/' in inspect.getfile(Goals), (
        'Use the installed candidate, not editable source'
    )
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ.update(XDG_CONFIG_HOME=str(stage / 'ui-config'),
                      XDG_STATE_HOME=str(stage / 'ui-state'),
                      XDG_DATA_HOME=str(stage / 'ui-data'))
    (stage / 'native').mkdir(mode=0o700)
    async with native_backend_fixture(stage / 'native', package=package) as native:
        await native.author_history()
        original = native.session.read_bytes()
        options = ('--extension', str(extension))
        async with native.open_owner(runtime_enabled=True, auto_wake=True,
                                     native_options=options) as (owner, session):
            comms = owner._comms
            comms.registry.register(
                replace(comms.registry.require(session), auto_title_pending=False)
            )

            class Responses:
                async def wait(self) -> bool:
                    number = native.provider.posts
                    if number == 1:
                        native.provider.tool_call = ('comms_set_goal', {
                            'text': 'Prove an explicit Retry starts the next generation',
                        })
                    elif number in (2, 4):
                        goal = comms.registry.require(session).goal.require()
                        claims: tuple[WorkingClaim, ...] = ()
                        if number == 4:
                            resumed = store.require_generation(goal.id)
                            assert resumed.number == before.number + 1
                            screenshot = app.save_screenshot(str(stage / 'retry-after-click.svg'))
                            claims = (WorkingClaim(screenshot),)
                        native.provider.tool_call = ('comms_goal', {
                            'goal_id': goal.id,
                            'status': 'blocked' if number == 2 else 'completed',
                            'progress': (
                                'Need an explicit user Retry decision'
                                if number == 2 else 'Retry completed'
                            ),
                            'claims': [FieldCodec.encode(claim) for claim in claims],
                        })
                    elif number not in (3, 5):
                        raise AssertionError(
                            f'Unexpected provider request {number}; no extra continuation allowed'
                        )
                    return True

            native.provider.response_gate = Responses()
            definition = AgentDefinition.decode({
                'name': 'Goal generation acceptance', 'identity': 'goal-generation',
                'short_name': 'goal', 'protocol': 'acp',
                'run_command': {'*': shlex.join([
                    sys.executable, '-m', 'agent_comms.acp',
                    *LaunchRoot.from_path(native.root).arguments(),
                ])},
            })
            app = ToadApp(agent_data=definition, project_dir=str(native.project),
                          agent_session_id=session)
            async with app.run_test(size=(120, 40)) as pilot:
                async def until(predicate: Callable[[], bool]) -> None:
                    async with asyncio.timeout(35):
                        while not predicate():
                            await pilot.pause(.05)
                            assert app._exception is None, app._exception

                await until(lambda: app.selected_session.conversation is not None
                            and app.selected_session.conversation.agent_binding.ready)
                view = app.selected_session.conversation
                await until(lambda: 'Original saved answer' in '\n'.join(
                    strip.text for strip in app.screen._compositor.render_strips()))
                view.prompt.text = (
                    f'[{stage.name}] Set your own persistent goal, '
                    'then block it for my explicit Retry.'
                )
                view.prompt.focus()
                await pilot.press('enter')
                await until(lambda: any(isinstance(goal.state, BlockedGoal)
                                        for goal in comms.registry.require(session).goal)
                            and not comms.registry.require(session).executing)
                blocked = comms.registry.require(session).goal.require()
                store = comms.goals.open_goal_store()
                before = store.require_generation(blocked.id)
                assert before.lifecycle == BlockedGeneration(), before
                retry = view.goal_bar.query_one(
                    f'#{ToggleInteraction.control_id()}', GoalControl,
                )
                await until(lambda: retry.display and not retry.disabled
                            and 'Retry' in str(retry.render()))
                app.save_screenshot(str(stage / 'blocked-before-retry.svg'))
                assert await pilot.click(retry, offset=(retry.size.width // 2, 0)), (
                    'Click the actual visible Retry control'
                )
                await until(lambda: any(isinstance(goal.state, CompletedGoal)
                                        for goal in comms.registry.require(session).goal)
                            and not comms.registry.require(session).executing)
                completed = comms.registry.require(session).goal.require()
                generation = store.require_generation(completed.id)
                assert completed.id == blocked.id
                assert generation.number == before.number + 1
                assert generation.lifecycle == CompletedGeneration()
                assert native.provider.posts == 5
                # SDK seed, initial prompt, fresh Retry continuation.
                assert len(native.saved_inputs()) == 3
                assert native.session.read_bytes().startswith(original)
                await until(lambda: any(isinstance(goal.state, CompletedGoal)
                                        for goal in view.goal_display.snapshot))
                app.save_screenshot(str(stage / 'completed-after-retry.svg'))
                print(json.dumps({'installed_core': inspect.getfile(Goals),
                                  'saved_history_visible': True, 'model_set_then_blocked': True,
                                  'actual_ui_retry_click': True, 'goal_identity_preserved': True,
                                  'retry_generation': generation.number,
                                  'completed': True, 'localhost_posts': native.provider.posts,
                                  'saved_inputs': len(native.saved_inputs())}), flush=True)
    print('GOAL_GENERATION_INSTALLED_UI_PASS', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--extension', type=Path, required=True,
                        help='Approved compiled comms entry from the native deployment manifest')
    args = parser.parse_args()
    ThreadLaunch.strip(os.environ)
    asyncio.run(run(args.stage.resolve(), args.extension.resolve(strict=True),
                    package=VerifiedNativePackage.acquire(pinned_native_package())))
