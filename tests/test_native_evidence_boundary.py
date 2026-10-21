"""Evidence preserves opaque content without granting plain-text or launch authority."""

import json

import pytest

from agent_comms.native_entries import NativeEntry
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.pi_payloads import PiOmitted, TextContent, UnknownContent


def test_original_header_preserves_project_and_fork_lineage(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    file = sessions / "child.jsonl"
    raw = {
        "type": "session", "version": 3, "id": "child-session",
        "timestamp": "2026-10-01T00:00:00.000Z",
        "cwd": str(tmp_path / "original-project"),
        "parentSession": str(sessions / "original-parent.jsonl"),
    }
    file.write_text(json.dumps(raw) + "\n")
    file.chmod(0o600)
    before = file.read_bytes()
    with NativeEntry.open_evidence(file) as evidence:
        header, entries = evidence.observe()
        assert entries == (header,)
        assert header.cwd == raw["cwd"]
        assert header.parent_session == raw["parentSession"]
        encoded = header.to_wire()
        assert all(encoded[name] == value for name, value in raw.items())
    assert file.read_bytes() == before
    incomplete = NativeEntry.from_evidence({"type": "session", "timestamp": "2026-10-01T00:00:00.000Z", "id": "unrecorded"})
    assert incomplete.cwd == incomplete.parent_session == PiOmitted()


@pytest.mark.parametrize(
    "part",
    [
        {"type": "text", "text": "same", "extra": "not plain text"},
        {"type": "text", "text": "same", "textSignature": None},
        {"type": "extension", "text": "same", "data": {"version": 1}},
    ],
)
def test_unrepresented_content_is_preserved_but_cannot_match_started_text(part):
    entry = NativeEntry.from_evidence(
        {
            "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
            "id": "entry",
            "message": {
                "role": "user",
                "inputId": "a" * 32,
                "inputDigest": "b" * 64,
                "content": [part],
            },
        }
    )
    assert entry.tracked_user is entry
    assert entry.message.content != (TextContent("same"),)
    assert isinstance(entry.message.content[0], UnknownContent)
    assert entry.message.content[0].to_wire() == part


@pytest.mark.parametrize("role", ["assistant", "toolResult", "extension"])
def test_non_user_tracked_identity_cannot_disappear_into_display_fallback(role):
    raw = {"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "id": "entry", "message": {
        "role": role, "content": [], "inputId": "a" * 32,
    }}
    # Pi requires assistant content; omission fails before tracked-role ownership.
    # Both original history and input-proof projection must reject this same ID.
    for decode in (NativeEntry.from_evidence, NativeEntry.wire_member(raw).input_evidence):
        with pytest.raises(ValueError):
            decode(raw)






def test_duplicate_input_ids_are_not_silently_projected_away(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    session = sessions / "s.jsonl"
    rows = [{"type": "session", "timestamp": "2026-10-01T00:00:00.000Z", "id": "sid", "version": 3}] + [
        {"type": "message", "timestamp": "2026-10-01T00:00:00.000Z", "id": entry_id, "message": {"role": "user", "inputId": "a" * 32, "inputDigest": "b" * 64}}
        for entry_id in ("first", "second")]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    session.chmod(0o600)
    with pytest.raises(NativePiUnavailable):
        NativeEntry.read_evidence(session)
