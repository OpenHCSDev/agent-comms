"""Scoped attachment retirement through original stores, sockets and ACP routing."""

import asyncio
import os

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router
from acp.schema import SessionInfoUpdate

from agent_comms.acp import CommsAgent, CommsClient
from agent_comms.comms import wire
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from agent_comms.child_process import ProcessIdentity


class AttachmentClient:
    def __init__(self):
        self.updates = []
        self.entered = {name: asyncio.Event() for name in ("alpha", "beta")}
        self.release = asyncio.Event()
        self.retired = set()

    async def session_update(self, *, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    async def request_permission(self, *, session_id, **kwargs):
        self.entered[session_id].set()
        try:
            await self.release.wait()
            return {"outcome": {"outcome": "cancelled"}}
        finally:
            self.retired.add(session_id)


@pytest.mark.asyncio
async def test_close_retires_one_real_subscription_and_permission(tmp_path, monkeypatch):
    # This declared model catalogue avoids SDK/model discovery; no model runs.
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one")
    comms = wire(tmp_path / "w")
    owner = CommsAgent(comms, runtime_enabled=True, auto_wake=False)
    client = CommsClient(comms, auto_wake=False, use_unstable_protocol=True)
    other = CommsClient(comms, auto_wake=False, use_unstable_protocol=True)
    callbacks = AttachmentClient()
    client.on_connect(callbacks)
    permissions = []
    try:
        for name in ("alpha", "beta"):
            project = tmp_path / name
            project.mkdir()
            thread = owner.sessions.declare_thread(str(project))
            await owner.sessions.bind_owned(thread, name)
            await client.sessions.attach_owner(thread, name)
        await other.sessions.attach_owner(comms.registry.require("alpha"), "alpha")
        alpha = client.sessions.proxies["alpha"]
        beta = client.sessions.proxies["beta"]
        retained_owner = comms.registry.require("alpha").require_process()
        selected = {}
        for name, proxy in (("alpha", alpha), ("beta", beta)):
            selected[name] = next(socket for socket in owner._runtime.clients[name]
                                  if socket.token == proxy.controller_token)
            permissions.append(asyncio.create_task(owner._runtime.request_permission(
                name, selected[name], {"toolCall": {}, "options": []})))
        async with asyncio.timeout(5):
            await asyncio.gather(*(event.wait() for event in callbacks.entered.values()))
        router = build_agent_router(client, use_unstable_protocol=client.use_unstable_protocol)
        assert (await client.initialize(1)).agent_capabilities.session_capabilities.close is not None
        result = await router("session/close", {"sessionId": "alpha"}, False)
        assert result == {}  # SDK normalize_result serializes the official response.
        assert "alpha" not in client.sessions.proxies
        assert client.sessions.proxies["beta"] is beta
        assert alpha.task.done() and not alpha._permission_tasks and not alpha._requests
        assert callbacks.retired == {"alpha"}
        async with asyncio.timeout(5):
            assert await permissions[0] is None
        assert not permissions[1].done()
        assert comms.registry.require("alpha").require_process() == retained_owner
        assert retained_owner.alive() and comms.registry.status("alpha").running
        assert owner.sessions.bindings == {"alpha": "alpha", "beta": "beta"}
        assert other.sessions.proxies["alpha"].controller_token is not None
        assert owner._runtime.is_controller("beta", selected["beta"])
        await owner._runtime.session_update(session_id="beta", update=SessionInfoUpdate(
            session_update="session_info_update", title="Still attached"))
        async with asyncio.timeout(5):
            while not any(name == "beta" and update.get("title") == "Still attached"
                          for name, update in callbacks.updates):
                await asyncio.sleep(0)
        # Reloading the closed ID creates a new original subscription.
        await client.sessions.attach_owner(comms.registry.require("alpha"), "alpha")
        assert client.sessions.proxies["alpha"] is not alpha
    finally:
        callbacks.release.set()
        await client.shutdown()
        await other.shutdown()
        await asyncio.gather(*permissions, return_exceptions=True)
        await owner.shutdown()


@pytest.mark.asyncio
async def test_close_joins_unsent_request_and_capability_requires_admission(tmp_path):
    comms = wire(tmp_path / "w")
    comms.registry.declare(Thread("cold", frozenset(), str(tmp_path),
                                 process_identity=ProcessIdentity.capture(os.getpid())))
    client = CommsClient(comms, auto_wake=False, use_unstable_protocol=True)
    proxy = RuntimeProxy(client, "cold", socket_path(comms.root, os.getpid()))
    client.sessions.proxies["cold"] = proxy
    request = asyncio.create_task(proxy.request("clear_queue"))
    try:
        async with asyncio.timeout(5):
            while not proxy._requests:
                await asyncio.sleep(0)
        await client.close_session("cold")
        assert request.cancelled() and not proxy._requests
        assert comms.registry.status("cold").running
        with pytest.raises(ConnectionError, match="closed before request dispatch"):
            await proxy.request("clear_queue")
        with pytest.raises(RequestError):
            await client.close_session("cold")
        for agent_type, unstable in ((CommsClient, False), (CommsAgent, True)):
            agent = agent_type(comms, auto_wake=False, use_unstable_protocol=unstable)
            try:
                assert (await agent.initialize(1)).agent_capabilities.session_capabilities.close is None
                with pytest.raises(RequestError):
                    await agent.close_session("cold")
            finally:
                await agent.shutdown()
    finally:
        await client.shutdown()
