"""Own live publications advance authority; external changes and saved data do not."""
import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import OwnerInvocable, PausedGoalAction
from agent_comms.goal_states import PausedGoal
from agent_comms.goals import Goal, NoGoal
from agent_comms.native_tools import CoordinationCall, ReadTool
from agent_comms.pi_payloads import ProvidedToolResult
from agent_comms.tools import GoalSet
from agent_comms.turn_goal_permission import OwnerGoalPermission, TurnGoalPermission
from delivery_owner_fixture import reply_turn_finished


@pytest.mark.parametrize('pause_by_owner', (False, True))
async def test_turn_keeps_working_after_setting_its_own_goal(native_backend, monkeypatch, pause_by_owner):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    (native.project / 'owned-goal-read.txt').write_text('the read ran after the goal write\n')
    checks = []
    allow = CodingToolOwner.allow

    async def authorized(owner, turn, round_number, call):
        if pause_by_owner and call.declared_name == 'read':
            owner.comms.goals.update_goal(
                owner.owner_name, PausedGoalAction(), actor=OwnerInvocable)
        reservation = await allow(owner, turn, round_number, call)
        checks.append(call.declared_name)
        return reservation

    monkeypatch.setattr(CodingToolOwner, 'allow', authorized)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread('requester', tags=frozenset(), worktree=str(native.project))

        def chunks():
            number = native.provider.posts
            assert number <= 4, 'No original input or uncertain tool is replayed'
            if number == (3 if pause_by_owner else 4):
                yield {'content': 'The own-goal authority check is complete.'}, None
                yield {}, 'stop'
                return
            if number == 1:
                name, arguments = 'comms_set_goal', {'text': 'Read the private file, then block this goal.'}
            elif number == 2:
                name, arguments = 'read', {'path': 'owned-goal-read.txt'}
            else:
                goal = comms.registry.require(session).goal.require()
                name, arguments = 'comms_goal', dict(goal_id=goal.id, status='blocked',
                                                     progress='The private read completed.')
            yield {'tool_calls': [{'index': 0, 'id': f'own-goal-{number}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, None
            yield {}, 'tool_calls'

        monkeypatch.setattr(native.provider, 'response_chunks', chunks)
        comms.messaging.send('requester', session, 'Set your private goal and read the file.').message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 45)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        results = [row['message'] for row in rows if row.get('message', {}).get('role') == 'toolResult']
        assert results[0]['toolName'] == 'comms_set_goal' and not results[0].get('isError')
        assert results[1]['toolName'] == 'read'
        if pause_by_owner:
            assert results[1]['isError']
            assert 'ordinary_goal' in results[1]['content'][0]['text']
            assert checks == ['comms_set_goal']
            assert isinstance(comms.registry.require(session).goal.require().state, PausedGoal)
        else:
            assert not results[1].get('isError')
            assert 'the read ran after the goal write' in results[1]['content'][0]['text']
            assert checks == ['comms_set_goal', 'read', 'comms_goal']
            assert comms.registry.require(session).goal.require().state.declared_name == 'blocked'
        assert native.session.read_bytes().startswith(original)
        assert len([row for row in rows if row.get('message', {}).get('inputId')]) == 1


def test_goal_publication_is_exact_and_coding_output_cannot_author_it():
    original = NoGoal()
    goal = Goal('the actual publication', 'own-goal', published_seq=0)
    permission = TurnGoalPermission(OwnerGoalPermission(original))
    result = ProvidedToolResult.from_wire({'content': [], 'details':
        FieldCodec.encode(GoalSet(goal, 'continue in this turn'))})
    # Neither decoding a saved result nor coding output grants authority.
    assert permission.allows(original) and not permission.allows(goal)
    ReadTool(call_id='coding', path='x').published_goal(result, permission)
    assert not permission.allows(goal)
    CoordinationCall(call_id='own', declared_name='comms_set_goal', arguments={}).published_goal(result, permission)
    assert permission.allows(goal)
    assert not permission.allows(replace(goal, revision=goal.revision + 1, state=PausedGoal()))
    assert not permission.allows(replace(goal, id='another-commanders-goal'))
