"""Notification operations consume one real read-ledger document across source windows."""
from agent_comms.locked_store import JsonDocument
from agent_comms.presentation import MessageNotification
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_reference_windows_share_one_read_document(tmp_path, monkeypatch):
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    comms.bus.reads.replace(comms.bus.reads.empty())
    expected = comms.views.message_notifications((message,))
    decoded = []
    decode = JsonDocument._decode

    def counted(self, data):
        if self.path == comms.bus.reads.path:
            decoded.append(self.path)
        return decode(self, data)

    monkeypatch.setattr(JsonDocument, "_decode", counted)
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)
    ) == expected
    assert decoded == [comms.bus.reads.path]
    decoded.clear()
    assert comms.views.message_notifications_for_references(()) == {}
    assert decoded == []
