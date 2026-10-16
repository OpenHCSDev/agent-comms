"""Notification windows share one exact SQL acquisition and prepared namespace cut."""
from agent_comms.presentation import MessageNotification
from agent_comms.read_ledger import ReadDocument, ReadLedger
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


def test_reference_windows_share_one_read_document(tmp_path, monkeypatch):  # noqa: F811
    _path, _root_id, comms, initial, _people = _root(tmp_path)
    message = initial.message
    expected = comms.views.message_notifications((message,))
    acquired, prepared = [], []
    capture = ReadLedger.capture_observations
    prepare = ReadDocument.observations

    def capture_once(self, snapshot, sources):
        acquired.append(len(sources))
        return capture(self, snapshot, sources)

    def prepare_once(self, snapshot):
        prepared.append(len(self.messages))
        return prepare(self, snapshot)

    monkeypatch.setattr(ReadLedger, 'capture_observations', capture_once)
    monkeypatch.setattr(ReadDocument, 'observations', prepare_once)
    assert comms.views.message_notifications_for_references(
        (message.reference,) * (MessageNotification.window_limit + 1)
    ) == expected
    assert len(acquired) == len(prepared) == 1
    acquired.clear()
    prepared.clear()
    assert comms.views.message_notifications_for_references(()) == {}
    assert acquired == prepared == []
