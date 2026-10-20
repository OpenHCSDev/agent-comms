from conftest import transcript_records
from agent_comms.ui_model.message_category import AgentCategory
from agent_comms.transcript_events import (
    AssistantTranscript,
    ContextTranscript,
    LiveTextTranscript,
    ThinkingTranscript,
    UserTranscript,
)


def test_stream_merge_is_owned_by_event_and_preserves_declaration():
    for kind in (AssistantTranscript, ThinkingTranscript):
        first, continued = transcript_records.event(kind, "start", position=1), transcript_records.event(kind, "start continued", position=1)
        assert first.merge(continued) is continued
        assert first.merge(transcript_records.event(UserTranscript, "start continued", position=1)) is None
    assert transcript_records.event(ContextTranscript, "start", position=1).merge(transcript_records.event(ContextTranscript, "continued", position=1)) is None


def test_new_stream_declaration_inherits_merge_without_dispatch_changes():
    class TestStreamTranscript(LiveTextTranscript):
        category = AgentCategory

        @property
        def routed(self):
            return False

    first, continued = transcript_records.event(TestStreamTranscript, "start", position=1), transcript_records.event(TestStreamTranscript, "continued", position=1)
    assert first.merge(continued) is continued
