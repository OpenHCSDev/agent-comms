"""A remote Core is reached over one kept connection per thread, and a dropped connection is
the request's to answer: a read is asked once more, a call that may change the bus is never sent
twice (Core may already have run it). Opening a fresh TLS connection per call cost 170-260 ms
of handshake each against ts-gaming; a dropped one surfaced as a crashed worker."""

import http.client
import json
import threading
import urllib.error

import pytest
from yarl import URL

from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.nominal.field_codec import RootId
from agent_comms.transport.connector_profile import (
    BearerSecret,
    HttpsMcpEndpoint,
    UiCredential,
    UiGrantId,
)
from agent_comms.transport.remote_core import RemoteCoreAccess
from agent_comms.transport.request import ConnectionDropped
from agent_comms.transport.tools.channels import CommsChannelsTool
from agent_comms.transport.tools.orders import CommsEmergencyReleaseTool

REPLY = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
    "content": [{"type": "text", "text": "{}"}], "isError": False, "structuredContent": {"read": True}}}).encode()


class Reply:
    status, reason, headers = 200, "OK", {}

    def read(self) -> bytes:
        return REPLY


class DroppingConnection:
    """A kept connection the server closed while idle: its first answer never comes."""

    def __init__(self, drops: int) -> None:
        self.drops, self.sent = drops, 0

    def request(self, method, path, body, headers) -> None:
        self.sent += 1

    def getresponse(self) -> Reply:
        if self.drops:
            self.drops -= 1
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return Reply()

    def close(self) -> None:
        pass


def access_over(connection: DroppingConnection) -> RemoteCoreAccess:
    access = RemoteCoreAccess(
        HttpsMcpEndpoint(URL("https://core.example/mcp")),
        UiCredential(UiGrantId("1" * 32), RootId("a" * 32), 2**31, BearerSecret("token")))
    access.connections.connection = connection
    return access


def test_a_read_survives_one_dropped_connection():
    connection = DroppingConnection(drops=1)

    assert access_over(connection).post(CommsChannelsTool()) == {"read": True}
    assert connection.sent == 2


def test_a_read_defers_when_its_fresh_connection_is_unavailable():
    connection = DroppingConnection(drops=2)
    with pytest.raises(CoordinationReadUnavailable) as failed:
        access_over(connection).post(CommsChannelsTool())
    assert isinstance(failed.value, CoordinationReadUnavailable)
    assert isinstance(failed.value.__cause__, ConnectionDropped)
    assert connection.sent == 2


@pytest.mark.parametrize('call,deferred', [
    (CommsChannelsTool(), True), (CommsEmergencyReleaseTool(), False),
])
def test_a_gateway_failure_defers_only_reads(call, deferred):
    class GatewayReply(Reply):
        status, reason = 502, 'Bad Gateway'

    class GatewayConnection(DroppingConnection):
        def getresponse(self):
            return GatewayReply()

    connection = GatewayConnection(drops=0)
    with pytest.raises(CoordinationReadUnavailable if deferred else urllib.error.HTTPError) as failed:
        access_over(connection).post(call)
    assert connection.sent == 1
    if deferred:
        assert isinstance(failed.value.__cause__, urllib.error.HTTPError)
    else:
        assert not isinstance(failed.value, CoordinationReadUnavailable)


def test_a_call_that_changes_the_bus_is_never_sent_twice():
    connection = DroppingConnection(drops=1)

    with pytest.raises(ConnectionDropped):
        access_over(connection).post(CommsEmergencyReleaseTool())
    assert connection.sent == 1


def test_a_thread_keeps_its_connection_and_another_thread_gets_its_own():
    access = RemoteCoreAccess(
        HttpsMcpEndpoint(URL("https://core.example/mcp")),
        UiCredential(UiGrantId("1" * 32), RootId("a" * 32), 2**31, BearerSecret("token")))
    here = access.connections.connection
    elsewhere: list[http.client.HTTPSConnection] = []
    worker = threading.Thread(target=lambda: elsewhere.append(access.connections.connection))
    worker.start()
    worker.join()

    assert access.connections.connection is here
    assert elsewhere[0] is not here
