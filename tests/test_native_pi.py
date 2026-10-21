"""Strict native Pi proof-reader tests; real RPC/provider exercise is separate."""

from __future__ import annotations

from agent_comms.native_package import NativePackageError, VerifiedNativePackage
import json
import os
import sys
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_arguments import SelectedModel
from agent_comms.native_startup import NATIVE_STARTUP_POLICY
from agent_comms.native_pi import (
    NativeContextProof,
    NativePiRpcLaunch,
    NativePiTerminalFailure,
    NativePiUnavailable,
)
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.native_transcript import NativeTranscript
from agent_comms.selected_session import SelectedSession

INPUT_ID = "a" * 32
DIGEST = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="native Pi proof and owner-only UID checks require POSIX"
)


def _evidence(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    session = sessions / "test.jsonl"
    entries = [
        {"type": "session", "timestamp": "2026-10-01T00:00:00.000Z", "id": "sid", "version": 3},
        {
            "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
            "id": "entry",
            "message": {
                "role": "user",
                "inputId": INPUT_ID,
                "inputDigest": DIGEST,
                "content": [{"type": "text", "text": "separate from its identity"}],
            },
        },
    ]
    journal = [
        {
            "schema": 2,
            "type": "context_committed",
            "sessionId": "sid",
            "inputId": INPUT_ID,
            "sessionEntryId": "entry",
            "requestGeneration": 1,
            "llmContextDigest": "c" * 64,
            "committedAtMs": 1_791_964_800_001,
        }
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in entries))
    proof = Path(str(session) + ".input-proof")
    write_proof_rows(session, journal)
    session.chmod(0o600)
    proof.chmod(0o600)
    return session


def test_read_only_journal_parser_is_not_recovery_authority(tmp_path: Path) -> None:
    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    first = NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)
    second = NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)
    assert first == second
    assert first.session_id == "sid"
    assert first.session_entry_id == "entry"
    assert first.request_generation == 1
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(session, "d" * 32, session_entry_start=input_record.start)


@pytest.mark.parametrize(
    "damage",
    [
        "malformed_database",
        "truncated_database",
        "world_readable",
        "wrong_entry",
        "wrong_session",
        "symlink",
        "hardlink",
        "removed_constraint",
    ],
)
def test_corrupt_or_redirected_journal_cannot_assert_context(tmp_path: Path, damage: str) -> None:
    import sqlite3
    from agent_comms.native_pi import NativeContextJournal

    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    journal = Path(str(session) + ".input-proof")
    if damage == "malformed_database":
        journal.write_bytes(b"not a SQLite database")
    elif damage == "truncated_database":
        journal.write_bytes(journal.read_bytes()[:-4096])
    elif damage == "world_readable":
        journal.chmod(0o644)
    elif damage in {"wrong_entry", "wrong_session"}:
        rows = read_proof_rows(session)
        rows[0]["sessionEntryId" if damage == "wrong_entry" else "sessionId"] = "other"
        write_proof_rows(session, rows)
    elif damage in {"symlink", "hardlink"}:
        replacement = tmp_path / "replacement"
        journal.rename(replacement)
        if damage == "symlink":
            journal.symlink_to(replacement)
        else:
            os.link(replacement, journal)
    elif damage == "removed_constraint":
        with sqlite3.connect(journal) as db:
            db.execute(f"DROP TRIGGER {NativeContextJournal.declared_name}_append")
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)


def test_native_journal_declaration_preserves_current_envelope_and_rejects_bad_facts(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_pi import NativeContextJournal

    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    journal = Path(str(session) + ".input-proof")
    original = journal.read_bytes()
    row = read_proof_rows(session)[0]
    decoded = FieldCodec.decode(NativeContextJournal, row)
    assert FieldCodec.encode(decoded) == row
    assert decoded.at(session, session_entry_start=input_record.start) == NativeContextProof.read_evidence(
        session, INPUT_ID, session_entry_start=input_record.start
    )
    for changed in (
        {**row, "schema": True},
        {**row, "schema": 1},
        {**row, "committedAtMs": 0},
        {**row, "type": "input_committed"},
        {**row, "requestGeneration": True},
        {**row, "requestGeneration": 0},
        {**row, "inputId": "not-an-input"},
        {**row, "llmContextDigest": "not-a-digest"},
        {**row, "extra": "not-native"},
        {key: value for key, value in row.items() if key != "schema"},
    ):
        with pytest.raises((ValueError, TypeError)):
            FieldCodec.decode(NativeContextJournal, changed)
    assert NativeContextProof.read_evidence(
        session, INPUT_ID, session_entry_start=input_record.start
    ) == decoded.at(session, session_entry_start=input_record.start)
    assert journal.read_bytes() == original


def test_proof_reader_rejects_session_id_duplicate_or_untrusted_ancestor(tmp_path: Path) -> None:
    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    data = session.read_text()
    user = json.loads(data.splitlines()[1])
    session.write_text(data + json.dumps({**user, "id": "another"}) + "\n")
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)
    session.write_text(data)
    session.parent.chmod(0o777)
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)


def test_obsolete_copied_fork_is_rejected_before_process() -> None:
    old = os.environ.get("AC_NATIVE_OLD_COPIED_PACKAGE")
    if not old:
        pytest.skip("Set AC_NATIVE_OLD_COPIED_PACKAGE for copied-fork negative")
    with pytest.raises(NativePackageError):
        VerifiedNativePackage.acquire(Path(old))


@pytest.mark.parametrize("provider,model", [("", "model"), ("provider", ""), ("--bad", "model")])
def test_missing_native_model_fails_before_any_session_side_effect(tmp_path: Path, monkeypatch, provider: str, model: str, *, native_runtime_package) -> None:
    with pytest.raises(IdentityConflict):
        NativePiRpcLaunch.tracked(
            native_runtime_package,
            root=tmp_path, worktree=tmp_path,
            session=SelectedSession(tmp_path / "sessions"),
            model=SelectedModel(provider, model),
        )
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize(
    ("stop_reason", "detail"),
    [
        ("error", "provider failed"),
        ("length", "Model output limit reached"),
        ("aborted", "provider failed"),
    ],
)
async def test_failed_stop_reason_records_failure_with_its_terminal(stop_reason, detail):
    from agent_comms.pi_events import PiEvent
    from agent_comms.turn_failure import ModelRequestFailed
    from agent_comms.turn_output import TurnOutput
    from agent_comms.turn_usage import UsageAccount
    from agent_comms.turn_watchdog import DEFAULT_MODEL_WAIT, ProgressWatchdog

    event = PiEvent.from_wire({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [],
            "stopReason": stop_reason,
            "errorMessage": detail if stop_reason != "length" else None,
        },
    })
    turn = object.__new__(TrackedTurnSession)
    output = turn.output = TurnOutput()
    turn.usage, turn.explicit_interrupt = UsageAccount(), False
    turn.watchdog = ProgressWatchdog(DEFAULT_MODEL_WAIT, 1, 1, NATIVE_STARTUP_POLICY)
    [update async for update in event.message.stop_reason.apply(turn, event.message)]

    assert output.declared_failure == ModelRequestFailed(detail)
    with pytest.raises(NativePiTerminalFailure) as caught:
        output.raise_if_failed(None, SelectedModel("provider", "model"))
    assert caught.value.failure == output.declared_failure


def test_provider_failure_notice_does_not_broadcast_untrusted_error_body(tmp_path):
    from agent_comms.native_pi import NativeContextProof
    from agent_comms.turn_failure import ModelRequestFailed

    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    context = NativeContextProof(
        INPUT_ID, "sid", "entry", 1, DIGEST, 1_791_964_800_001, session,
        session_entry_start=input_record.start,
    )
    failed = NativePiTerminalFailure(
        "SECRET provider response including prompt data", context, SelectedModel("provider", "model"),
        ModelRequestFailed("SECRET provider response including prompt data"),
    )
    assert failed.public_message == "provider/model: request failed."
    assert "SECRET" not in failed.public_message
    usage = NativePiTerminalFailure(
        "Codex error: The usage limit has been reached", context, SelectedModel("openai-codex", "gpt-6-sol"),
        ModelRequestFailed("Codex error: The usage limit has been reached"),
    )
    assert "The usage limit has been reached" in usage.public_message


def test_recorded_context_remains_verifiable_after_later_tool_rounds(tmp_path):
    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    recorded = NativeContextProof.read_evidence(session, INPUT_ID, session_entry_start=input_record.start)
    journal = Path(str(session) + ".input-proof")
    first = read_proof_rows(session)[0]
    later = {**first, "requestGeneration": 2, "llmContextDigest": "d" * 64}
    import sqlite3
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_pi import NativeContextJournal

    with sqlite3.connect(journal) as db:
        FieldCodec.decode(NativeContextJournal, later).insert(db)
    assert NativeContextProof.read_evidence(
        session, INPUT_ID, session_entry_start=input_record.start
    ).request_generation == 2
    assert NativeContextProof.read_evidence(
        session, INPUT_ID, request_generation=1, session_entry_start=input_record.start
    ) == recorded
    assert recorded.corroborates_input(INPUT_ID)
    assert not recorded.corroborates_input("e" * 32)

    from dataclasses import replace
    from agent_comms.native_pi import _verify_context
    from agent_comms.pi_events import ContextCommitted, InputCommitted

    input_event = InputCommitted(
        input_id=INPUT_ID, session_id="sid", session_entry_id="entry",
        session_entry_start=input_record.start,
    )
    context_event = ContextCommitted.from_wire({key: value for key, value in first.items() if key != "schema"})
    assert _verify_context(session, INPUT_ID, "sid", input_event, context_event) == recorded
    with pytest.raises(NativePiUnavailable):
        _verify_context(
            session, INPUT_ID, "sid", input_event,
            replace(context_event, llm_context_digest="d" * 64),
        )
    with pytest.raises(NativePiUnavailable):
        NativeContextProof.read_evidence(
            session, INPUT_ID, request_generation=3, session_entry_start=input_record.start
        )


def test_native_proof_database_enforces_generation_and_digest_without_scanning(tmp_path):
    import sqlite3
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_pi import NativeContextJournal

    session = _evidence(tmp_path)
    _header, input_record = NativeTranscript(session).forward(0, session.stat().st_size)
    first = read_proof_rows(session)[0]
    with sqlite3.connect(Path(str(session) + ".input-proof")) as db:
        later = FieldCodec.decode(NativeContextJournal, {**first, "requestGeneration": 2})
        later.insert(db)
        for invalid in (
            first,
            {**first, "inputId": "d" * 32},
            {**first, "inputId": "d" * 32, "requestGeneration": 2, "llmContextDigest": "e" * 64},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                FieldCodec.decode(NativeContextJournal, invalid).insert(db)
    assert (
        NativeContextProof.read_evidence(
            session, INPUT_ID, request_generation=1, session_entry_start=input_record.start
        ).request_generation
        == 1
    )


def test_proof_observation_rejects_file_replacement(tmp_path):
    from agent_comms.native_pi import NativeContextJournal

    session = _evidence(tmp_path)
    journal = Path(str(session) + ".input-proof")
    with pytest.raises(NativePiUnavailable):
        with NativeContextJournal.open_evidence(session) as db:
            assert NativeContextJournal.for_input(db, INPUT_ID).input_id == INPUT_ID
            changed = journal.with_name("replacement")
            changed.write_bytes(journal.read_bytes())
            changed.chmod(0o600)
            changed.replace(journal)
