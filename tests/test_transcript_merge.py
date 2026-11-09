from agent_comms.transcript.transcript_events import (
    AssistantTranscript,
    ContextTranscript,
    ThinkingTranscript,
    UserTranscript,
)
from conftest import transcript_records


def test_stream_merge_is_owned_by_event_and_preserves_declaration():
    for kind in (AssistantTranscript, ThinkingTranscript):
        first, continued = transcript_records.event(kind, "start", position=1), transcript_records.event(kind, "start continued", position=1)
        assert first.merge(continued) is continued
        assert first.merge(transcript_records.event(UserTranscript, "start continued", position=1)) is None
    assert transcript_records.event(ContextTranscript, "start", position=1).merge(transcript_records.event(ContextTranscript, "continued", position=1)) is None

