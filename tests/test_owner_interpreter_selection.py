"""Restart captures the original per-session launch over the real owner socket."""

import sys
from dataclasses import replace
from functools import partial

import pytest
from xdist.remote import setproctitle

from agent_comms.coordination.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.execution.owner_lifecycle import OwnerRestartSelection
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.owner_requests import RetainedOwnerLaunch
from delivery_owner_fixture import canonical_agent, start_headless_owner


async def test_original_session_launches_share_host_and_refuse_changed_selection(comms, tmp_path, native_runtime_package):
    agent = canonical_agent(comms, auto_wake=False)
    try:
        expected = {}
        for name in ("older", "current"):
            comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)))
            launch = agent.inputs.configuration.launch
            original = replace(launch, environment={
                **launch.environment, "AUTHORED_CREDENTIAL": name,
            })
            await start_headless_owner(agent, name, original)
            expected[name] = agent.inputs.state(name).owned().configuration.launch
        snapshot = comms.registry.snapshot()
        # This owner is the test process, and pytest-xdist retitles its workers per test, which
        # overwrites argv[0]. A launched owner's argv[0] is its interpreter (owner_lifecycle.py).
        # xdist's own writer does nothing exactly where xdist could not have retitled it either.
        setproctitle(sys.executable)
        selections = tuple(OwnerRestartSelection.capture(snapshot, name) for name in expected)
        assert len({selected.process for selected in selections}) == 1
        for selected in selections:
            launch = await Coordination.run_worker(partial(
                RetainedOwnerLaunch.capture, selected, snapshot, comms.root))
            assert launch == expected[selected.name]
            assert launch.environment["AUTHORED_CREDENTIAL"] == selected.name
            with pytest.raises(RelationViolationError):
                await Coordination.run_worker(partial(
                    RetainedOwnerLaunch.capture, replace(selected, interpreter="/wrong/python"),
                    snapshot, comms.root))
    finally:
        await agent.shutdown()
