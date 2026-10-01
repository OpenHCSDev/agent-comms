"""Typed source fixtures for tests of journal and native behavior."""


import os
from dataclasses import replace

from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.selected_source import ManualSource, SelectedAdmissionSource, SelectedSource, SessionRevision
from agent_comms.selected_summary_admission import SelectedAdmissionIdentity
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation, TurnId


def manual_source(session, owner="owner", *, incarnation=None):
    return FieldCodec.encode(
        ManualSource(
            owner=ProcessIdentity.capture(os.getpid()),
            incarnation=incarnation or ThreadIncarnation(owner, 1.0),
            turn=TurnId("turn"),
            reserved_revision=SessionRevision.observe(str(session)).require_available(),
        )
    )


def admission_identity(
    session, *, text, key, turn, owner="project", admission=1, original_text=None, incarnation=None
):
    digest = TextDigest.of(text)
    return SelectedAdmissionIdentity(
        source=SelectedAdmissionSource(
            incarnation=incarnation or ThreadIncarnation(owner, 1.0),
            owner=ProcessIdentity.capture(os.getpid()),
            turn=TurnId(turn),
            ingress_key=key,
            admission_generation=admission,
            correction_witness=f"{admission}:{digest.value}",
            input_digest=digest,
            original_digest=TextDigest.of(text if original_text is None else original_text),
            reserved_revision=SessionRevision.observe(str(session)).require_available(),
        ),
        session_revision=SessionRevision.observe(str(session)).require_available(),
    )


def refresh_source(envelope, session):
    """Recapture only the reserved revision through the current declared owner."""
    source = FieldCodec.decode(SelectedSource, envelope["source"])
    envelope["source"] = FieldCodec.encode(
        replace(source, reserved_revision=SessionRevision.observe(str(session)).require_available())
    )
