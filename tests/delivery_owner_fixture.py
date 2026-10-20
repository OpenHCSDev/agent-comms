"""Canonical ACP owners sharing the production bus, SQL stores and owner socket."""

from contextlib import asynccontextmanager

from agent_comms.acp import CommsAgent
from agent_comms.launch_root import LaunchRoot


class FakeClient:
    async def session_update(self, **kwargs):
        pass


def native_model(*, decision="FULL", fail_on=None):
    """Use the shared model boundary with the production selected-session owner."""
    from test_coordinated_runtime import _fake_model

    return _fake_model(decision=decision, fail_on=fail_on)


@asynccontextmanager
async def canonical_delivery_owner(root, *, direct=False):
    """Only model execution is supplied by tests; routing and persistence are real."""
    from test_coordinated_runtime import _root

    _path, root_id, comms, initial, people = _root(root, direct=direct)
    owner = CommsAgent(
        comms,
        runtime_enabled=True,
    )
    for person in people[1:]:
        owner.sessions.bindings[person.name] = person.name
        owner.sessions.titles[person.name] = person.name
        owner.sessions.worktrees[person.name] = person.worktree
    await owner._runtime.start()
    try:
        yield comms, owner, initial.message, root_id
    finally:
        await owner.shutdown()


def canonical_agent(comms, **options):
    """Configure the existing owner against its real canonical root marker.

    ACP startup selects this process root before constructing the agent. Tests
    construct it here instead; conftest restores the prior selection at teardown.
    """
    LaunchRoot.from_path(comms.root).install()
    with comms.bus.log.locked():
        metadata = comms.bus.log.read_metadata_unlocked()
    if not metadata.private:
        comms.messaging.initialize_private_initial_protocol()
    agent = CommsAgent(
        comms,
        **options,
    )
    agent.on_connect(FakeClient())
    return agent


async def reply_turn_finished(owner, session):
    """Wait for the reply turn a drain scheduled; the task removes itself when it ends."""
    task = owner.inputs.state(session).wake_task
    if task is not None:
        await task


def drain_diagnostic(comms, thread):
    """What the owner records on its thread when its inbox drain failed."""
    from agent_comms.comms import Comms

    view = next(
        view for view in Comms(comms.root).views.thread_views() if view.thread.name == thread
    )
    return view.activity.readiness.source_diagnostic()
