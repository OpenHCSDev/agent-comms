from agent_comms.transcript_events import AssistantTranscript, ContextTranscript, LiveTextTranscript, ThinkingTranscript, UserTranscript


def test_stream_merge_is_owned_by_event_and_preserves_declaration():
    for kind in (AssistantTranscript, ThinkingTranscript):
        first, continued = kind('start'), kind('start continued')
        assert first.merge(continued) is continued
        assert first.merge(UserTranscript('start continued')) is None
    assert ContextTranscript('start').merge(ContextTranscript('continued')) is None


def test_new_stream_declaration_inherits_merge_without_dispatch_changes():
    class TestStreamTranscript(LiveTextTranscript):
        def replay_update(self):
            return None
    first, continued = TestStreamTranscript('start'), TestStreamTranscript('continued')
    assert first.merge(continued) is continued
