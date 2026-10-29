"""Open notification interests refresh from real input-only and reply-only commits."""

from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from pathlib import Path

from agent_comms.bus_publication import Delivered
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.message_reference import MessageReference
from agent_comms.notification_evidence import AnsweredResponse, RecipientLimit, UnansweredResponse
from agent_comms.notification_reads import LiveNotificationRead
from agent_comms.private_path import FileObservation, FileRevision
from agent_comms.ui_model.observation import (
    NotificationsObserved, ObserveNotifications, ObservationService,
    ReadNotifications, RevisionObserved,
)
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_following_context_notifications import following_input, following_session
from test_notification_recipient_window import frozen_window  # noqa: F401


@contextmanager
def observing_notifications(
    comms: Comms, references: tuple[MessageReference, ...],
) -> Iterator[tuple[ObservationService, Connection]]:
    client, server = Pipe()
    with client, server:
        service = ObservationService(comms, server)
        request = ReadNotifications(
            str(comms.root), LiveNotificationRead(references, limit=RecipientLimit(1)),
        )
        ObserveNotifications(frozenset((request,))).receive(service)
        try:
            yield service, client
        finally:
            service.retire()
            comms.views.transcript_reads.close()


def notification_reply(client: Connection) -> NotificationsObserved:
    """Both real pipe frames must arrive; a missed wake fails without a blocking recv."""
    assert client.poll()
    revision: object = client.recv()
    assert isinstance(revision, RevisionObserved)
    assert client.poll()
    result: object = client.recv()
    assert isinstance(result, NotificationsObserved)
    assert result.revision == revision.revision
    assert not client.poll()
    return result


def coordinator_revision(comms: Comms) -> tuple[FileObservation, ...]:
    return tuple(FileRevision.capture(path)
                 for path in CoordinationStore.source_paths(comms.root))


async def test_open_notification_interest_refreshes_after_input_only_context(tmp_path: Path) -> None:
    async with following_session(tmp_path) as claim:
        case = following_input(claim, generation=2)
        case.start()
        proof = await case.verify()
        comms = claim.selected.comms
        with observing_notifications(comms, case.references) as (service, client):
            await service.observe(set())
            before = notification_reply(client)
            old, = before.outcomes[case.references[0].seq, case.references[0].message_id]
            assert not old.state.consumed and old.answer == UnansweredResponse()
            coordinator = coordinator_revision(comms)

            case.acknowledge(proof)

            await service.observe(set())
            after = notification_reply(client)
            assert coordinator_revision(comms) == coordinator
            assert after.revision.bus_changes == before.revision.bus_changes
            assert after.revision.bus_seq == before.revision.bus_seq
            assert after.revision.registry == before.revision.registry
            assert after.revision.activity == before.revision.activity
            assert after.revision.notifications != before.revision.notifications
            current, = after.outcomes[case.references[0].seq, case.references[0].message_id]
            assert current.state.consumed and current.state.at_ms == proof.committed_at_ms
            assert current.answer == UnansweredResponse()
            await service.observe(set())
            assert not client.poll()


async def test_open_notification_interest_refreshes_after_reply_only_commit(
    frozen_window: tuple[Comms, Delivered],
) -> None:
    comms, source = frozen_window
    reference = source.reference
    with observing_notifications(comms, (reference,)) as (service, client):
        await service.observe(set())
        before = notification_reply(client)
        old, = before.outcomes[reference.seq, reference.message_id]
        assert old.answer == UnansweredResponse() and not old.state.consumed
        coordinator = coordinator_revision(comms)

        response = comms.messaging.send(
            old.recipient_identity.canonical_thread, source.message.sender,
            "Independent answer", answers=(reference,),
        ).message

        await service.observe(set())
        after = notification_reply(client)
        assert coordinator_revision(comms) == coordinator
        assert after.revision.registry == before.revision.registry
        assert after.revision.activity == before.revision.activity
        assert after.revision.notifications != before.revision.notifications
        current, = after.outcomes[reference.seq, reference.message_id]
        assert current.answer == AnsweredResponse(response.reference, response.at_ms)
        assert not current.state.consumed
        await service.observe(set())
        assert not client.poll()
