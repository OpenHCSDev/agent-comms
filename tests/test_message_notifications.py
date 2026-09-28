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
    assert comms.registry.require('beta').active_turn is None
    wrong = replace(message, body='Different source bytes')
    assert comms.views.message_notifications((wrong,)) == {(wrong.seq, wrong.message_id): ()}


def test_legacy_bus_has_no_fabricated_receipts(tmp_path):  # noqa: F811
    from agent_comms.comms import Comms
    from agent_comms.threads import Thread

    root = tmp_path / 'legacy'
    comms = Comms(root)
    comms.threads.register(Thread('reader', frozenset(), str(tmp_path)))
    assert comms.views.recent_notifications('reader') == ()
    assert not (root / 'coordination.sqlite3').exists()
