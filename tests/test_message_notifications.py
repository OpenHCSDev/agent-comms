"""Read real persisted notification assignments without scheduling a model."""
from agent_comms.repo import Checkout
from dataclasses import replace
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from conftest import installed_comms
from agent_comms.field_codec import TurnId
from agent_comms.threads import Thread


def test_channel_assignments_are_visible_from_agent_view(tmp_path):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    per_message = comms.views.message_notifications_for_references((message.reference,))
    receipts = comms.views.thread_presentations(('beta',)).require('beta').notifications
    assert len(receipts) == 1
    acknowledgement = receipts[0]
    assert acknowledgement.message == message
    assert acknowledgement.message.target == '#team'
    assert replace(acknowledgement, message=None) in per_message.outcomes[(message.seq, message.message_id)]
    assert acknowledgement.label == acknowledgement.timed_state('Sent', acknowledgement.state.sent_at_ms)
    assert not comms.registry.require('beta').executing


def test_new_reader_has_no_fabricated_receipts(tmp_path):  # noqa: F811

    root = tmp_path / 'new'
    comms = installed_comms(root)
    comms.registry.declare(Thread('reader', frozenset(), Checkout(tmp_path)))
    assert comms.views.thread_presentations(('reader',)).require('reader').notifications == ()


def test_live_turn_never_presents_ready_between_activity_events(tmp_path):  # noqa: F811
    from agent_comms.activity import ActivityState

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    lease = comms.agents.begin_turn('beta', TurnId('3c5469ccca7ada4828e500d32e2025e7')).turn_lease
    comms.agents.set_activity('beta', ActivityState.IDLE)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert view.presentation.status.working
    state = comms.registry.require('beta').turn_state
    assert state.active.id == lease.turn_id
    assert state.active.phase.summary in view.presentation.summary
    comms.agents.finish_turn(lease)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert not view.presentation.status.working
    assert view.presentation.summary == 'Ready'


def test_individual_view_keeps_alias_goal_and_active_owner_semantics(tmp_path):  # noqa: F811
    from agent_comms.activity import ActivityState
    from agent_comms.goal_actions import SetGoalAction

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.goals.update_goal('beta', SetGoalAction(text='Finish native delivery'))
    comms.registry.rename('beta', 'renamed')
    for name in ('beta', 'renamed'):
        individual = comms.views.thread_presentations((name,)).require(name)
        view = next(v for v in comms.views.thread_views() if v.thread.name == 'renamed')
        assert individual.title == view.presentation.title
        assert individual.summary == view.presentation.summary
        assert individual.read_identity.thread.name == view.thread.name
    lease = comms.agents.begin_turn(
        'renamed', TurnId('6d520fcb678df3e43b0fa663d3e618be')
    ).turn_lease
    comms.agents.set_activity('renamed', ActivityState.IDLE)
    assert comms.views.thread_presentations(('beta',)).require('beta').status.working
    state = comms.registry.require('renamed').turn_state
    assert state.active.id == lease.turn_id
    assert state.active.phase.summary in comms.views.thread_presentations(('beta',)).require('beta').summary
    comms.agents.finish_turn(lease)
    assert not comms.views.thread_presentations(('beta',)).require('beta').status.working
