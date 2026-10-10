"""Named refusal behavior through the rule family and the real input ledger."""


from dataclasses import dataclass

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions
from agent_comms.reservation_rules import ReservationRule, ReservationViolationError, RuleCheck
from agent_comms.session_revision import SessionRevision


def test_session_observation_preserves_source_and_distinguishes_unreadable_proof(tmp_path):
    import sqlite3

    from agent_comms.session_revision import (
        MissingInputProofRevision,
        PresentInputProofRevision,
        SessionRevisionUnavailable,
    )

    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    absent = SessionRevision.observe(str(session)).require_available()
    assert isinstance(absent.input_proof, MissingInputProofRevision)
    assert absent.current(str(session))
    assert absent.native.size == session.stat().st_size
    proof = session.with_name(session.name + ".input-proof")
    with sqlite3.connect(proof) as db:
        db.execute("CREATE TABLE original_context (input_id TEXT)")
    present = SessionRevision.observe(str(session)).require_available()
    assert isinstance(present.input_proof, PresentInputProofRevision)
    assert not absent.current(str(session))
    assert not present.matches(absent)
    assert FieldCodec.decode(SessionRevision, FieldCodec.encode(present)) == present
    session.write_text("{}\n{}\n")
    appended = SessionRevision.observe(str(session)).require_available()
    assert appended.input_proof == present.input_proof
    assert not present.current(str(session))
    encoded = FieldCodec.encode(present)
    encoded["native"]["size"] = True
    with pytest.raises(ValueError):
        FieldCodec.decode(SessionRevision, encoded)
    with pytest.raises(ValueError):
        FieldCodec.decode(SessionRevision, [[1, 2, 3, 4, 5], None])
    # A real stat error must not be called absent or match a reserved source.
    proof.rename(tmp_path / "original-proof.sqlite3")
    proof.symlink_to(proof.name)
    unavailable = SessionRevision.observe(str(session))
    assert not unavailable.matches(present)
    with pytest.raises(SessionRevisionUnavailable) as error:
        unavailable.require_available()
    assert isinstance(error.value.__cause__, OSError)
    session.unlink()
    assert not present.current(str(session))


def test_new_rule_declaration_is_the_entire_dispatch_extension(monkeypatch):
    @dataclass(frozen=True)
    class PolicyCheck(RuleCheck):
        allowed: bool

    # A concrete declaration is the entire dispatch extension; no roster edit.
    monkeypatch.setattr(ReservationRule, "__registry__", dict(ReservationRule.__registry__))

    class NewPolicyRule(ReservationRule):
        check_type = PolicyCheck
        explanation = "New policy is enforced."

        def violated(self, check):
            return not check.allowed

    PolicyCheck(allowed=True).require_valid()
    with pytest.raises(ReservationViolationError, match="new_policy") as error:
        PolicyCheck(allowed=False).require_valid()
    assert type(error.value.rule) is NewPolicyRule


def test_plural_originals_preserve_order_and_bind_without_partial_transition(tmp_path):
    owner = "project"
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    keys = ("acp:first", "acp:second")
    for key, text in zip(keys, ("first", "second"), strict=True):
        inputs.record(key, seq=None, owner=owner, admission=1, target=owner, text=text)
    assert tuple(row.source_text for row in inputs.read().originals(keys)) == ("first", "second")
    assert inputs.bind(keys[1], admission=1, turn_id="other", native_id="a" * 32, text="second")
    before = inputs.read()
    assert not inputs.bind_originals(keys, admission=1, turn_id="turn", native_id="b" * 32,
                                     text="first\n\nsecond")
    assert inputs.read() == before
    assert inputs.read().lookup(keys[0]).accepts_reservation
