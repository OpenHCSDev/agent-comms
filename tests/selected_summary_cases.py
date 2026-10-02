"""Typed source fixtures for tests of journal and native behavior."""


import os
from dataclasses import replace

from agent_comms.child_process import ProcessIdentity
from agent_comms.input_attempt import ReservedInput
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.selected_source import ManualSource, SelectedAdmissionSource, SessionRevision
from agent_comms.selected_summary_admission import SelectedAdmissionIdentity
from agent_comms.text_digest import TextDigest
from agent_comms.thread_identity import ThreadIncarnation, TurnId


def manual_source(session, owner="owner", *, incarnation=None):
    return FieldCodec.encode(manual_source_value(session, owner, incarnation=incarnation))


def manual_source_value(session, owner="owner", *, incarnation=None):
    return ManualSource(
        owner=ProcessIdentity.capture(os.getpid()),
        incarnation=incarnation or ThreadIncarnation(owner, 1.0),
        turn=TurnId("turn"),
        reserved_revision=SessionRevision.observe(str(session)).require_available(),
    )


def manual_summary_source(
    session, owner="owner", *, incarnation=None,
    selected=SelectedModel("fixture", "model", 1000),
    settings=PiCompactionSettings(100, 100),
    retained=RetainedTaskFacts(()),
):
    """The declaration owns the complete current journal fixture shape.

    These empty retained facts are explicit for neutral reservation/fresh-file
    controls, not a production default or proof of actual task retention.
    """
    return FieldCodec.encode(
        manual_summary_record(
            session, owner, incarnation=incarnation, selected=selected,
            settings=settings, retained=retained,
        )
    )


def manual_summary_record(
    session, owner="owner", *, incarnation=None,
    selected=SelectedModel("fixture", "model", 1000),
    settings=PiCompactionSettings(100, 100),
    retained=RetainedTaskFacts(()),
) -> SelectedSummarySource:
    """Declared source record; the wire fixture above encodes this same value."""
    return SelectedSummarySource(
        manual_source_value(session, owner, incarnation=incarnation),
        selected, settings, retained,
    )


def summary_source(
    source, *, selected=SelectedModel("fixture", "model", 1000),
    settings=PiCompactionSettings(100, 100), retained=RetainedTaskFacts(()),
):
    return FieldCodec.encode(SelectedSummarySource(
        source=source, selected=selected, settings=settings, retained=retained,
    ))


def admission_identity(
    session, *, text, key, turn, owner="project", admission=1, original_text=None, incarnation=None
):
    digest = TextDigest.of(text)
    return SelectedAdmissionIdentity(
        source=SelectedAdmissionSource(
            incarnation=incarnation or ThreadIncarnation(owner, 1.0),
            owner=ProcessIdentity.capture(os.getpid()),
            turn=TurnId(turn),
            originals=(ReservedInput(
                key=key, sequence=None, owner=owner, admission=admission,
                target=owner, source_text=text if original_text is None else original_text,
            ),),
            admission_generation=admission,
            correction_witness=f"{admission}:{digest.value}",
            input_digest=digest,
            reserved_revision=SessionRevision.observe(str(session)).require_available(),
        ),
        session_revision=SessionRevision.observe(str(session)).require_available(),
    )


def refresh_source(envelope, session):
    """Recapture only the reserved revision through the current declared owner."""
    source = FieldCodec.decode(SelectedSummarySource, envelope)
    updated = replace(source, source=replace(source.source,
        reserved_revision=SessionRevision.observe(str(session)).require_available()))
    envelope.clear()
    envelope.update(FieldCodec.encode(updated))


def native_intent(session, *, owner="owner", selected=None, retained=RetainedTaskFacts(())):
    """Neutral journal controls through the actual intent/source declarations.

    Uses this fixture's physical file revision. These supplied journal controls
    are not a native writer grant, backend observation or live acceptance proof.
    A selected link is the caller's original SelectedCommitReference unchanged.
    """
    from pathlib import Path
    from agent_comms.compaction_source import CompactionSource
    from agent_comms.native_compaction_request import NativeIntent, NativeSummaryPayload
    from agent_comms.native_revision_text import NativeRevisionText
    from agent_comms.owner_compaction_gate import OwnerCompactionAttestation
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.private_path import FileRevision

    session = Path(session).resolve(strict=True)
    witness = NativeWitness(
        "fixture-session", str(session), "fixture-leaf", "fixture-kept",
        NativeRevisionText.encode(FileRevision.from_stat(session.stat())),
    )
    payload = NativeSummaryPayload(summary="private journal fixture summary", tokens_before=0)
    intent = NativeIntent(witness, payload.payload_digest(witness), payload.metadata_digest())
    attestation = OwnerCompactionAttestation(
        owner, 1, "turn", None, None, str(session), witness.leaf_id, witness.revision, None,
    )
    source = CompactionSource(
        witness, str(session.parent), owner, 1, "turn", None, None,
        "fixture-bus-revision", "fixture-input-revision", retained,
    )
    return intent, attestation, source, selected
