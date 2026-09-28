"""Canonical ACP owners sharing the production bus, SQL stores and owner socket."""

from contextlib import asynccontextmanager

from agent_comms.acp import CommsAgent
from test_coordinated_runtime import _root


@asynccontextmanager
async def canonical_delivery_owner(root, *, direct=False):
    """Only model execution is supplied by tests; routing and persistence are real."""
    _path, root_id, comms, initial, people = _root(root, direct=direct)
    owner = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=root,
        private_nk_wire_root_id=root_id,
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
