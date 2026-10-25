"""Typed values and prepared crash-test workers share their existing owners."""


from agent_comms.repo import Checkout
import os
from multiprocessing.context import ForkServerContext
from pathlib import Path

from agent_comms.audience_manifest import FrozenRecipient, freeze_audience
from agent_comms.bus import PrivateRecord
from agent_comms.bus_publication import Delivered, KeyedPublication
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.envelope_claim_transitions import UnscopedClaims
from agent_comms.field_codec import ClaimId, InputId, RootId, ThreadLookup
from agent_comms.messages import ClaimMessage, ConversationRole, InfoKind, Message
from agent_comms.response_acknowledgement import (
    KeyedResponseAcknowledgement,
    canonical_publication_key,
    public_envelope_digest,
)
from agent_comms.threads import Thread
from agent_comms.wake import OrdinaryControl, resolve_wake_cohort

publication_processes = ForkServerContext()
publication_processes.set_forkserver_preload([ClaimMessage.__module__.partition(".")[0]])
SENDER_LOOKUP = ThreadLookup("a" * 32)
RECIPIENT_LOOKUP = ThreadLookup("b" * 32)
ROOT_ID = RootId("d" * 32)
OWNER_LOOKUP = ThreadLookup("c" * 32)
INPUT_ID = InputId("e" * 32)
FIRST_INPUT_ID, SECOND_INPUT_ID, THIRD_INPUT_ID = (InputId(letter * 32) for letter in "abc")
UNRELATED_LOOKUP = ThreadLookup("f" * 32)
RENAMED_OWNER_LOOKUP = ThreadLookup("d" * 32)
RENAMED_RECIPIENT_LOOKUP = ThreadLookup("e" * 32)


def prepare_native_comms(comms: Comms) -> Comms:
    """Validate native-root authority before installing data or opening a coordinator."""
    comms.registry.prepare_native_root()
    return comms


def context_comms(directory: Path) -> Comms:
    """The shared native-context dataset, built through canonical bus/guard owners."""
    comms = prepare_native_comms(wire(directory / "wire"))
    for tag in ("team", "else"):
        comms.channels.create_tag(tag)
    process = ProcessIdentity.capture(os.getpid())
    for name, tags, stamp in (
        ("sender", {"team"}, 17001.0),
        ("Alice", {"team"}, 17002.0),
        ("Bob", {"team"}, 17003.0),
        ("outsider", {"else"}, 17004.0),
    ):
        comms.registry.declare(Thread(
            name, frozenset(tags), Checkout(directory), process_identity=process, created_at=stamp))
    return comms


def record_from(
    declaration, message, *,
    sender_lookup: ThreadLookup = SENDER_LOOKUP,
    recipient_lookup: ThreadLookup = RECIPIENT_LOOKUP,
):
    capabilities = {}
    if issubclass(declaration, Delivered):
        audience = freeze_audience(
            message,
            (FrozenRecipient(recipient_lookup, message.target),),
            "c" * 64,
            sender_lookup=sender_lookup,
            sender_name=message.sender,
        )
        message = audience
        capabilities.update(
            decisions=resolve_wake_cohort(
                message, control=OrdinaryControl
            ),
            control=OrdinaryControl.declared_name,
        )
    if issubclass(declaration, KeyedPublication):
        capabilities["acknowledgement"] = KeyedResponseAcknowledgement(
            ROOT_ID,
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
                generation=ClaimId("f" * 32),
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
