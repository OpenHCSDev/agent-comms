"""A bus thread's harness on another machine reads that thread's inbox through its profile."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from yarl import URL

from agent_comms.comms import Comms
from agent_comms.connector import Connector
from agent_comms.connector_profile import ThreadCredential
from agent_comms.connector_caller import ForkingCaller, GrantRefusedError
from agent_comms.connector_store import ConnectorStore
from agent_comms.remote_core import ConnectorCalls
from agent_comms.remote_thread import WatchInboxVerb
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsInboxTakenTool
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class Endpoint:
    url: URL


@dataclass(frozen=True)
class PlainThreadCalls(ConnectorCalls[ThreadCredential]):
    """A thread profile's calls over the test server's plain HTTP, which the HTTPS profile
    refuses; the calls read only the endpoint's URL and the credential's token."""

    endpoint: Endpoint
    credential: ThreadCredential


async def test_a_thread_profile_watches_its_own_inbox_across_the_connector(
    tmp_path: Path, comms: Comms,
) -> None:
    """Each message arrives once and in order, a later one wakes the waiting read, and a
    restarted watch does not replay what the earlier one finished with."""
    for name in ("sender", "worker"):
        comms.registry.declare(Thread(name, frozenset(), Checkout(comms.root)))
    store = ConnectorStore.at(tmp_path / "auth")
    credential = store.update(lambda grants: grants.thread_issued("worker"))
    comms.messaging.send("sender", "worker", "first")
    connector = Connector(store, "http://127.0.0.1:18767", comms.root)
    async with TestServer(connector.application()) as server:
        calls = PlainThreadCalls(Endpoint(server.make_url("/mcp")), credential)
        messages = WatchInboxVerb(profile=tmp_path / "profile", seconds=5).messages(calls)
        assert (await asyncio.to_thread(next, messages)).body == "first"
        waiting = asyncio.ensure_future(asyncio.to_thread(next, messages))
        await asyncio.sleep(0.5)
        comms.messaging.send("sender", "worker", "second")
        assert (await asyncio.wait_for(waiting, timeout=30)).body == "second"
        restarted = WatchInboxVerb(profile=tmp_path / "profile", seconds=5).messages(calls)
        assert (await asyncio.to_thread(next, restarted)).body == "second"


def test_a_thread_takes_only_its_own_oldest_inbox_message(comms: Comms) -> None:
    """The inbox decides what is the thread's: another thread's message, or one out of order,
    is refused and stays in the inbox."""
    for name in ("sender", "worker"):
        comms.registry.declare(Thread(name, frozenset(), Checkout(comms.root)))
    first = comms.messaging.send("sender", "worker", "first").message.reference
    second = comms.messaging.send("sender", "worker", "second").message.reference
    worker, sender = ThreadCaller("worker"), ThreadCaller("sender")
    with pytest.raises(ToolRefusedError):
        CommsInboxTakenTool(reference=first).apply(comms, sender)
    with pytest.raises(ToolRefusedError):
        CommsInboxTakenTool(reference=second).apply(comms, worker)
    assert CommsInboxTakenTool(reference=first).apply(comms, worker).reference == first
    assert CommsInboxTakenTool(reference=second).apply(comms, worker).reference == second


def test_a_thread_grant_replaces_the_earlier_grant_for_that_thread(tmp_path: Path) -> None:
    """Reissuing a thread's profile revokes its old bearer; other threads keep theirs."""
    store = ConnectorStore.at(tmp_path / "auth")
    first = store.update(lambda grants: grants.thread_issued("worker"))
    other = store.update(lambda grants: grants.thread_issued("sender"))
    second = store.update(lambda grants: grants.thread_issued("worker"))
    held = store.read()
    with pytest.raises(GrantRefusedError):
        held.caller_for(first.token)
    assert held.caller_for(second.token) == ForkingCaller("worker")
    assert held.caller_for(other.token) == ForkingCaller("sender")
