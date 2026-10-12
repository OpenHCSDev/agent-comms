"""How a selected turn's final answer becomes replies.

Real private bus, registry and coordination stores; the last test runs the
real Pi child against a local provider. Found on a live thread whose model
ended a direct-message turn in plain text: the turn was recorded as failed
and the answer was never delivered.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.selected_source_batch import SelectedSource, SelectedSourceBatch
from agent_comms.threads import Thread
from native_backend_fixture import native_backend_fixture

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private cohort admission requires real /var/tmp"
)


@contextmanager
def alice_batch(*sent: tuple[str, str]):
    """Alice's selected sources for these (target, text) messages from sender."""
    with TemporaryDirectory(prefix="ac-answer-", dir="/var/tmp") as dirname:
        root = Path(dirname) / "wire"
        root.mkdir(mode=0o700)
        comms = Comms(root)
        for name, created in (("sender", 17021.0), ("Alice", 17022.0)):
            comms.registry.declare(Thread(
                name, frozenset({"team"}), dirname,
                process_identity=ProcessIdentity.capture(os.getpid()), created_at=created,
            ))
        root_id = comms.messaging.initialize_private_initial_protocol()
        messages = [comms.messaging.send_initial_cohort("sender", target, text) for target, text in sent]
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            deliveries = [comms.bus.log.read_delivery_cohort(root_id, m.seq) for m in messages]
            for recipient in deliveries[0].audience.recipients:
                store.participants.register(
                    recipient.recipient_lookup, recipient.canonical_thread,
                    recipient.canonical_thread, committed=True,
                )
            for message in messages:
                accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            lookup = stable_thread_lookup(comms.registry.require("Alice").created_at)
            assignments = sealed_cohort_assignments(store, lookup)
            yield SelectedSourceBatch(tuple(
                SelectedSource(assignment.assignment_id, store.assignments, delivery)
                for assignment, delivery in zip(assignments, deliveries, strict=True)
            ))


def test_plain_answer_to_one_reply_route_is_that_routes_reply() -> None:
    with alice_batch(("Alice", "What did you find?")) as batch:
        assert batch.targets == ("sender",)
        reply, = batch.response_messages("Blocked: the edit did not run.\n", "Alice")
        assert (reply.sender, reply.target, reply.body, reply.type) == (
            "Alice", "sender", "Blocked: the edit did not run.", MessageType.INFO,
        )
        # A JSON value that is not the requested array is plain text too.
        reply, = batch.response_messages('{"files": 3}', "Alice")
        assert reply.body == '{"files": 3}'
        with pytest.raises(NativePiUnavailable):
            batch.response_messages("  \n", "Alice")


def test_array_answer_still_has_to_match_the_reply_routes() -> None:
    with alice_batch(("Alice", "What did you find?")) as batch:
        def array(target):
            return json.dumps(FieldCodec.encode(
                (Message("Alice", target, "Found it.", MessageType.INFO, timestamp=0),)
            ))

        reply, = batch.response_messages(array("sender"), "Alice")
        assert (reply.target, reply.body) == ("sender", "Found it.")
        with pytest.raises(NativePiUnavailable):
            batch.response_messages(array("#team"), "Alice")
        with pytest.raises(NativePiUnavailable):
            batch.response_messages('["Found it."]', "Alice")


def test_plain_answer_to_several_reply_routes_is_refused() -> None:
    with alice_batch(("Alice", "A private question"), ("#team", "@Alice a team question")) as batch:
        assert batch.targets == ("sender", "#team")
        with pytest.raises(NativePiUnavailable) as refused:
            batch.response_messages("One answer for everyone.", "Alice")
        assert "cannot be divided between 2 reply routes" in str(refused.value.__cause__)


@pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"),
    reason="Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle",
)
async def test_direct_message_answered_in_plain_text_is_delivered(tmp_path) -> None:
    async with native_backend_fixture(tmp_path) as native:
        await native.author_history()
        async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
            comms = agent._comms
            comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
            comms.messaging.send_message("sender", session, "What did you find?")
            native.provider.text = "Blocked: the edit did not run."
            await agent.inputs.drain_inbox(session)
            async with asyncio.timeout(60):
                while native.provider.posts < 1 or agent.turns.turn_state(session).busy:
                    await asyncio.sleep(0.01)
            replies = [
                (m.target, m.body) for m in comms.views.full_history() if m.sender == session
            ]
            assert replies == [("sender", "Blocked: the edit did not run.")]
            assert not comms.registry.require(session).executing
