"""A UI's ObservationProcess served by a Core across the connector."""

import asyncio
from dataclasses import dataclass

from aiohttp.test_utils import TestServer
from yarl import URL

from agent_comms.comms import Comms
from agent_comms.transport.connector import Connector
from agent_comms.projects.checkouts import Checkout
from agent_comms.transport.connector_profile import EndpointRoute, ResolvedRoute
from agent_comms.transport.connector_store import ConnectorStore, UiReadAccess
from agent_comms.transport.remote_core import ConnectorClient, ObservationRetired
from agent_comms.ui_model.observation import (
    ConnectionClosed,
    ObservationProcess,
    ObserveSidebar,
    SidebarResult,
)
from bus_record_fixture import prepare_native_comms


@dataclass(frozen=True)
class Endpoint:
    url: URL


@dataclass(frozen=True)
class Credential:
    token: str


@dataclass(frozen=True)
class TestConnectorSource(ConnectorClient):
    """The peer reads only the endpoint, credential, route and TLS trust; the test server is plain
    HTTP, which the owner's HTTPS profile refuses, so the test names them directly."""

    endpoint: Endpoint
    credential: Credential
    route: EndpointRoute = ResolvedRoute()


async def test_an_observation_process_receives_its_sidebar_from_a_remote_core(tmp_path, comms: Comms) -> None:
    prepare_native_comms(comms)
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    granted = store.update(lambda grants: grants.ui_issued(namespace, 60, UiReadAccess))
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    wanted = ObserveSidebar(Checkout(comms.root.parent), True, True)
    async with TestServer(connector.application()) as server:
        source = TestConnectorSource(Endpoint(server.make_url("/mcp")), Credential(granted.token))
        sidebar: asyncio.Future[SidebarResult] = asyncio.get_running_loop().create_future()

        def observed(result) -> None:
            if isinstance(result, SidebarResult) and not sidebar.done():
                sidebar.set_result(result)

        def failed(error: Exception) -> None:
            if not sidebar.done():
                sidebar.set_exception(error)

        async with ObservationProcess(source) as process:
            process.start_receiving(observed, failed)
            process.request(wanted)
            answered = await asyncio.wait_for(sidebar, timeout=60)
        assert answered.request == wanted


def test_retiring_a_peer_ends_its_relay_while_the_connector_is_still_connecting() -> None:
    """A listener that accepts but never answers the upgrade: retiring the peer must not wait."""
    import socket
    import time

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        peer = TestConnectorSource(Endpoint(URL(f"http://127.0.0.1:{port}")), Credential("t")).observation_peer()
        accepted, _ = listener.accept()
        with accepted:
            started = time.monotonic()
            peer.connection.close()
            peer.retire()
            assert not peer.thread.is_alive()
            assert time.monotonic() - started < 5
            assert ConnectionClosed().reported_by(peer).ending == ObservationRetired()


def test_an_ended_observation_reports_its_own_cause() -> None:
    """The service's traceback is reported as itself; a plain close reports the relay's recorded
    end, and waiting on the relay never rewrites that end as a retirement."""
    from agent_comms.ui_model.observation import ServiceReported

    peer = TestConnectorSource(Endpoint(URL("http://127.0.0.1:9/mcp")), Credential("token")).observation_peer()
    try:
        peer.join()
        assert not peer.thread.is_alive()
        assert peer.ending != ObservationRetired()
        assert "Traceback: the service failed" in str(ServiceReported("Traceback: the service failed").reported_by(peer))
        closed = str(ConnectionClosed().reported_by(peer))
        assert peer.ending.describe() in closed and "Traceback" not in closed
    finally:
        peer.retire()
