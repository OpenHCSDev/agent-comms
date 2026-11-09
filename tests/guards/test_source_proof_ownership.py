"""Transcript categories and historical identity retain their declared distinctions."""

import ast
from inspect import getfile
from pathlib import Path

import pytest

from agent_comms import history_views
from agent_comms.execution.transcript_sources import TranscriptCursor
from agent_comms.messaging import historical_views
from agent_comms.transcript.transcripts import TranscriptPage

pytestmark = pytest.mark.refactor_guard


def test_original_wire_reply_has_no_native_streaming_membership():
    """Real215-05 and composition215-01 rendered one row through both handlers."""
    from agent_comms.transcript.transcript_events import (
        AgentTextTranscript,
        IncomingTranscript,
        MarkdownTranscript,
        SentTranscript,
        UserTranscript,
        WireTextTranscript,
    )
    from agent_comms.transcript.transcript_merge import StreamingMerge

    assert not issubclass(SentTranscript, AgentTextTranscript)
    assert not issubclass(SentTranscript, StreamingMerge)
    assert not issubclass(IncomingTranscript, UserTranscript)
    assert not issubclass(IncomingTranscript, StreamingMerge)
    assert issubclass(WireTextTranscript, MarkdownTranscript)


def test_archived_identity_cannot_reacquire_a_live_registry_or_thread():
    from dataclasses import MISSING, fields
    from typing import get_type_hints

    from agent_comms.messaging.historical_views import HistoricalThread, HistorySource
    from agent_comms.registry.threads import ThreadProvenance

    provenance = next(item for item in fields(HistorySource) if item.name == "provenance")
    assert provenance.default is MISSING and provenance.default_factory is MISSING
    assert get_type_hints(HistoricalThread)["thread"] is ThreadProvenance
    assert not hasattr(HistorySource, "registry")
    paths = (Path(historical_views.__file__), Path(history_views.__file__),
             Path(getfile(TranscriptCursor)), Path(getfile(TranscriptPage)))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            assert not (isinstance(node, ast.Attribute) and node.attr == "registry"
                        and isinstance(node.value, ast.Name) and node.value.id == "source"), path
