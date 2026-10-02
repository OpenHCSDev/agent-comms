"""Named refusal behavior through the real input ledger and journal boundary."""


import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.reservation_rules import (
    CommitReservationCheck,
    InputReservationCheck,
    InterruptedInputCheck,
    ReservationCheck,
    ReservationRule,
    ReservationViolationError,
)
from agent_comms.selected_source import ManualSource, SelectedSource, SessionRevision
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation, TurnId
from selected_summary_cases import admission_identity


def test_session_observation_preserves_source_and_distinguishes_unreadable_proof(tmp_path):
    import sqlite3

    from agent_comms.selected_source import (
        MissingInputProofRevision,
        PresentInputProofRevision,
        SessionRevisionUnavailable,
    )

    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    absent = SessionRevision.observe(str(session)).require_available()
    assert isinstance(absent.input_proof, MissingInputProofRevision)
    assert absent.current(str(session))
    observed = session.stat()
    assert absent.native.size == session.stat().st_size
    proof = session.with_name(session.name + ".input-proof")
    with sqlite3.connect(proof) as db:
        db.execute("CREATE TABLE original_context (input_id TEXT)")
    present = SessionRevision.observe(str(session)).require_available()
    assert isinstance(present.input_proof, PresentInputProofRevision)
    assert not absent.current(str(session))
    assert not present.same_input_proof(absent)
    assert FieldCodec.decode(SessionRevision, FieldCodec.encode(present)) == present
    session.write_text("{}\n{}\n")
    appended = SessionRevision.observe(str(session)).require_available()
    assert appended.same_input_proof(present)
    present.require_native_cut(str(session), appended.native.size)
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


def test_rule_family_names_actual_refusals_and_discovers_new_policy(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    source = admission_identity(session, text="original", key="acp:original", turn="before").source
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    inputs.record(
        source.originals[0].key,
        seq=None,
        owner="project",
        admission=1,
        target="project",
        text="original",
    )
    row = inputs.read().lookup(source.originals[0].key)
    check = InterruptedInputCheck(
        source=source,
        revision=source.reserved_revision,
        rows=(row,),
        incarnation=source.incarnation,
        turn=TurnId("now"),
    )
    check.require_valid()
    commit = CommitReservationCheck(
        source=source,
        revision=source.reserved_revision,
        incarnation=source.incarnation,
        turn=source.turn,
        owner=source.owner,
        pending_input_keys=source.pending_input_keys,
    )
    commit.require_valid()
    inputs.bind(
        source.originals[0].key, admission=1, turn_id="before", native_id="a" * 32, text="original"
    )
    cases = {
        "owner_changed": replace(check, incarnation=ThreadIncarnation("project", 2.0)),
        "process_changed": replace(
            commit, owner=ProcessIdentity(source.owner.pid, source.owner.start_time + 1)
        ),
        "turn_changed": replace(commit, turn=TurnId("another")),
        "turn_still_active": replace(check, turn=source.turn),
        "session_changed": replace(check, revision=SessionRevision.observe(str(tmp_path / "missing"))),
        "ingress_changed": replace(commit, pending_input_keys=("acp:another",)),
        "missing_input": replace(check, rows=(inputs.read().lookup("acp:missing"),)),
        "already_sent": InputReservationCheck(
            source=source,
            revision=source.reserved_revision,
            rows=(inputs.read().lookup(source.originals[0].key),),
        ),
        "native_binding_exists": replace(check, rows=(inputs.read().lookup(source.originals[0].key),)),
        "input_owner_changed": replace(check, rows=(replace(row, owner="another"),)),
        "admission_changed": replace(check, rows=(replace(row, admission=2),)),
        "content_changed": replace(check, rows=(replace(row, source_text="changed"),)),
    }
    for declaration in ReservationRule.members_with(ReservationRule):
        if not issubclass(declaration.check_type, ReservationCheck):
            continue
        with pytest.raises(ReservationViolationError) as error:
            cases[declaration.declared_name].require_valid()
        assert type(error.value.rule) is declaration
        assert declaration.declared_name in str(error.value)

    class PolicyCheck(ReservationCheck):
        pass

    # A concrete declaration is the entire dispatch extension; no roster edit.
    monkeypatch.setattr(ReservationRule, "__registry__", dict(ReservationRule.__registry__))

    class NewPolicyRule(ReservationRule):
        check_type = PolicyCheck
        explanation = "New policy is enforced."

        def violated(self, check):
            return True

    with pytest.raises(ReservationViolationError, match="new_policy"):
        PolicyCheck(source=source, revision=source.reserved_revision).require_valid()


def test_source_family_roundtrips_nested_values_and_requires_declared_kind(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    admission = admission_identity(session, text="original", key="acp:original", turn="turn").source
    manual = ManualSource(
        owner=ProcessIdentity.capture(os.getpid()),
        incarnation=admission.incarnation,
        turn=TurnId("turn"),
        reserved_revision=SessionRevision.observe(str(session)).require_available(),
    )
    assert not hasattr(manual, "ingress_key")
    manual.interrupted_check(
        manual.reserved_revision, InputDocument(), manual.incarnation, TurnId("later")
    ).require_valid()
    values = {value.declared_name: value for value in (manual, admission)}
    for name in SelectedSource.names():
        encoded = FieldCodec.encode(values[name])
        assert FieldCodec.decode(SelectedSource, encoded) == values[name]
        del encoded["kind"]
        with pytest.raises(ValueError, match="family kind"):
            FieldCodec.decode(SelectedSource, encoded)
    assert FieldCodec.decode(TextDigest, FieldCodec.encode(admission.input_digest)).matches(
        "original"
    )


def test_plural_originals_preserve_order_and_bind_without_partial_transition(tmp_path):
    from agent_comms.selected_source import SelectedAdmissionSource
    from agent_comms.threads import Thread

    session = tmp_path / "saved.jsonl"
    session.write_text("{}\n")
    owner = Thread("project", frozenset(), str(tmp_path),
                   process_identity=ProcessIdentity.capture(os.getpid()))
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    keys = ("acp:first", "acp:second")
    for key, text in zip(keys, ("first", "second"), strict=True):
        inputs.record(key, seq=None, owner=owner.name, admission=1, target=owner.name, text=text)
    revision = SessionRevision.observe(str(session)).require_available()
    source = SelectedAdmissionSource.capture(
        owner, TurnId("turn"), 1, keys, inputs.read(), "first\n\nsecond", revision
    )
    assert source.originals == inputs.read().originals(keys)
    assert tuple(row.source_text for row in source.originals) == ("first", "second")
    assert FieldCodec.decode(SelectedSource, FieldCodec.encode(source)) == source
    assert source.pending_input_keys == keys
    assert not source.matches_pending_inputs(tuple(reversed(keys)))
    assert inputs.bind(keys[1], admission=1, turn_id="other", native_id="a" * 32, text="second")
    before = inputs.read()
    assert not inputs.bind_originals(keys, admission=1, turn_id="turn", native_id="b" * 32,
                                     text="first\n\nsecond")
    assert inputs.read() == before
    assert inputs.read().lookup(keys[0]).accepts_reservation
