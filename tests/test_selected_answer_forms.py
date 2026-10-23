"""How a selected turn's final answer becomes replies.

Real private bus, registry and coordination stores; the last test runs the
real Pi child against a local provider. Found on a live thread whose model
ended a direct-message turn in plain text: the turn was recorded as failed
and the answer was never delivered.
"""

from __future__ import annotations

from bus_record_fixture import prepare_native_comms

from conftest import publish_initial
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import InfoKind, Message
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.selected_source_batch import SelectedSource, SelectedSourceBatch

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private cohort claim_scope requires real /var/tmp"
)


@contextmanager
def alice_batch(*sent: tuple[str, str]):
    """Alice's selected sources for these (target, text) messages from sender."""
    with TemporaryDirectory(prefix="ac-answer-", dir="/var/tmp") as dirname:
        root = Path(dirname) / "wire"
        root.mkdir(mode=0o700)
        comms = prepare_native_comms(Comms(root))
        for name, created in (("sender", 17021.0), ("Alice", 17022.0)):
            comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), dirname, process_identity=ProcessIdentity.capture(os.getpid()), created_at=created))
        root_id = comms.registry.prepare_native_root().namespace
        messages = [publish_initial(comms, "sender", target, text) for target, text in sent]
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            deliveries = [comms.bus.bus.delivery(m.seq) for m in messages]
            for recipient in deliveries[0].audience.recipients:
                store.participants.register(
                    recipient.recipient_lookup, recipient.canonical_thread,
                    recipient.canonical_thread, committed=True,
                )
            for message in messages:
                accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            lookup = RecordedThreadIncarnation("fixture", comms.registry.require("Alice").created_at).lookup
            assignments = sealed_cohort_assignments(store, lookup)
            yield SelectedSourceBatch.first_route(tuple(
                SelectedSource(assignment.assignment_id, store.assignments, delivery)
                for assignment, delivery in zip(assignments, deliveries, strict=True)
            ))


def test_plain_answer_to_one_reply_route_is_that_routes_reply() -> None:
    with alice_batch(("Alice", "What did you find?")) as batch:
        assert batch.target == "sender"
        reply = batch.response_message("Blocked: the edit did not run.\n", "Alice")
        assert (reply.sender, reply.target, reply.body, reply.type) == (
            "Alice", "sender", "Blocked: the edit did not run.", InfoKind,
        )
        # A JSON value that is not the requested array is plain text too.
        reply = batch.response_message('{"files": 3}', "Alice")
        assert reply.body == '{"files": 3}'
        with pytest.raises(NativePiUnavailable):
            batch.response_message("  \n", "Alice")


def test_no_answer_format_is_demanded_of_the_model() -> None:
    """The routed-answer shape is no longer asked for or decoded.

    Found on GLM 5.3 Flash agents whose batch answer arrived in prose or in a
    near-miss array; the whole batch was then marked uncertain and never
    replayed. The turn already fixes sender, targets and type, so Core stamps
    the one plain answer onto each original route itself.
    """
    with alice_batch(("Alice", "What did you find?")) as batch:
        array = json.dumps(FieldCodec.encode(
            (Message("Alice", "sender", "Found it.", InfoKind, timestamp=0),)
        ))
        reply = batch.response_message(array, "Alice")
        assert (reply.target, reply.body) == ("sender", array)


def test_one_selected_turn_answers_one_reply_route() -> None:
    """A mixed direct/channel wave never shares one answer across audiences.

    The batch type itself groups the captured sources by reply route
    (SelectedSourceBatch.first_route); the gathered batch answers only the
    earliest route and the later routes stay pending for their own turns.
    """
    with alice_batch(("Alice", "A private question"), ("#team", "@Alice a team question")) as batch:
        assert batch.target == "sender"
        assert all(source.delivery.message.target == "Alice" for source in batch.sources)
        reply = batch.response_message("One answer for everyone.", "Alice")
        assert (reply.target, reply.body) == ("sender", "One answer for everyone.")


