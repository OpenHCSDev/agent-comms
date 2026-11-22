"""An artifact frame must not stop the Claude transcript reader."""
import json

import pytest
from pydantic import ValidationError

from agent_comms.cli_bridges.claude_protocol import CLAUDE_RECORD, ClaudeSession


def test_frame_link_is_metadata_without_turn_or_delivery_effects():
    # Observed native shape; the original title, path and artifact URL are private.
    raw = json.dumps({
        "type": "frame-link", "sessionId": "frame-session",
        "path": "/workspace/scratchpad/brief.html",
        "frameUrl": "https://claude.ai/artifact/frame-fixture",
        "title": "Brief", "artifactCount": 1,
        "timestamp": "2026-11-11T21:54:39.489Z", "position": 33135,
    })
    record = CLAUDE_RECORD.validate_json(raw)
    assert type(record).declared_name == "frame-link"
    assert record.updates(ClaudeSession()) == ()
    assert record.delivered() == ()
    assert record.owner_words() == ()
    assert not record.ends_turn()
    assert not record.begins_history()


@pytest.mark.parametrize("extra", [{}, {"snapshot": True}])
def test_data_flow_records_are_metadata_without_turn_or_delivery_effects(extra):
    # Claude Code 2.1.330 writes these with and without the snapshot flag; ids are fixtures.
    record = CLAUDE_RECORD.validate_python({
        "type": "data-flow-records", "v": 1, "sessionId": "data-flow-session",
        "records": [{"tool_name": "RemoteControlLinked", "event_id": "event_fixture"}], **extra,
    })
    assert type(record).declared_name == "data-flow-records"
    assert record.updates(ClaudeSession()) == ()
    assert record.delivered() == ()
    assert record.owner_words() == ()
    assert not record.ends_turn()
    assert not record.begins_history()


def test_unknown_top_level_record_is_not_assumed_to_be_metadata():
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        CLAUDE_RECORD.validate_python({"type": "unmodelled-input-fixture"})
