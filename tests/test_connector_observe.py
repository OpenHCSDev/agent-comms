"""A UI grant observes its root through the connector, as declared records."""

import asyncio

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from bus_record_fixture import prepare_native_comms

from agent_comms.comms import Comms
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore, UiReadAccess
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.threads import NativeThread
from agent_comms.repo import Checkout
from agent_comms.ui_model.observation import ObservationRequest, ObserveSidebar, ServiceResult, SidebarResult


async def test_a_ui_grant_receives_its_sidebar_over_the_observe_socket(tmp_path, comms: Comms) -> None:
    prepare_native_comms(comms)
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, UiReadAccess))
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    wanted = ObserveSidebar(Checkout(comms.root.parent), True, True)
    async with TestServer(connector.application()) as server, TestClient(server) as client:
        async with client.ws_connect(
            "/observe", headers={"Authorization": f"Bearer {credential.token}"},
        ) as viewer:
            frame = FieldCodec.compact(FieldCodec.encode_document(wanted, ObservationRequest))
            await viewer.send_bytes(frame.encode())

            async def sidebar() -> SidebarResult:
                while True:
                    message = await viewer.receive()
                    assert message.type is WSMsgType.BINARY, message
                    result = FieldCodec.decode_document(
                        ServiceResult, FieldCodec.parse(message.data.decode()))
                    if isinstance(result, SidebarResult):
                        return result

            answered = await asyncio.wait_for(sidebar(), timeout=60)
            assert answered.request == wanted


async def test_the_observe_socket_challenges_a_missing_bearer(tmp_path, comms: Comms) -> None:
    connector = Connector(ConnectorStore.at(tmp_path / "auth"), "http://127.0.0.1:18766", comms.root)
    async with TestServer(connector.application()) as server, TestClient(server) as client:
        response = await client.get("/observe")
        assert response.status == 401
        assert "resource_metadata" in response.headers["WWW-Authenticate"]


def test_a_relay_kills_a_service_that_does_not_exit() -> None:
    import multiprocessing
    import time

    from agent_comms.connector import ObservationRelay

    relay = ObservationRelay.__new__(ObservationRelay)
    relay.process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,))
    relay.process.start()
    relay.exit_seconds = 0.2
    relay.retire()
    assert relay.process.exitcode is not None


async def test_a_message_on_the_bus_reaches_the_observer(tmp_path, comms: Comms) -> None:
    """The first thing the remote-bus plan's slice2 asks to see: one message on that bus
    updating what a UI across the connector holds.

    The sidebar case above proves a grant can read; this proves it is told when the bus
    changes, which is the difference between a remote read and a remote view.

    It settles first, because a sidebar observation reads in stages (RosterSidebarRead,
    then the full read) and answers twice on its own: asserting a second frame after a send
    passed with the send removed, which is no assertion at all.
    """
    prepare_native_comms(comms)
    for name in ("alice", "bob"):
        comms.registry.declare(NativeThread(name, frozenset({"api"}), Checkout(tmp_path)))
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, UiReadAccess))
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    wanted = ObserveSidebar(Checkout(comms.root.parent), True, True)

    async with TestServer(connector.application()) as server, TestClient(server) as client:
        async with client.ws_connect(
            "/observe", headers={"Authorization": f"Bearer {credential.token}"},
        ) as viewer:
            frame = FieldCodec.compact(FieldCodec.encode_document(wanted, ObservationRequest))
            await viewer.send_bytes(frame.encode())

            async def observed(seconds: float) -> SidebarResult | None:
                """The next sidebar this observer is told, or nothing within `seconds`."""
                try:
                    async with asyncio.timeout(seconds):
                        while True:
                            message = await viewer.receive()
                            assert message.type is WSMsgType.BINARY, message
                            result = FieldCodec.decode_document(
                                ServiceResult, FieldCodec.parse(message.data.decode()))
                            if isinstance(result, SidebarResult):
                                return result
                except TimeoutError:
                    return None

            settled = await asyncio.wait_for(observed(60), timeout=60)
            assert settled is not None, "the observer was never told anything"
            while (again := await observed(3)) is not None:
                settled = again  # Its own staged reads, which are not the change under test.

            comms.messaging.send("alice", "bob", "REMOTE_VIEW_UPDATE")
            told = await observed(30)

    assert told is not None, (
        "the bus changed and the observer was told nothing, so a remote view would sit on "
        f"the sidebar it settled with: {settled.revision}")
    assert told.revision != settled.revision, "and the revision it carries is the new one"
    assert told.request == wanted, "and it is still the interest the UI asked for"
