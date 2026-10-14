from pathlib import Path
from types import SimpleNamespace

from agent_comms.native_attestation import KnownSessionAttestation, PendingAttestation
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.selected_session import EnrolledSelectedSession, SavedSelectedSession, SelectedSession
from agent_comms.tracked_turn import TrackedTurnSession


def test_only_saved_tracked_session_uses_known_identity(tmp_path: Path):
    identity = NativeSessionIdentity("saved-id", str(tmp_path / "saved.jsonl"))
    pending = PendingAttestation(identity)
    saved = SavedSelectedSession(tmp_path, identity=identity)

    known = saved.tracked_attestation(pending)

    assert isinstance(known, KnownSessionAttestation)
    assert known.require_identity() == identity
    assert SelectedSession(tmp_path).tracked_attestation(pending) is pending
    enrolled = object.__new__(EnrolledSelectedSession)
    assert enrolled.tracked_attestation(pending) is pending


async def test_saved_tracked_attestation_sends_no_get_state(tmp_path: Path):
    session_file = tmp_path / "saved.jsonl"
    identity = NativeSessionIdentity("saved-id", str(session_file))
    turn = object.__new__(TrackedTurnSession)
    turn.native = SimpleNamespace(attestation=PendingAttestation(identity))
    turn.launch = SimpleNamespace(
        session=SavedSelectedSession(tmp_path, identity=identity)
    )

    await turn.attest()

    assert isinstance(turn.native.attestation, KnownSessionAttestation)
    assert turn.active_session_file == session_file
