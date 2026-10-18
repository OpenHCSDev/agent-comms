"""One-time P502 carry uses the old decoder and leaves every item identity intact."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cutover.convert_external_tools import OLD_PYTHON, _events, convert

from agent_comms.external_transcript import (
    ExternalDraftRow,
    ExternalItemRow,
    NoEarlierPage,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.tool_declaration import ToolDeclaration
from agent_comms.transcript_events import ToolStartTranscript


def test_unrecognized_old_tool_shape_refused():
    with pytest.raises(ValueError, match="Unrecognized"):
        _events([{"kind": "tool_start", "tool": {"other": "bash"}}], "external_item", 1)


@pytest.mark.skipif(not OLD_PYTHON.is_file(), reason="pinned old installed Core unavailable")
def test_copied_external_items_and_drafts_convert_once(tmp_path: Path):
    path = tmp_path / "external_transcripts.sqlite3"
    event = ToolStartTranscript(
        tool_call_id="call",
        tool_name="bash",
        raw_input={"command": "pwd"},
        tool=ToolDeclaration.record("bash", {"command": "pwd"}),
    )
    raw = FieldCodec.encode(event)
    assert raw["tool"]["tool"] == "bash"
    raw["tool"]["kind"] = raw["tool"].pop("tool")
    old = json.dumps([raw], separators=(",", ":"))
    with closing(sqlite3.connect(path)) as db:
        ExternalItemRow.create(db)
        ExternalDraftRow.create(db)
        ExternalItemRow(
            thread="thread",
            turn_id="turn",
            item_id="item",
            seq=1,
            earlier=NoEarlierPage(),
            events=(),
        ).insert(db)
        ExternalDraftRow(thread="draft", turn_id="turn", item_id="item", events=()).insert(db)
        db.execute("UPDATE external_item SET events=?", (old,))
        db.execute("UPDATE external_draft SET events=?", (old,))
        db.commit()

    before = convert(path, apply=False)
    assert before["rows"] == {"external_item": 1, "external_draft": 1}
    assert before["old_tool_parts"] == 2
    assert before["changed_rows"] == {"external_item": 1, "external_draft": 1}
    assert convert(path, apply=True)["identity_sha256"] == before["identity_sha256"]
    after = convert(path, apply=False)
    assert after["old_tool_parts"] == 0
    assert after["already_new_tool_parts"] == 2
    assert after["identity_sha256"] == before["identity_sha256"]
    with closing(sqlite3.connect(path)) as db:
        assert tuple(ExternalItemRow.stream(db))[0].events[0].tool is not None
        assert tuple(ExternalDraftRow.stream(db))[0].events[0].tool is not None
