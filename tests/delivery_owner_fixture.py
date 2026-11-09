"""Canonical ACP owners sharing the production bus, SQL stores and owner socket."""
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.native_arguments import GivenBinary
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.transport.owner_requests import RetainedOwnerLaunch
from bus_record_fixture import prepare_native_comms


@pytest.fixture
def qualified_owner_launch(native_runtime_package: VerifiedNativePackage) -> RetainedOwnerLaunch:
    """The admitted original command comes from the fixture's actual pinned package."""
    return RetainedOwnerLaunch.configured(GivenBinary(str(native_runtime_package.cli)))


class FakeClient(SessionUpdateSink):
    async def publish_update(self, **kwargs):
        return True


@asynccontextmanager
async def canonical_delivery_owner(root, *, direct=False):
    """Only model execution is supplied by tests; routing and persistence are real."""
    from test_coordinated_runtime import _root

    _path, root_id, comms, initial, people = _root(root, direct=direct)
    owner = CommsAgent(
        comms,
        auto_wake=True
    )
    for person in people[1:]:
        owner.sessions.publish(AcpSessionId(person.name), person)
    await owner._runtime.start()
    try:
        yield comms, owner, initial.message, root_id
    finally:
        await owner.shutdown()


def canonical_agent(comms, **options):
    """Prepare the actual bus and native authority, then select this owner's root."""
    prepare_native_comms(comms)
    LaunchRoot.from_path(comms.root).install()
    agent = CommsAgent(
        comms,
        **options,
    )
    agent.on_connect(FakeClient())
    return agent


async def start_headless_owner(agent: CommsAgent, name: str, launch: RetainedOwnerLaunch) -> None:
    """Bind an actual local lease with the fixture's original session configuration."""
    thread = await Coordination.run_worker(partial(agent._comms.owners.acquire_thread, name))
    entry = (await Coordination.run_worker(partial(agent._comms.registry.entry, name))).entry
    await agent.sessions.start_owner(replace(
        launch, process=thread.require_process(), environment=thread.launch().apply(launch.environment),
    ), entry.owner_identity)


async def reply_turn_finished(owner, session):
    """Wait for the reply turn a drain scheduled; the task removes itself when it ends."""
    task = owner.inputs.state(session).wake_task
    if task is not None:
        await task


