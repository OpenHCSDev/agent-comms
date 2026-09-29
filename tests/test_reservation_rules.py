"""Named refusal behavior through the real input ledger and journal boundary."""

import os
from dataclasses import replace

import pytest

from agent_comms.backend import _session_revision
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
from agent_comms.selected_source import ManualSource, SelectedSource
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation, TurnId
from selected_summary_cases import admission_identity


def test_rule_family_names_actual_refusals_and_discovers_new_policy(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    source = admission_identity(session, text="original", key="acp:original", turn="before").source
    inputs = InputDispositions(tmp_path / InputDispositions.filename)
    inputs.record(
        source.ingress_key,
        seq=None,
        owner="project",
        admission=1,
        target="project",
        text="original",
    )
    row = inputs.read().lookup(source.ingress_key)
    check = InterruptedInputCheck(
        source=source,
        revision=source.reserved_revision,
        row=row,
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
        pending_input_key=source.ingress_key,
    )
    commit.require_valid()
    inputs.bind(
        source.ingress_key, admission=1, turn_id="before", native_id="a" * 32, text="original"
    )
    cases = {
        "owner_changed": replace(check, incarnation=ThreadIncarnation("project", 2.0)),
        "process_changed": replace(
            commit, owner=ProcessIdentity(source.owner.pid, source.owner.start_time + 1)
        ),
        "turn_changed": replace(commit, turn=TurnId("another")),
        "turn_still_active": replace(check, turn=source.turn),
        "session_changed": replace(check, revision=None),
        "ingress_changed": replace(commit, pending_input_key="acp:another"),
        "missing_input": replace(check, row=inputs.read().lookup("acp:missing")),
        "already_sent": InputReservationCheck(
            source=source,
            revision=source.reserved_revision,
            row=inputs.read().lookup(source.ingress_key),
        ),
        "native_binding_exists": replace(check, row=inputs.read().lookup(source.ingress_key)),
        "input_owner_changed": replace(check, row=replace(row, owner="another")),
        "admission_changed": replace(check, row=replace(row, admission=2)),
        "content_changed": replace(check, row=replace(row, source_text="changed")),
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
        reserved_revision=_session_revision(str(session)),
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
