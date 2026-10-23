"""Typed values and prepared crash-test workers share their existing owners."""

import os
from multiprocessing.context import ForkServerContext
from pathlib import Path

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import Bus, PrivateRecord
from agent_comms.comms import Comms
from agent_comms.child_process import ProcessIdentity
from agent_comms.bus_publication import Delivered, KeyedPublication
from agent_comms.envelope_claim_transitions import UnscopedClaims
from agent_comms.messages import ClaimMessage, ConversationRole, InfoKind, Message
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement, canonical_publication_key, public_envelope_digest,
)
from agent_comms.wake import MessageControl, resolve_wake_cohort, OrdinaryControl


publication_processes = ForkServerContext()
publication_processes.set_forkserver_preload([ClaimMessage.__module__.partition(".")[0]])


def prepare_native_comms(comms: Comms) -> Comms:
    """Validate native-root authority before installing data or opening a coordinator."""
    comms.registry.prepare_native_root()
    Bus.install(Bus.path_in(comms.root))
    return comms


def context_comms(directory: Path) -> Comms:
    """The shared native-context dataset, built through canonical bus/guard owners."""
    comms = prepare_native_comms(Comms(directory / "wire"))
    for tag in ("team", "else"):
        comms.channels.create_tag(tag)
    process = ProcessIdentity.capture(os.getpid())
    for name, tags, stamp in (
        ("sender", {"team"}, 17001.0),
        ("Alice", {"team"}, 17002.0),
        ("Bob", {"team"}, 17003.0),
        ("outsider", {"else"}, 17004.0),
    ):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(tags), str(directory), process_identity=process, created_at=stamp))
    return comms


def record_from(declaration, message, *, sender_lookup="a" * 32, recipient_lookup="b" * 32):
    capabilities = {}
    if issubclass(declaration, Delivered):
        audience = freeze_audience(
            message,
            (FrozenRecipient(recipient_lookup, message.target),),
            "c" * 64,
            sender_lookup=sender_lookup,
            sender_name=message.sender,
        )
        capabilities.update(
            audience=audience,
            decisions=resolve_wake_cohort(
                message, frozen_audience=audience, control=OrdinaryControl
            ),
            control=OrdinaryControl.declared_name,
        )
    if issubclass(declaration, KeyedPublication):
        capabilities["acknowledgement"] = KeyedResponseAcknowledgement(
            "d" * 32,
            "e" * 32,
            canonical_publication_key("e" * 32, message.target),
            public_envelope_digest(message.to_wire()),
        )
    if issubclass(declaration, ClaimMessage):
        return declaration.of(
            message,
            UnscopedClaims(
                owner=message.sender,
                incarnation="1",
                seq=message.seq,
                message_id=message.message_id,
                claims=("/owned/resource",),
                generation="f" * 32,
            ),
        )
    return declaration.from_message(message, **capabilities)


def append_reply(bus, sender, recipient, body="reply", *, role=ConversationRole(), answers=()):
    def build(seq, db):
        message = Message(sender.name, recipient.name, body, InfoKind, seq=seq, role=role,
                          answers=answers)
        return record_from(PrivateRecord, message,
                           sender_lookup=sender.lookup, recipient_lookup=recipient.lookup)
    return bus.append(build)
