from agent_comms.acp_extension import TranscriptDraftUpdate
from agent_comms.comms import wire
from agent_comms.ui_model.observation import NotPublished, Published


def test_published_identity_refresh_reads_only_the_draft(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread("reader", frozenset(), str(tmp_path)))
    read = comms.transcripts.capture_page_read("reader")
    snapshot = NotPublished().result(read)
    assert snapshot.identity == read.identity
    expected = TranscriptDraftUpdate(read.identity, read.current_draft())

    def no_page_read(self):
        raise AssertionError("An unchanged published page must not be read again")

    monkeypatch.setattr(type(read), "read_snapshot", no_page_read)
    assert Published(read.identity).result(read) == expected
