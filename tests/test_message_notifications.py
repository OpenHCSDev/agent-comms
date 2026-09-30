"""Read real persisted notification assignments without scheduling a model."""
from dataclasses import replace

from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_channel_assignments_are_visible_from_agent_view(tmp_path):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    per_message = comms.views.message_notifications((message,))
    receipts = comms.views.recent_notifications('beta')
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.message == message
    assert receipt.message.target == '#team'
    assert replace(receipt, message=None) in per_message[(message.seq, message.message_id)]
    assert receipt.state == 'Pending'
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert comms.views.thread_presentation('beta') == replace(
        view.presentation,
        notifications=receipts,
        read_identity=comms.transcripts.capture_page_read('beta').identity,
    )
    assert comms.registry.require('beta').active_turn is None
    wrong = replace(message, body='Different source bytes')
    assert comms.views.message_notifications((wrong,)) == {(wrong.seq, wrong.message_id): ()}


def test_legacy_bus_has_no_fabricated_receipts(tmp_path):  # noqa: F811
    from agent_comms.comms import Comms
    from agent_comms.threads import Thread

    root = tmp_path / 'legacy'
    comms = Comms(root)
    comms.registry.declare(Thread('reader', frozenset(), str(tmp_path)))
    assert comms.views.recent_notifications('reader') == ()
    assert not (root / 'coordination.sqlite3').exists()


def test_live_turn_never_presents_ready_between_activity_events(tmp_path):  # noqa: F811
    from agent_comms.activity import ActivityState

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    lease = comms.agents.begin_turn('beta', 'test-active-turn')
    comms.agents.set_activity('beta', ActivityState.IDLE)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert view.presentation.busy
    assert 'In a turn' in view.presentation.summary
    comms.agents.finish_turn(lease)
    view = next(v for v in comms.views.thread_views() if v.thread.name == 'beta')
    assert not view.presentation.busy
    assert view.presentation.summary == 'Ready'


def test_individual_view_keeps_alias_goal_and_active_owner_semantics(tmp_path):  # noqa: F811
    from agent_comms.activity import ActivityState
    from agent_comms.goal_actions import SetGoalAction

    _path, _root_id, comms, _initial, _people = _root(tmp_path)
    comms.goals.update_goal('beta', SetGoalAction(text='Finish native delivery'))
    comms.registry.rename('beta', 'renamed')
    for name in ('beta', 'renamed'):
        individual = comms.views.thread_presentation(name)
        view = next(v for v in comms.views.thread_views() if v.thread.name == 'renamed')
        assert individual == replace(
            view.presentation,
            notifications=comms.views.recent_notifications(name),
            read_identity=comms.transcripts.capture_page_read(view.thread.name).identity,
        )
    lease = comms.agents.begin_turn('renamed', 'individual-active-turn')
    comms.agents.set_activity('renamed', ActivityState.IDLE)
    assert comms.views.thread_presentation('beta').busy
    assert 'In a turn' in comms.views.thread_presentation('beta').summary
    comms.agents.finish_turn(lease)
    assert not comms.views.thread_presentation('beta').busy
