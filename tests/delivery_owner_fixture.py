"""Canonical ACP owners sharing the production bus, SQL stores and owner socket."""
from agent_comms.controller import SessionUpdateSink

from bus_record_fixture import prepare_native_comms

from contextlib import asynccontextmanager

from agent_comms.acp import CommsAgent
from agent_comms.launch_root import LaunchRoot


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
    """Prepare the actual bus and native authority, then select this owner's root."""
    prepare_native_comms(comms)
    LaunchRoot.from_path(comms.root).install()
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


