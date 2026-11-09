"""Canonical bus setup for owner lifecycle tests with no automatic model work."""


import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from functools import partial

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.comms import Comms, wire
from agent_comms.execution.owned_turn import OwnedTurn
from agent_comms.execution.native_arguments import GivenBinary
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.execution.turn_input_source import NoTurnAcceptance
from agent_comms.projects.storage_rules import StorageRule


def canonical_goal_wire(root):
    """Create an empty real checked source for goal/wait fixture operations."""
    comms = wire(root)
    comms.registry.storage_rule = StorageRule(
        volume=root.parent, checkout_base=root.parent,
        scratch_base=root.parent / "scratch",
    )
    return comms


class GoalTurnOwners(AsyncExitStack):
    """The test owns native owners until its last observation, not just its last turn."""

    def __init__(self, package: VerifiedNativePackage) -> None:
        super().__init__()
        self.package = package

    async def for_bus(self, comms: Comms) -> Callable[..., AbstractAsyncContextManager[OwnedTurn]]:
        agent = CommsAgent(comms, GivenBinary(str(self.package.cli)), auto_wake=False)
        self.push_async_callback(agent.shutdown)
        session = AcpSessionId("goal-execution-session")
        await agent.sessions.bind_owned(comms.registry.require("worker"), session)
        return partial(acquired_turn, agent, session)


@pytest.fixture
async def goal_turn_owners(native_runtime_package: VerifiedNativePackage) -> AsyncIterator[GoalTurnOwners]:
    async with GoalTurnOwners(native_runtime_package) as owners:
        yield owners


@asynccontextmanager
async def acquired_turn(
    agent: CommsAgent, session: AcpSessionId, comms: Comms, *, original_owner_input: bool = False,
) -> AsyncIterator[OwnedTurn]:
    owner = comms.registry.require("worker")
    turn = OwnedTurn(
        agent.turns, session, owner.name, "Continue original work",
        original_owner_input=original_owner_input, original_goal_id=owner.goal.require().id,
        acceptance=NoTurnAcceptance(),
    )
    async with AsyncExitStack() as resources, AsyncExitStack() as permits:
        assert await turn.acquire(resources, permits)
        yield turn


@pytest.fixture
def copied_goal_bus(tmp_path):
    source = canonical_goal_wire(tmp_path / "source")
    target = tmp_path / "copy"
    shutil.copytree(source.root, target)
    return canonical_goal_wire(target)


