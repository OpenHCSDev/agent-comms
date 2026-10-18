from dataclasses import replace

from agent_comms.presentation import WireRevision
from agent_comms.ui_model.observation import NotRead, ReadAt, ReadNotifications, ReadRecordedNotifications


def test_notification_read_states_use_the_request_revision_contract():
    original = WireRevision.empty()
    later = replace(original, expiry_tick=original.expiry_tick + 60)
    live = ReadNotifications("root", ())
    recorded = ReadRecordedNotifications("root", (), "archive")
    assert NotRead().changed(live, original)
    assert not ReadAt(original).changed(live, original)
    assert ReadAt(original).changed(live, later)
    assert not ReadAt(original).changed(recorded, later)
