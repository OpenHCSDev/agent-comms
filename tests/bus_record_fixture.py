"""Typed values and prepared crash-test workers share their existing owners."""


import json
import os
import sys
from multiprocessing.context import ForkServerContext
from pathlib import Path

from agent_comms.comms import Comms, wire
from agent_comms.coordination.response_acknowledgement import (
    KeyedResponseAcknowledgement,
    canonical_publication_key,
    public_envelope_digest,
)
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_runtime import RestoredRuntime
from agent_comms.execution.native_arguments import NativeArguments
from agent_comms.messaging.audience_manifest import freeze_audience
from agent_comms.messaging.bus import PrivateRecord
from agent_comms.messaging.bus_publication import Delivered, KeyedPublication
from agent_comms.messaging.envelope_claim_transitions import UnscopedClaims
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.messages import ClaimMessage, ConversationRole, InfoKind, Message
from agent_comms.messaging.wake import OrdinaryControl, resolve_wake_cohort
from agent_comms.nominal.field_codec import ClaimId, InputId, RootId, ThreadLookup
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread

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
    comms.registry.store.prepare_native_root()
    return comms


def configure_local_native(directory: Path, monkeypatch, *, provider: str, port: int,
                           models: tuple[str, ...]) -> None:
    """Point native Pi at a local provider, so a real host starts with no credential or network."""
    directory.mkdir(mode=0o700)
    (directory / "models.json").write_text(json.dumps({"providers": {provider: {
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "api": "openai-completions",
        "models": [{"id": name, "name": name, "contextWindow": 32768, "maxTokens": 2048}
                   for name in models],
    }}}))
    (directory / "auth.json").write_text(json.dumps({provider: {"type": "api_key", "key": "local-only"}}))
    (directory / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False},
        "retry": {"enabled": False, "maxRetries": 0},
    }))
    for key, value in {
        "PI_CODING_AGENT_DIR": str(directory),
        "AGENT_COMMS_NATIVE_CONFIG_DIR": str(directory),
        "AGENT_COMMS_AGENT_BIN": str(Path(sys.executable).with_name("pi-comms-native")),
        "PYTHONPATH": os.pathsep.join(
            str(Path(__file__).parents[1] / name) for name in ("src", "tests", "")
        ),
        "AGENT_COMMS_AGENT_MODELS": ",".join(f"{provider}/{name}" for name in models),
        "AGENT_COMMS_AGENT_ARGS": "--offline --no-extensions --no-skills --no-context-files",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("PI_PROMPT", "PI_PARENT_ID", "AGENT_COMMS_THREAD"):
        monkeypatch.delenv(key, raising=False)


def start_source_owner(comms: Comms, name: str, runtime: RestoredRuntime):
    """Use the shared interpreter, but retain this fixture's exact source imports.

    Ordinary owner starts intentionally discard PYTHONPATH and would launch
    shared main, making the branch client a read-only other-build attachment.
    The retained restart request uses this same explicit source-runtime policy.
    """
    thread = comms.registry.require(name)
    with comms.owners.launching_owner(
        thread, NativeArguments.default_binary,
        launch_runtime=runtime,
    ) as owner:
        return owner


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
        comms.registry.declare(NativeThread(
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
