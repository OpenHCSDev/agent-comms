"""Named refusal behavior through the rule family and the real input ledger."""



import pytest

from agent_comms.input_key import AcpInputKey
from agent_comms.field_codec import FieldCodec, InputId, TurnId
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.session_revision import SessionRevision
from agent_comms.thread_identity import RecordedThreadIncarnation
from stored_inputs import stored_inputs


FIRST_KEY = AcpInputKey(InputId.mint())
SECOND_KEY = AcpInputKey(InputId.mint())


def test_session_observation_preserves_source_and_distinguishes_unreadable_proof(tmp_path):
    import sqlite3

    from agent_comms.session_revision import (
        MissingInputProofRevision,
        PresentInputProofRevision,
        SessionRevisionUnavailable,
    )

    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    files = NativeSessionFiles.for_identity(
        NativeSessionIdentity("revision-test", str(session)),
        root=tmp_path / "bus", owner=RecordedThreadIncarnation("owner", 1.0),
    )
    absent = SessionRevision.observe(files).require_available()
    assert isinstance(absent.input_proof, MissingInputProofRevision)
    assert absent.current(files)
    assert absent.native.size == session.stat().st_size
    proof = files.input_proof
    files.prepare_private_directory(proof.parent)
    with sqlite3.connect(proof) as db:
        db.execute("CREATE TABLE original_context (input_id TEXT)")
    present = SessionRevision.observe(files).require_available()
    assert isinstance(present.input_proof, PresentInputProofRevision)
    assert not absent.current(files)
    assert not present.matches(absent)
    assert FieldCodec.decode(SessionRevision, FieldCodec.encode(present)) == present
    session.write_text("{}\n{}\n")
    appended = SessionRevision.observe(files).require_available()
    assert appended.input_proof == present.input_proof
    assert not present.current(files)
    encoded = FieldCodec.encode(present)
    encoded["native"]["size"] = True
    with pytest.raises(ValueError):
        FieldCodec.decode(SessionRevision, encoded)
    with pytest.raises(ValueError):
        FieldCodec.decode(SessionRevision, [[1, 2, 3, 4, 5], None])
    # A real stat error must not be called absent or match a reserved source.
    proof.rename(tmp_path / "original-proof.sqlite3")
    proof.symlink_to(proof.name)
    unavailable = SessionRevision.observe(files)
    assert not unavailable.matches(present)
    with pytest.raises(SessionRevisionUnavailable) as error:
        unavailable.require_available()
    assert isinstance(error.value.__cause__, OSError)
    session.unlink()
    assert not present.current(files)


def test_plural_originals_preserve_order_and_bind_without_partial_transition(tmp_path):
    owner = "project"
    inputs = InputDispositions(tmp_path)
    keys = (FIRST_KEY, SECOND_KEY)
    for key, text in zip(keys, ("first", "second"), strict=True):
        inputs.record(key, seq=None, owner=owner, claim_scope=1, target=owner, text=text)
    assert tuple(row.source_text for row in stored_inputs(inputs.root).originals(keys)) == ("first", "second")
    assert inputs.bind(
        keys[1],
        claim_scope=1,
        turn_id=TurnId('d9298a10d1b0735837dc4bd85dac641b'),
        native_id=InputId("a" * 32),
        text="second",
    )
    before = stored_inputs(inputs.root)
    assert not inputs.bind_originals(
        keys,
        claim_scope=1,
        turn_id=TurnId('9b473845f684444f80a26586ae298d2b'),
        native_id=InputId("b" * 32),
        text="first\n\nsecond",
    )
    assert stored_inputs(inputs.root) == before
    assert stored_inputs(inputs.root).lookup(keys[0]).accepts_reservation
