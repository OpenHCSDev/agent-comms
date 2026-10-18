"""Owned installed old-goal journey: missing refusal -> carry -> UI Retry."""
import argparse
import asyncio
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import shlex
import sys
from uuid import uuid4

from agent_comms.comms import Comms
from agent_comms.goal_attempts import MissingGoalGenerationError
from agent_comms.goal_generation import CompletedGeneration, ReadyGeneration
from agent_comms.goal_management import Goals
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import Goal
from native_backend_fixture import native_backend_fixture


async def run(stage, extension):
    assert '/site-packages/' in inspect.getfile(Goals)
    from toad.agent_schema import AgentDefinition
    from toad.app import ToadApp
    stage.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ.update(XDG_CONFIG_HOME=str(stage/'config'), XDG_STATE_HOME=str(stage/'state'),
                      XDG_DATA_HOME=str(stage/'data'))
    (stage/'native').mkdir(mode=0o700)
    async with native_backend_fixture(stage/'native') as native:
        await native.author_history()
        original = native.session.read_bytes()
        async with native.open_owner(runtime_enabled=True, auto_wake=True,
            native_options=('--extension', str(extension))) as (owner, session):
            comms = owner._comms
            legacy = Goal(text='Retained pre-777 objective; never replay uncertain work',
                          id=uuid4().hex, state=BlockedGoal('Explicit Retry is required'))
            comms.registry.register(replace(comms.registry.require(session), goal=legacy,
                                            auto_title_pending=False))
            store = comms.goals.open_goal_store()
            with_exception = False
            try:
                store.require_generation(legacy.id)
            except MissingGoalGenerationError:
                with_exception = True
            assert with_exception
            class Responses:
                async def wait(self):
                    number = native.provider.posts
                    if number == 1:
                        native.provider.tool_call = ('comms_goal', {
                            'goal_id': legacy.id, 'status': 'completed', 'progress': 'Carried goal completed'})
                    else:
                        assert number == 2, 'No extra continuation admitted'
                    return True
            native.provider.response_gate = Responses()
            definition = AgentDefinition.decode({
                'name': 'Historical goal carry acceptance', 'identity': 'historical-goal-carry',
                'short_name': 'carry', 'protocol': 'acp',
                'run_command': {'*': shlex.join([sys.executable, '-m', 'agent_comms.acp'])},
            })
            app = ToadApp(agent_data=definition, project_dir=str(native.project), agent_session_id=session)
            async with app.run_test(size=(120,40)) as pilot:
                async def until(predicate):
                    async with asyncio.timeout(35):
                        while not predicate():
                            await pilot.pause(.05)
                            assert app._exception is None, app._exception
                await until(lambda: app.selected_session.conversation is not None and
                            app.selected_session.conversation.agent_ready)
                view = app.selected_session.conversation
                await until(lambda: view.goal_display.snapshot is not None and
                            view.goal_display.snapshot.state.declared_name=='blocked')
                await until(lambda: view.query_one('#goal-toggle').display and
                            not view.query_one('#goal-toggle').disabled)
                assert await pilot.click(view.query_one('#goal-toggle'))
                await until(lambda: any('no launch authority' in strip.text
                            for strip in app.screen._compositor.render_strips()))
                assert native.provider.posts == 0
                assert len(native.saved_inputs()) == 1
                assert comms.registry.require(session).goal == legacy
                app.save_screenshot(str(stage/'missing-before-carry.svg'))
                # The operator's exact durable writer, now creating a fresh
                # unreserved generation. This says nothing about past execution.
                store.create_goal(legacy.id)
                assert comms.registry.require(session).goal == legacy
                assert store.require_generation(legacy.id).lifecycle == ReadyGeneration()
                assert native.provider.posts == 0
                app.save_screenshot(str(stage/'carried-still-blocked.svg'))
                await until(lambda: not view.query_one('#goal-toggle').disabled)
                assert await pilot.click(view.query_one('#goal-toggle'))
                await until(lambda: comms.registry.require(session).goal.state.declared_name=='completed'
                            and not comms.registry.require(session).executing)
                generation = store.require_generation(legacy.id)
                assert generation.number==2 and generation.lifecycle==CompletedGeneration()
                assert native.provider.posts==2 and len(native.saved_inputs())==2
                assert native.session.read_bytes().startswith(original)
                await until(lambda: view.goal_display.snapshot.state.declared_name=='completed')
                app.save_screenshot(str(stage/'completed-after-explicit-retry.svg'))
                (stage/'receipt.json').write_text(json.dumps({
                    'core': inspect.getfile(Goals), 'saved_history_prefix_preserved': True,
                    'missing_retry_refused_with_zero_provider_requests': True,
                    'carry_preserved_blocked_public_goal': True,
                    'actual_retry_widget_clicked': True, 'same_goal_generation': generation.number,
                    'completed': True, 'localhost_posts': native.provider.posts,
                    'saved_inputs': len(native.saved_inputs())}, indent=2)+'\n')
    print((stage/'receipt.json').read_text())
    print('HISTORICAL_GOAL_INSTALLED_JOURNEY_PASS')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', type=Path, required=True)
    p.add_argument('--extension', type=Path, required=True)
    a=p.parse_args()
    asyncio.run(run(a.stage.resolve(),a.extension.resolve(strict=True)))
