"""PID registration is not an original loaded-session resource."""


import asyncio
import json
import os
import subprocess
import sys
from contextlib import AsyncExitStack

import pytest
from acp.schema import PromptRequest, TextContentBlock

from agent_comms.execution.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.coordination.errors import Refused
from agent_comms.errors import UnregisteredThreadError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.owner_protocol import OwnerFailure, OwnerLine
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.private_path import PrivateSocketRole
from agent_comms.transport.owner_connection import socket_path
from agent_comms.transport.runtime_requests import GoalHistoryRuntimeRequest
from served_requests import served


@pytest.mark.skipif(os.name == "nt", reason="original Unix owner socket")
@pytest.mark.parametrize("line", (b"{invalid json\n", b"[]\n"))
async def test_malformed_runtime_request_receives_failure_before_socket_closes(tmp_path, line):
    async with AsyncExitStack() as resources:
        owner = CommsAgent(wire(tmp_path / "wire"), auto_wake=False)
        resources.push_async_callback(owner.shutdown)
        await owner._runtime.start()
        with PrivateSocketRole.address(owner._runtime.path) as address:
            reader, writer = await asyncio.open_unix_connection(address)
        resources.push_async_callback(writer.wait_closed)
        resources.callback(writer.close)
        writer.write(line)
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=5)
        failure = FieldCodec.decode(OwnerLine, json.loads(response))
        assert isinstance(failure, OwnerFailure)
        assert failure.error
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""


@pytest.mark.skipif(os.name == "nt", reason="original Unix owner socket")
async def test_cli_registration_refuses_unbound_then_uses_original_owned_session(tmp_path):
    root = tmp_path / "wire"
    project = tmp_path / "project"
    project.mkdir()
    comms = wire(root)
    owner = CommsAgent(comms, auto_wake=False)
    client = CommsAgent(comms, auto_wake=False)
    registered = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "agent_comms.cli", "--root", str(root),
         "register", "--name", "registered", "--worktree", str(project),
         "--pid", str(os.getpid())],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(registered.stdout) == {"registered": "registered"}
    thread = comms.registry.require("registered")
    await owner._runtime.start()
    proxy = client._create_runtime_proxy(thread, AcpSessionId("registered"))
    try:
        for action, parameters in (
            (
                "prompt",
                {
                    "request": PromptRequest(
                        session_id="registered",
                        prompt=[
                            TextContentBlock(
                                type="text", text="Unbound input must never execute"
                            )
                        ],
                    )
                },
            ),
            ("goal_history", {}),
        ):
            with pytest.raises(Refused) as caught:
                await proxy.request(action, **parameters)
            assert caught.value.code == Refused.INVALID_PARAMS
            assert caught.value.data["owner"] == "registered"
        assert not owner.sessions.published
        assert not owner._runtime.clients
        assert comms.registry.require("registered") == thread
        assert not (root / "native-sessions").exists()
        assert not (root / "input_dispositions.sqlite3").exists()

        retired = NativeThread('retired-owner', frozenset(), Checkout(project), process_identity=ProcessIdentity.capture(os.getpid()))
        comms.registry.declare(retired)
        await owner.sessions.bind_owned(retired, AcpSessionId("retired-acp-session"))
        await asyncio.to_thread(comms.owners.stop, retired.name)
        comms.registry.remove(retired.name)
        snapshot = comms.registry.snapshot()
        assert snapshot.canonical_name(retired.name) == retired.name
        assert comms.registry.canonical_name(retired.name) == retired.name
        with pytest.raises(UnregisteredThreadError):
            served(GoalHistoryRuntimeRequest(thread=retired.name)).require_owner(comms.registry.entry(retired.name).entry)

        # Use the original production publisher before another independent read.
        # The failed prompt is never resent or turned into a binding.
        await owner.sessions.bind_owned(thread, AcpSessionId("original-acp-session"))
        assert await proxy.request("goal_history") == {"history": [], "failures": []}
        comms.registry.rename("registered", "renamed-owner")
        assert await proxy.request("goal_history") == {"history": [], "failures": []}
        assert (
            owner.sessions.require_owned_session(comms.registry.entry("renamed-owner").entry)
            == "original-acp-session"
        )
        assert {session: published.name for session, published in owner.sessions.published.items()} == {
            "original-acp-session": "registered",
        }
        # A same-name replacement in this process owns none of the original's loaded sessions.
        shadowed = NativeThread("shadowed-owner", frozenset(), Checkout(project),
                          process_identity=ProcessIdentity.capture(os.getpid()))
        comms.registry.declare(shadowed)
        loaded = comms.registry.require("shadowed-owner")
        await owner.sessions.bind_owned(loaded, AcpSessionId("shadowed-acp-session"))
        comms.registry.remove("shadowed-owner")
        comms.registry.declare(NativeThread("shadowed-owner", frozenset(), Checkout(project),
                                      process_identity=ProcessIdentity.capture(os.getpid())))
        replacement = comms.registry.entry("shadowed-owner").entry
        assert replacement.thread.incarnation != loaded.incarnation
        with pytest.raises(Refused):
            owner.sessions.require_owned_session(replacement)
        # Restoring the same birth allocates a new owner lease; the retained session keeps
        # the old allocation, so the restored row does not own it either.
        comms.registry.remove("shadowed-owner")
        comms.registry.declare(loaded)
        restored = comms.registry.entry("shadowed-owner").entry
        assert restored.thread.incarnation == loaded.incarnation
        with pytest.raises(Refused):
            owner.sessions.require_owned_session(restored)
        await asyncio.to_thread(comms.owners.stop, "shadowed-owner")
        assert not (root / "native-sessions").exists()
        assert not (root / "input_dispositions.sqlite3").exists()
    finally:
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()
    assert not socket_path(root, os.getpid()).exists()


def test_a_captured_owner_binds_only_its_birth_after_its_name_is_reused_in_process(tmp_path):
    """The owner a caller checked is the owner the request binds: a same-process replacement
    under the same name is refused at bind, and a rename of the captured owner still binds."""
    from agent_comms.errors import RelationViolationError
    from agent_comms.transport.owner_connection import AnyOwner, CapturedOwner

    comms = wire(tmp_path / "wire")
    project = tmp_path / "project"
    project.mkdir()
    here = ProcessIdentity.capture(os.getpid())
    original = NativeThread("bound-owner", frozenset(), Checkout(project), process_identity=here)
    comms.registry.declare(original)
    captured = comms.registry.require("bound-owner").incarnation

    comms.registry.rename("bound-owner", "renamed-owner")
    renamed = comms.registry.entry("renamed-owner").entry
    served(GoalHistoryRuntimeRequest(thread="renamed-owner", captured=CapturedOwner(captured))).require_owner(renamed)

    comms.registry.remove("renamed-owner")
    comms.registry.declare(NativeThread("renamed-owner", frozenset(), Checkout(project), process_identity=here))
    replacement = comms.registry.entry("renamed-owner").entry
    assert replacement.thread.incarnation != captured
    with pytest.raises(RelationViolationError, match="incarnation changed"):
        served(GoalHistoryRuntimeRequest(thread="renamed-owner", captured=CapturedOwner(captured))).require_owner(replacement)
    assert served(GoalHistoryRuntimeRequest(thread="renamed-owner", captured=AnyOwner())).require_owner(replacement) \
        == replacement.thread
