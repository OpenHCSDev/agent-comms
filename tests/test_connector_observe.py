"""A UI grant observes its root through the connector, as declared records."""

import asyncio

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.transport.connector import Connector
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.connector_store import ConnectorStore, UiReadAccess
from agent_comms.ui_model.observation import (
    ObservationRequest,
    ObserveSidebar,
    ServiceResult,
    SidebarResult,
)
from bus_record_fixture import prepare_native_comms


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


async def test_a_stopping_connector_ends_an_open_observation(tmp_path, comms: Comms) -> None:
    """This socket joined no registry, so the stop could not see it: begin_shutdown closed
    every open inbox watch and never reached the observation. What a socket the stop cannot
    end costs is aiohttp's shutdown_timeout, and that bill was paid once already, on
    2026-11-06, by a watch handler busy away from its socket; the lesson sits in
    OpenWatch.ended() one module over. This is that shape, not that measurement.

    Note the other meaning of the word here: the shutdown fixture's ShutdownWatch.observe is
    a background pass's phase, not this route, so nothing was covering this socket.

    Every long-lived socket now joins one list and the stop walks that list, so a route
    cannot open one the stop does not know about."""
    prepare_native_comms(comms)
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, UiReadAccess))
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    application = connector.application()
    async with TestServer(application) as server, TestClient(server) as client:
        async with client.ws_connect(
            "/observe", headers={"Authorization": f"Bearer {credential.token}"},
        ):
            async with asyncio.timeout(60):
                while not connector.open_viewers:
                    await asyncio.sleep(0.01)
            held, = connector.open_viewers
            assert not held.watching("claude-ai"), "an observation is nobody's inbox watch"

            await connector.begin_shutdown(application)

            assert held.viewer.closed, "the stop left the observation's socket open"
            # The handler is left cancelling, so waiting for it is the assertion: a handler
            # the stop cannot end is exactly what sat through aiohttp's shutdown_timeout.
            async with asyncio.timeout(10):
                while not held.handler.done():
                    await asyncio.sleep(0.01)


async def test_the_observe_socket_challenges_a_missing_bearer(tmp_path, comms: Comms) -> None:
    connector = Connector(ConnectorStore.at(tmp_path / "auth"), "http://127.0.0.1:18766", comms.root)
    async with TestServer(connector.application()) as server, TestClient(server) as client:
        response = await client.get("/observe")
        assert response.status == 401
        assert "resource_metadata" in response.headers["WWW-Authenticate"]


def test_a_relay_kills_a_service_that_does_not_exit() -> None:
    import multiprocessing
    import time

    from agent_comms.transport.connector import ObservationRelay

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


async def test_an_observer_that_reconnects_sees_what_changed_while_it_was_gone(
    tmp_path, comms: Comms,
) -> None:
    """Slice2's disconnect and reconnect: a UI whose socket drops and comes back sees the
    bus as it is now, not as it was when it left.

    Two measured facts shape this. A sidebar keeps answering, so settling waits out a
    window rather than waiting for quiet, which is a loop with no end. And the signal is
    the bus unread the frame's own snapshot carries: a revision moves on its own
    (`WireRevision.expiry_tick`), and a row's `unread` counts transcript reads, which a
    message on the bus never touches.
    """
    prepare_native_comms(comms)
    comms.registry.declare(NativeThread("alice", frozenset({"api"}), Checkout(tmp_path)))
    store = ConnectorStore.at(tmp_path / "auth")
    namespace = comms.registry.store.privacy().namespace
    credential = store.update(lambda grants: grants.ui_issued(namespace, 60, UiReadAccess))
    connector = Connector(store, "http://127.0.0.1:18766", comms.root)
    worktree = Checkout(comms.root.parent)
    wanted = ObserveSidebar(worktree, True, True)
    frame = FieldCodec.compact(FieldCodec.encode_document(wanted, ObservationRequest)).encode()
    reader = comms.messaging.user_identity(worktree).name

    async def sidebar(viewer) -> SidebarResult:
        """The sidebar this viewer holds after its staged reads: every frame within one
        window, keeping the last, so what is compared is what a UI ends up showing."""
        answered: SidebarResult | None = None
        try:
            async with asyncio.timeout(20):
                while True:
                    message = await viewer.receive()
                    assert message.type is WSMsgType.BINARY, message
                    result = FieldCodec.decode_document(
                        ServiceResult, FieldCodec.parse(message.data.decode()))
                    if isinstance(result, SidebarResult):
                        answered = result
        except TimeoutError:
            assert answered is not None, "the observer was told nothing in its window"
            return answered

    async with TestServer(connector.application()) as server, TestClient(server) as client:
        headers = {"Authorization": f"Bearer {credential.token}"}
        async with client.ws_connect("/observe", headers=headers) as viewer:
            await viewer.send_bytes(frame)
            before = await sidebar(viewer)

        # The socket is gone, and the bus moves on without it.
        comms.messaging.send("alice", reader, "CHANGED_WHILE_DISCONNECTED")

        async with client.ws_connect("/observe", headers=headers) as viewer:
            await viewer.send_bytes(frame)
            after = await sidebar(viewer)

    def unread(result: SidebarResult) -> int:
        return result.snapshot.unread["alice"].count

    assert unread(after) > unread(before), (
        "a reconnected observer was answered as the bus was when its socket dropped, so a "
        f"UI that reconnects would miss every message it was away for: alice's conversation "
        f"held {unread(before)} unread before and {unread(after)} after")
    assert after.request == wanted, "and it is still the interest the UI asked for"
