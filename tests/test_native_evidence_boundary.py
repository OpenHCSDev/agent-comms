"""Evidence preserves opaque content without granting plain-text or launch authority."""

import json
from pathlib import Path

import pytest

from agent_comms.fresh_private_session import FreshPrivateSession, create_fresh_private_session
from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import (
    NativeContextProof,
    NativePiUnavailable,
    prepare_native_pi_rpc_launch,
)
from agent_comms.pi_payloads import TextContent, UnknownContent


@pytest.mark.parametrize("input_id", [None, 1, True, b"a" * 32, "", "a" * 31, "g" * 32])
def test_context_owner_rejects_invalid_lookup_before_reading_files(tmp_path, input_id):
    with pytest.raises(ValueError, match="128-bit input ID"):
        NativeContextProof.read_evidence(tmp_path / "absent.jsonl", input_id)


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
            "type": "message",
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
    with pytest.raises(ValueError, match="must belong to a user"):
        NativeEntry.from_evidence(
            {"type": "message", "id": "entry", "message": {"role": role, "inputId": "a" * 32}}
        )


@pytest.mark.parametrize(
    "marker",
    [
        {"schema": True, "thinkingLevel": "high"},
        {"schema": 2, "thinkingLevel": "high"},
        {"schema": 1, "thinkingLevel": "off"},
        {"schema": 1, "thinkingLevel": "high", "extra": True},
        {"schema": 1},
        [],
        "high",
    ],
)
def test_malformed_saved_denial_marker_never_prepares_launch(tmp_path, monkeypatch, marker):
    import agent_comms.native_pi as native

    fresh = create_fresh_private_session(tmp_path / "sessions", worktree=tmp_path)
    row = json.loads(fresh.path.read_text())
    row["agentCommsSelectedFresh"] = marker
    fresh.path.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    with pytest.raises(NativePiUnavailable, match="first-start token"):
        prepare_native_pi_rpc_launch(
            tmp_path,
            worktree=tmp_path,
            session_dir=fresh.path.parent,
            session_file=fresh.path,
            selected_thinking_level="high",
        )
    assert not (fresh.path.parent / ".native-pi-agent").exists()


@pytest.mark.parametrize("level", [None, "low", "high"])
def test_launch_uses_fresh_owner_and_preserves_exact_saved_marker(tmp_path, monkeypatch, level):
    import agent_comms.native_pi as native

    fresh = create_fresh_private_session(
        tmp_path / "sessions", worktree=tmp_path, selected_thinking_level=level
    )
    before = fresh.path.read_bytes()
    monkeypatch.setattr(native, "_trusted_package", lambda _: Path("/bin/true"))
    launch = prepare_native_pi_rpc_launch(
        tmp_path,
        worktree=tmp_path,
        session_dir=fresh.path.parent,
        session_file=fresh.path,
        selected_thinking_level=level,
    )
    assert launch.session_file == fresh.path
    assert fresh.path.read_bytes() == before
    fresh.verify_prewrite()
    if level is not None:
        for wrong in (None, "low" if level == "high" else "high"):
            with pytest.raises(NativePiUnavailable, match="first-start token"):
                FreshPrivateSession.require_launch_header(fresh.path, wrong)
    else:
        with pytest.raises(NativePiUnavailable, match="first-start token"):
            FreshPrivateSession.require_launch_header(fresh.path, "high")


def test_duplicate_input_ids_are_not_silently_projected_away():
    entries = tuple(
        NativeEntry.from_evidence(
            {
                "type": "message",
                "id": entry_id,
                "message": {"role": "user", "inputId": "a" * 32, "inputDigest": "b" * 64},
            }
        )
        for entry_id in ("first", "second")
    )
    with pytest.raises(NativePiUnavailable, match="ambiguous"):
        NativeEntry.tracked_users(entries)
